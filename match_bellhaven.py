#!/usr/bin/env python3
"""Compare the Bellhaven website CSV with a fresh, read-only CRM snapshot."""

import argparse
from collections import Counter
import csv
from getpass import getpass
from http.client import HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from scrape_bellhaven import BASE_URL, CSV_FIELDS, Page, fetch_html, https_context


PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
CARE_TYPES = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}
STREET_WORDS = {
    "avenue": "ave", "boulevard": "blvd", "drive": "dr", "lane": "ln",
    "pike": "pk", "road": "rd", "street": "st", "north": "n", "south": "s",
    "east": "e", "west": "w", "northwest": "nw", "northeast": "ne",
    "southwest": "sw", "southeast": "se",
}
REPORT_FIELDS = (
    "classification", "website_location", "crm_account_id", "crm_account_name", "crm_parent",
    "flags", "evidence", "review_action", "website_address", "crm_billing_address",
    "website_care_offerings", "crm_care_type", "crm_status",
)
EXCEPTION_FIELDS = REPORT_FIELDS + ("related_crm_account_id", "lifetime_revenue", "outstanding_ar")


def fetch_accounts(token):
    connection = HTTPSConnection(urlsplit(BASE_URL).hostname, timeout=25, context=https_context())
    accounts = []
    total = None
    page = 1
    try:
        while total is None or len(accounts) < total:
            path = f"/api/v1/accounts?page={page}&page_size=50"
            connection.request("GET", path, headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise ValueError(f"CRM accounts page {page} returned HTTP {response.status}")
            result = json.loads(body)
            if total is None:
                total = result["total"]
            if result["page"] != page or result["total"] != total or not result["data"]:
                raise ValueError(f"CRM pagination changed or returned an empty page at page {page}")
            accounts.extend(result["data"])
            page += 1
    finally:
        connection.close()

    if len(accounts) != total or len({row["account_id"] for row in accounts}) != total:
        raise ValueError("CRM account count did not reconcile to the API total and unique IDs")
    return accounts


def words(value):
    return re.findall(r"[a-z0-9]+", value.casefold().replace("&", " and "))


def street_key(value):
    return tuple(STREET_WORDS.get(word, word) for word in words(value))


def location_evidence(location, account):
    website_street = street_key(location["address"])
    crm_street = street_key(account["billing_street"])
    same_name = words(location["name"]) == words(account["name"])
    same_city = words(location["city"]) == words(account["billing_city"])
    same_zip = location["zip"][:5] == account["billing_zip"][:5]
    if location["state"].casefold() != account["billing_state"].casefold():
        return ""
    if website_street and website_street == crm_street and same_city:
        return "Normalized street, city, and state agree." + (" Name agrees." if same_name else "")
    if same_name and same_city and same_zip:
        if crm_street[:2] == ("po", "box"):
            return "Name, city, state, and ZIP agree. CRM billing uses a PO Box."
        if website_street and crm_street and website_street[0].isdigit() and website_street[0] == crm_street[0]:
            return "Name, locality, and house number agree. Street wording differs."
    return ""


def website_phone(location):
    slug = "-".join(words(location["name"]))
    try:
        page = Page(fetch_html(f"{BASE_URL}/communities/{slug}"))
    except ValueError:
        # shortcut: Slugs follow the published names for the current duplicate clusters. If a slug
        # differs, use directory link labels to resolve the page before relying on its phone.
        return ""
    if words(page.name) != words(location["name"]):
        return ""
    return next(iter(page.details.get("Phone", [])), "")


def primary_match(location, candidates, parent_id, lookup_phone):
    eligible = [
        account for account in candidates
        if not account.get("duplicate_of_account") and not account.get("chow_current_account")
    ]
    if len(candidates) == 1 and eligible:
        return eligible[0], ""
    if not eligible:
        return None, "CRM links point to another record."

    phone = re.sub(r"\D", "", lookup_phone(location))
    phone_matches = [account for account in eligible if phone and re.sub(r"\D", "", account["phone"]) == phone]
    if len(phone_matches) == 1:
        return phone_matches[0], "Public phone agrees and distinguishes this CRM record."
    preferred = [
        account for account in eligible if account["parent_id"] == parent_id
        and words(account["name"]) == words(location["name"])
        and account["status"] == "Active"
    ]
    if len(preferred) == 1:
        return preferred[0], "Only active candidate with the published name and Bellhaven parent."
    if phone and not phone_matches:
        return None, "Public phone differs from every candidate's CRM phone."
    return None, "Available evidence does not identify one primary account."


def differences(location, account, parent_id):
    flags = []
    if account["parent_id"] != parent_id:
        flags.append("Wrong parent" if account["parent_id"] else "Missing parent")
    if words(account["name"]) != words(location["name"]):
        flags.append("CRM name differs from website")
    old_street = street_key(account["billing_street"])
    if old_street != street_key(location["address"]):
        flags.append("Billing PO Box versus website street" if old_street[:2] == ("po", "box") else "Billing street differs")
    for field in ("city", "state", "zip"):
        if words(account["billing_" + field]) != words(location[field]):
            flags.append(f"Billing {field.upper() if field == 'zip' else field} differs")
    offerings = [CARE_TYPES.get(care) for care in location["care_offerings"].split(" | ")]
    if None in offerings:
        flags.append("Care mapping needs review")
    elif account["care_type"] not in offerings:
        flags.append("CRM care type differs from website")
    elif len(set(offerings)) > 1:
        flags.append("Additional website care offering")
    if account["status"] != "Active":
        flags.append("Website-listed location has non-active CRM status")
    return flags


def as_address(street, city, state, zip_code):
    return ", ".join(filter(None, (street, city, " ".join(filter(None, (state, zip_code))))))


def report_row(classification, location=None, account=None, flags=(), evidence="", action=""):
    location = location or {}
    account = account or {}
    return {
        "classification": classification,
        "website_location": location.get("name", ""),
        "crm_account_id": account.get("account_id", ""),
        "crm_account_name": account.get("name", ""),
        "crm_parent": account.get("parent_name", ""),
        "flags": " | ".join(flags),
        "evidence": evidence,
        "review_action": action,
        "website_address": as_address(*(location.get(field, "") for field in ("address", "city", "state", "zip"))),
        "crm_billing_address": as_address(*(account.get("billing_" + field, "") for field in ("street", "city", "state", "zip"))),
        "website_care_offerings": location.get("care_offerings", ""),
        "crm_care_type": account.get("care_type", ""),
        "crm_status": account.get("status", ""),
    }


def exception_row(classification, location, account, evidence, action, related=None):
    return {
        **report_row(classification, location, account, evidence=evidence, action=action),
        "related_crm_account_id": related["account_id"] if related else "",
        "lifetime_revenue": account["lifetime_revenue"],
        "outstanding_ar": account["outstanding_ar"],
    }


def rejected_lookalike(location, account):
    if words(location["name"]) == words(account["name"]):
        return True
    generic = {"bellhaven", "at", "of", "the", "care", "center", "senior", "living"}
    shared_words = (set(words(location["name"])) & set(words(account["name"]))) - generic
    return bool(shared_words) and all(
        words(location[field]) == words(account["billing_" + field]) for field in ("city", "state")
    )


def same_billing_site(first, second):
    return bool(first["billing_street"]) and street_key(first["billing_street"]) == street_key(second["billing_street"]) and all(
        words(first[field]) == words(second[field]) for field in ("billing_city", "billing_state", "billing_zip")
    )


def match_locations(locations, accounts, lookup_phone=website_phone):
    parents = [account for account in accounts if account["name"] == PARENT_NAME and not account["parent_id"]]
    if len(parents) != 1:
        raise ValueError(f"Expected one Bellhaven parent account, found {len(parents)}")
    parent_id = parents[0]["account_id"]
    facilities = [account for account in accounts if not account["name"].endswith("(Parent Account)")]
    candidates_by_location = [
        [(account, evidence) for account in facilities if (evidence := location_evidence(location, account))]
        for location in locations
    ]
    candidate_counts = Counter(account["account_id"] for group in candidates_by_location for account, _ in group)
    matches = []
    exceptions = []

    for location, candidates in zip(locations, candidates_by_location):
        if not candidates:
            matches.append(report_row(
                "Missing in CRM", location, evidence="No account met the identity checks across all CRM accounts.",
                action="Confirm the site details before creating a new account.",
            ))
            for account in facilities:
                if rejected_lookalike(location, account):
                    exceptions.append(exception_row(
                        "Rejected lookalike", location, account, "Similar name. Physical address or locality differs.",
                        "Treat as a separate record pending evidence tying it to the website site.",
                    ))
            continue

        account, reason = primary_match(location, [candidate for candidate, _ in candidates], parent_id, lookup_phone)
        if account and candidate_counts[account["account_id"]] > 1:
            account, reason = None, "The same CRM account also qualifies for another website location."
        if not account:
            matches.append(report_row(
                "Needs review", location, flags=(f"{len(candidates)} qualifying CRM candidates",),
                evidence=reason, action="Verify account history before selecting a primary record.",
            ))
        else:
            flags = differences(location, account, parent_id)
            if len(candidates) > 1:
                flags.append(f"{len(candidates) - 1} other CRM record(s) at this site")
            evidence = next(evidence for candidate, evidence in candidates if candidate == account)
            matches.append(report_row(
                "Needs fix" if flags else "Confident match", location, account, flags,
                " ".join(filter(None, (evidence, reason))), "Review flagged fields and related records." if flags else "",
            ))

        for candidate, evidence in candidates:
            if candidate == account:
                continue
            if not account:
                classification = "Unresolved same-site candidate"
            elif candidate["parent_id"] == parent_id:
                classification = "Likely duplicate"
            else:
                classification = "Possible legacy account or duplicate"
            exceptions.append(exception_row(
                classification, location, candidate, evidence,
                "Review ownership history and existing account activity before deduplicating.", account,
            ))

    for account in facilities:
        if account["parent_id"] != parent_id or account["account_id"] in candidate_counts:
            continue
        related = [
            other for other in facilities if other["parent_id"] != parent_id and same_billing_site(account, other)
        ]
        action = "Verify operating status and ownership."
        if account["outstanding_ar"]:
            action += " Reconcile outstanding A/R before changing the CRM status."
        exceptions.append(exception_row(
            "CRM only under Bellhaven", None, account, "No matching community in the website CSV.", action,
            related[0] if len(related) == 1 else None,
        ))
        for other in related:
            exceptions.append(exception_row(
                "Possible successor", None, other, "Same normalized billing address as the linked Bellhaven account.",
                "Verify whether ownership changed and when.", account,
            ))

    return matches, exceptions


def write_csv(path, fields, rows):
    with TemporaryDirectory(dir=path.parent) as temp_dir:
        temporary_csv = Path(temp_dir) / path.name
        with temporary_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temporary_csv.replace(path)


def load_locations(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(CSV_FIELDS).issubset(reader.fieldnames or ()):
            raise ValueError("Location CSV is missing required columns")
        locations = list(reader)
    if not locations or any(not row.get(field) for row in locations for field in CSV_FIELDS):
        raise ValueError("Location CSV is empty or has incomplete location rows")
    return locations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locations", nargs="?", type=Path, default=Path(__file__).with_name("bellhaven_locations.csv"))
    args = parser.parse_args()

    try:
        locations = load_locations(args.locations)
        token = os.environ.get("BELLHAVEN_CRM_TOKEN") or getpass("CRM API token: ")
        if not token.strip():
            raise ValueError("A CRM API token is required")
        accounts = fetch_accounts(token.strip())
        matches, exceptions = match_locations(locations, accounts)
        write_csv(args.locations.with_name("bellhaven_crm_matches.csv"), REPORT_FIELDS, matches)
        write_csv(args.locations.with_name("bellhaven_crm_exceptions.csv"), EXCEPTION_FIELDS, exceptions)
    except (EOFError, HTTPException, KeyError, OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")

    print(f"Reviewed {len(locations)} website locations against {len(accounts)} CRM accounts.")
    for classification, count in sorted(Counter(row["classification"] for row in matches + exceptions).items()):
        print(f"{classification}: {count}")


if __name__ == "__main__":
    main()
