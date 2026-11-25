#!/usr/bin/env python3
"""Run the Bellhaven review queue on the local machine."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from getpass import getpass
from hashlib import sha256
from hmac import compare_digest
from http.client import HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from time import time_ns
from urllib.parse import parse_qs, quote, urlsplit

from match_bellhaven import (
    BASE_URL, CARE_TYPES, PARENT_NAME, fetch_accounts, load_locations, location_evidence,
    match_locations, same_billing_site, street_key, words,
)
from review_bellhaven_ui import render
from scrape_bellhaven import CSV_FIELDS, collect_locations, https_context


ROOT = Path(__file__).resolve().parent
ADDRESS_FIELDS = ("billing_street", "billing_city", "billing_state", "billing_zip")
BILLING_FIELDS = ("lifetime_revenue", "outstanding_ar")
ERRORS = (OSError, HTTPException, ValueError, KeyError)


class CRM:
    def __init__(self, token):
        self.token = token

    def accounts(self):
        return fetch_accounts(self.token)

    def request(self, method, account_id=None, payload=None):
        path = "/api/v1/accounts"
        if account_id:
            path += "/" + quote(account_id, safe="")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload)
        with closing(HTTPSConnection(urlsplit(BASE_URL).hostname, timeout=25, context=https_context())) as connection:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if not 200 <= response.status < 300:
                raise ValueError(f"CRM returned HTTP {response.status}")
            return json.loads(raw) if raw else {}


def protected_billing(account):
    for field in BILLING_FIELDS:
        if type(account.get(field)) not in (int, float) or not -float("inf") < account[field] < float("inf"):
            raise ValueError(f"Missing or invalid {field} on {account.get('account_id', 'CRM account')}")
    return account["lifetime_revenue"] > 0 and account["outstanding_ar"] > 0


def new_account(location, parent_id):
    care = CARE_TYPES.get(location["care_offerings"])
    if not care:
        return None
    return {
        "name": location["name"], "parent_id": parent_id,
        "billing_street": location["address"], "billing_city": location["city"],
        "billing_state": location["state"], "billing_zip": location["zip"],
        "care_type": care, "status": "Active",
    }


def guard_fields(account, *keys):
    return {key: account[key] for key in (*ADDRESS_FIELDS, *keys)}


def append_note(account, note):
    return "\n\n".join(filter(None, (account["note"], note)))


def build_queue(locations, accounts, matcher=match_locations):
    matches, exceptions = matcher(locations, accounts)
    parent = next(account for account in accounts if account["name"] == PARENT_NAME and not account["parent_id"])
    by_id = {account["account_id"]: account for account in accounts}
    by_name = {location["name"]: location for location in locations}
    if len(by_name) != len(locations):
        raise ValueError("The website CSV has repeated community names")
    proposals = {}
    investigations = []

    def add(row, kind, changes, before=None, guard=None, successor=None, survivor=None):
        operation = {
            "kind": kind, "account_id": row["crm_account_id"], "location": row["website_location"] or row["crm_account_name"],
            "changes": changes, "before": before or {}, "guard": guard or {},
        }
        if successor is not None:
            operation["successor"] = successor
        if survivor is not None:
            operation["survivor"] = survivor
        key = sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()[:24]
        proposals[key] = {**operation, "source": row, "id": key}

    for row in matches:
        location = by_name[row["website_location"]]
        if row["classification"] == "Missing in CRM":
            payload = new_account(location, parent["account_id"])
            if payload:
                lookalikes = [
                    f'{record["crm_account_name"]} ({record["crm_billing_address"]})'
                    for record in exceptions if record["website_location"] == location["name"]
                    and record["classification"] == "Rejected lookalike"
                ]
                evidence = row["evidence"]
                if lookalikes:
                    evidence += " Similar CRM records at a different address: " + ", ".join(lookalikes) + "."
                add({**row, "evidence": evidence}, "Create account", payload)
                continue

        flags = row["flags"].split(" | ") if row["flags"] else []
        account = by_id.get(row["crm_account_id"])
        if account and row["classification"] == "Needs fix":
            name_flag = "CRM name differs from website"
            parent_flag = next((flag for flag in ("Wrong parent", "Missing parent") if flag in flags), None)
            guard = guard_fields(account, "status")
            if parent_flag and protected_billing(account):
                successor = new_account(location, parent["account_id"])
                if successor:
                    billing = (f' Lifetime revenue is ${account["lifetime_revenue"]:,.2f} and outstanding A/R is '
                               f'${account["outstanding_ar"]:,.2f}. The billing SOP requires a successor account.')
                    add({**row, "evidence": row["evidence"] + billing}, "Change of ownership", {},
                        guard=guard_fields(account, *BILLING_FIELDS, "name", "parent_id", "status", "note", "care_type"),
                        successor=successor)
                    flags = [flag for flag in flags if flag not in {parent_flag, name_flag}]
            else:
                if name_flag in flags:
                    add(row, "Update name", {"name": location["name"]}, {"name": account["name"]}, guard)
                    flags.remove(name_flag)
                if parent_flag:
                    billing = (f' Lifetime revenue is ${account["lifetime_revenue"]:,.2f} and outstanding A/R is '
                               f'${account["outstanding_ar"]:,.2f}. The billing SOP allows direct re-parenting.')
                    add({**row, "evidence": row["evidence"] + billing}, "Update parent", {"parent_id": parent["account_id"]},
                        {"parent_id": account["parent_id"]}, {**guard, **{key: account[key] for key in BILLING_FIELDS}})
                    flags.remove(parent_flag)
        outstanding_flags = [flag for flag in flags if "other CRM record(s) at this site" not in flag]
        if outstanding_flags or row["classification"] in {"Needs review", "Missing in CRM"}:
            investigations.append({**row, "flags": " | ".join(outstanding_flags)})

    for row in exceptions:
        if row["classification"] == "Rejected lookalike":
            continue
        account = by_id[row["crm_account_id"]]
        if account["duplicate_of_account"] or account["chow_current_account"]:
            continue
        if row["classification"] == "CRM only under Bellhaven" and account["status"] == "Active":
            follow_up = "Absent from the current Bellhaven website. Verify operating status and ownership."
            if row["related_crm_account_id"]:
                follow_up += f' Possible successor {row["related_crm_account_id"]} shares the billing address.'
            if account["outstanding_ar"]:
                follow_up += f' Outstanding A/R is ${account["outstanding_ar"]:,.2f}. Review with billing.'
            add({**row, "evidence": follow_up}, "Flag for review",
                {"status": "Needs Review", "note": append_note(account, follow_up)},
                {"status": account["status"], "note": account["note"]},
                guard_fields(account, "name", "parent_id", *BILLING_FIELDS))
            continue
        if row["classification"] == "Likely duplicate":
            survivor = by_id[row["related_crm_account_id"]]
            primary = next(match for match in matches if match["crm_account_id"] == survivor["account_id"])
            if (account["status"] == survivor["status"] == "Active"
                    and account["care_type"] == survivor["care_type"] and same_billing_site(account, survivor)
                    and words(account["name"]) == words(survivor["name"])
                    and "Public phone agrees" in primary["evidence"]
                    and account["lifetime_revenue"] == account["outstanding_ar"] == 0):
                winner_id = survivor["account_id"]
                note = (f"Duplicate of {winner_id}. Both accounts have the same name, address, parent and care type. "
                        "The surviving account matches the published phone. Review linked contacts on the inactive account separately.")
                add({**row, "evidence": note + " The duplicate has no recorded revenue or outstanding A/R."},
                    "Mark duplicate", {"duplicate_of_account": winner_id, "status": "Inactive", "note": append_note(account, note)},
                    {"duplicate_of_account": account["duplicate_of_account"], "status": account["status"], "note": account["note"]},
                    guard_fields(account, "name", "parent_id", "phone", "care_type", *BILLING_FIELDS),
                    survivor=guard_fields(survivor, "account_id", "name", "parent_id", "phone", "status", "care_type"))
                continue
        investigations.append(row)
    return proposals, investigations


def decision_key(proposal):
    # Compare the intended change independently of the CRM snapshot and evidence text.
    kind = proposal["kind"]
    account = proposal["account_id"]
    changes = proposal["changes"]
    if kind in {"Update parent", "Change of ownership"}:
        destination = proposal.get("successor", changes)["parent_id"]
        return "Ownership", account, destination
    if kind == "Create account":
        return kind, changes["parent_id"], street_key(changes["billing_street"]), tuple(words(changes["billing_city"])), changes["billing_state"].upper()
    if kind == "Update name":
        return kind, account, tuple(words(changes["name"]))
    if kind == "Mark duplicate":
        return kind, account, changes["duplicate_of_account"]
    return kind, account, changes["status"]


class Review:
    def __init__(self, crm, database, sandbox):
        self.crm = crm
        self.database = database
        self.sandbox = sandbox
        self.csrf = token_urlsafe(32)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS review_decisions (
                sandbox TEXT NOT NULL, id TEXT NOT NULL, proposal TEXT NOT NULL,
                state TEXT NOT NULL, detail TEXT NOT NULL, decided_at TEXT NOT NULL,
                PRIMARY KEY (sandbox, id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS review_queue (
                sandbox TEXT PRIMARY KEY, started_ns INTEGER NOT NULL, snapshot TEXT NOT NULL
            )""")

    def publish_queue(self, proposals, investigations, location_count, account_count, started_ns):
        snapshot = {
            "proposals": proposals, "investigations": investigations,
            "location_count": location_count, "account_count": account_count,
        }
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            history = db.execute("SELECT id, proposal FROM review_decisions WHERE sandbox = ?", (self.sandbox,)).fetchall()
            snapshot["proposals"] = {proposal["id"]: proposal for proposal in self.pending(snapshot, history)}
            db.execute("""INSERT INTO review_queue (sandbox, started_ns, snapshot) VALUES (?, ?, ?)
                ON CONFLICT(sandbox) DO UPDATE SET started_ns = excluded.started_ns, snapshot = excluded.snapshot
                WHERE excluded.started_ns > review_queue.started_ns""",
                (self.sandbox, started_ns, json.dumps(snapshot)))

    def queue(self):
        with closing(sqlite3.connect(self.database)) as db:
            result = db.execute("SELECT snapshot FROM review_queue WHERE sandbox = ?", (self.sandbox,)).fetchone()
        return json.loads(result[0]) if result else None

    def pending(self, snapshot, history):
        decided = {decision_key(json.loads(row[1])) for row in history}
        return [proposal for proposal in snapshot["proposals"].values() if decision_key(proposal) not in decided]

    def decisions(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("""SELECT id, proposal, state, detail, decided_at FROM review_decisions
                WHERE sandbox = ? ORDER BY decided_at DESC, id""", (self.sandbox,)).fetchall()

    def decide(self, proposal_id, decision):
        if decision == "finish_link":
            with closing(sqlite3.connect(self.database)) as db, db:
                saved = db.execute("SELECT proposal FROM review_decisions WHERE sandbox = ? AND id = ? AND state = 'Link pending'",
                                   (self.sandbox, proposal_id)).fetchone()
                if not saved:
                    raise ValueError("This ownership link is not ready to retry.")
                proposal = json.loads(saved[0])
                if not db.execute("UPDATE review_decisions SET state = 'Applying' WHERE sandbox = ? AND id = ? AND state = 'Link pending'",
                                  (self.sandbox, proposal_id)).rowcount:
                    return
            self.save(proposal, *self.finish_ownership_link(proposal))
            return proposal_id
        proposal = self.queue()["proposals"].get(proposal_id)
        if not proposal or decision not in {"approve", "reject"}:
            raise ValueError("Choose an action for a pending proposal.")
        state = "Applying" if decision == "approve" else "Rejected"
        detail = "" if decision == "approve" else "Rejected by the reviewer."
        with closing(sqlite3.connect(self.database)) as db, db:
            # Lock before checking history so a stale browser tab cannot submit the same change twice.
            db.execute("BEGIN IMMEDIATE")
            key = decision_key(proposal)
            existing = db.execute("SELECT id, proposal FROM review_decisions WHERE sandbox = ? ORDER BY decided_at DESC",
                                  (self.sandbox,)).fetchall()
            for previous_id, previous_proposal in existing:
                if decision_key(json.loads(previous_proposal)) == key:
                    return previous_id
            db.execute("""INSERT INTO review_decisions
                (sandbox, id, proposal, state, detail, decided_at) VALUES (?, ?, ?, ?, ?, ?)""", (
                self.sandbox, proposal_id, json.dumps(proposal), state, detail, datetime.now(timezone.utc).isoformat(),
            ))
        if decision == "reject":
            return proposal_id

        self.save(proposal, *self.apply(proposal))
        return proposal_id

    def save(self, proposal, state, detail):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE review_decisions SET proposal = ?, state = ?, detail = ? WHERE sandbox = ? AND id = ?",
                       (json.dumps(proposal), state, detail, self.sandbox, proposal["id"]))

    def verify_parent(self, parent_id):
        parent = self.crm.request("GET", parent_id)
        if not fields_match(parent, {"account_id": parent_id, "name": PARENT_NAME, "parent_id": "", "status": "Active"}):
            raise ValueError("The proposed Bellhaven parent changed.")

    def check_for_existing(self, payload, old_id=""):
        location = {
            "name": payload["name"], "address": payload["billing_street"], "city": payload["billing_city"],
            "state": payload["billing_state"], "zip": payload["billing_zip"],
        }
        for current in self.crm.accounts():
            if current["account_id"] == old_id:
                continue
            same_name_city = words(location["name"]) == words(current["name"]) and all(
                words(location[key]) == words(current["billing_" + key]) for key in ("city", "state")
            )
            if same_name_city or location_evidence(location, current):
                raise ValueError(f'An account now matches this location ({current["account_id"]}). Review it before creating one.')

    def create_record(self, payload, checkpoint=None):
        result = self.crm.request("POST", payload=payload)
        data = result.get("data", result) if isinstance(result, dict) else {}
        account_id = data.get("account_id", "") if isinstance(data, dict) else ""
        if not account_id:
            exact = [row for row in self.crm.accounts() if fields_match(row, payload)]
            if len(exact) == 1:
                account_id = exact[0]["account_id"]
        if not account_id:
            raise ValueError("The CRM did not identify the account created by the request.")
        if checkpoint:
            checkpoint(account_id)
        if not fields_match(self.crm.request("GET", account_id), {**payload, "account_id": account_id}):
            raise ValueError(f"The approved values could not be verified on CRM account {account_id}.")
        return account_id

    def apply(self, proposal):
        account_id = proposal["account_id"]
        changes = proposal["changes"]
        successor = proposal.get("successor")
        try:
            destination = successor["parent_id"] if successor else changes.get("parent_id")
            if destination:
                self.verify_parent(destination)
            if account_id:
                current = self.crm.request("GET", account_id)
                expected = {**proposal["guard"], **proposal["before"], "account_id": account_id}
                if not fields_match(current, expected):
                    return "Blocked", "CRM fields changed after the proposal was prepared. Verify the current record before reconsidering the change."
                if current.get("duplicate_of_account") or current.get("chow_current_account"):
                    return "Blocked", "The CRM record now links to another account. Verify the relationship before editing."
                if destination and protected_billing(current) != bool(successor):
                    return "Blocked", "The current billing fields require a different ownership action."
                if successor:
                    self.check_for_existing(successor, old_id=account_id)
                if "survivor" in proposal:
                    survivor = self.crm.request("GET", changes["duplicate_of_account"])
                    if not fields_match(survivor, proposal["survivor"]) or survivor.get("duplicate_of_account") or survivor.get("chow_current_account"):
                        return "Blocked", "The proposed surviving account changed. Review the duplicate again."
            else:
                self.check_for_existing(changes)
        except ERRORS as error:
            return "Blocked", str(error)

        if successor:
            return self.create_successor(proposal)

        try:
            if account_id:
                self.crm.request("PATCH", account_id, changes)
                if not fields_match(self.crm.request("GET", account_id), {**changes, "account_id": account_id}):
                    return "Check CRM", f"The approved values could not be verified on CRM account {account_id}."
            else:
                account_id = self.create_record(changes)
            return "Applied", f"Verified in CRM on account {account_id}."
        except ERRORS as error:
            return "Check CRM", f"The write could not be verified. {error}. Check the CRM before retrying."

    def create_successor(self, proposal):
        def checkpoint(account_id):
            proposal["successor_id"] = account_id
            self.save(proposal, "Applying", f"Successor account {account_id} identified. The ownership link is still being applied.")

        try:
            self.create_record(proposal["successor"], checkpoint)
        except ERRORS as error:
            if proposal.get("successor_id"):
                return "Link pending", f'Successor {proposal["successor_id"]} needs verification. {error}'
            return "Check CRM", f"The successor creation could not be verified. {error} Check the CRM before creating another account."
        return self.finish_ownership_link(proposal)

    def finish_ownership_link(self, proposal):
        old_id = proposal["account_id"]
        new_id = proposal["successor_id"]
        try:
            if old_id == new_id or not fields_match(self.crm.request("GET", new_id), {**proposal["successor"], "account_id": new_id}):
                raise ValueError("The successor does not match the approved account values.")
            old = self.crm.request("GET", old_id)
            expected = {**proposal["guard"], "account_id": old_id}
            if not fields_match(old, expected) or old.get("duplicate_of_account"):
                raise ValueError("The original account changed. Review its billing and ownership before linking.")
            link = old.get("chow_current_account")
            if link and link != new_id:
                raise ValueError("The original account already points to a different successor.")
            if not link:
                self.crm.request("PATCH", old_id, {"chow_current_account": new_id})
                old = self.crm.request("GET", old_id)
            if not fields_match(old, {**expected, "chow_current_account": new_id}):
                raise ValueError("The original account and its ownership link could not be verified.")
            return "Applied", f"Created {new_id} and verified the ownership link on {old_id}."
        except ERRORS as error:
            return "Link pending", f"Successor {new_id}, original {old_id}. {error}"


def fields_match(account, expected):
    return isinstance(account, dict) and all(account.get(field) == value for field, value in expected.items())


class ReviewHandler(BaseHTTPRequestHandler):
    def respond(self, status, body=b"", content_type="text/plain; charset=utf-8", location=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host") != self.server.host_header:
            return self.respond(403, b"Open the app using its local URL.")
        url = urlsplit(self.path)
        if url.path == "/style.css":
            return self.respond(200, (ROOT / "bellhaven_review.css").read_bytes(), "text/css; charset=utf-8")
        if url.path != "/":
            return self.respond(404, b"Page not found.")
        view = parse_qs(url.query).get("view", ["pending"])[0]
        return self.respond(200, render(self.server.review, view).encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/decision":
            return self.respond(404, b"Page not found.")
        if self.headers.get("Host") != self.server.host_header or self.headers.get("Origin") != self.server.origin:
            return self.respond(403, b"Open the app using its local URL.")
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return self.respond(415, b"Submit the review form.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                raise ValueError
            form = parse_qs(self.rfile.read(length).decode(), strict_parsing=True, max_num_fields=3)
            csrf, proposal_id, decision = (form[field][0] for field in ("csrf", "proposal", "decision"))
        except (KeyError, ValueError):
            return self.respond(400, b"Invalid review form.")
        if not compare_digest(csrf.encode(), self.server.review.csrf.encode()):
            return self.respond(403, b"Reload the review page before deciding.")
        try:
            receipt = self.server.review.decide(proposal_id, decision)
        except ValueError as error:
            return self.respond(400, str(error).encode())
        return self.respond(303, location=f"/?view=history#{receipt}")


def make_server(review, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), ReviewHandler)
    server.review = review
    server.host_header = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.host_header
    return server


def refresh_queue(review, locations_path=None):
    started_ns = time_ns()
    if locations_path:
        locations = load_locations(locations_path)
    else:
        locations = [dict(zip(CSV_FIELDS, row)) for row in collect_locations()]
    accounts = review.crm.accounts()
    proposals, investigations = build_queue(locations, accounts)
    review.publish_queue(proposals, investigations, len(locations), len(accounts), started_ns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--locations", type=Path, help="Use a website CSV instead of scraping during a refresh")
    parser.add_argument("--database", type=Path, default=ROOT / ".bellhaven_review.sqlite3")
    parser.add_argument("--refresh-only", action="store_true", help="Refresh the saved queue without starting the app")
    parser.add_argument("--token-file", type=Path, help="Read the CRM token from a private file for unattended runs")
    args = parser.parse_args()
    try:
        token = os.environ.get("BELLHAVEN_CRM_TOKEN", "").strip()
        if not token and args.token_file:
            token = args.token_file.read_text(encoding="utf-8").strip()
        if not token and not args.refresh_only:
            token = getpass("CRM API token: ").strip()
        if not token:
            raise ValueError("Set BELLHAVEN_CRM_TOKEN or pass --token-file for an unattended refresh")
        crm = CRM(token)
        review = Review(crm, args.database, sha256(token.encode()).hexdigest())
        if args.refresh_only or args.locations or review.queue() is None:
            refresh_queue(review, args.locations)
        if args.refresh_only:
            snapshot = review.queue()
            pending = review.pending(snapshot, review.decisions())
            print(f"Refreshed {snapshot['location_count']} website locations and {snapshot['account_count']} CRM accounts. "
                  f"{len(pending)} pending proposals, {len(snapshot['investigations'])} investigation entries.")
            return
        with make_server(review, args.port) as server:
            print(f"Bellhaven review · {server.origin}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return
    except (*ERRORS, sqlite3.Error, EOFError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
