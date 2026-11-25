from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import time_ns
from unittest import TestCase
from urllib.parse import urlencode

from match_bellhaven import PARENT_NAME, match_locations
from review_bellhaven import Review, build_queue, make_server


def account(id_, name, street="", *, parent="B", revenue=0, ar=0, phone="", note=""):
    return {
        "account_id": id_, "name": name, "parent_id": parent,
        "parent_name": PARENT_NAME if parent == "B" else "Former operator" if parent else "",
        "billing_street": street, "billing_city": "Dover" if street else "", "billing_state": "OH" if street else "",
        "billing_zip": "44622" if street else "", "care_type": "Assisted Living" if street else "",
        "status": "Active", "lifetime_revenue": revenue, "outstanding_ar": ar, "phone": phone, "note": note,
        "duplicate_of_account": "", "chow_current_account": "",
    }


def location(name, street):
    return {"name": name, "address": street, "city": "Dover", "state": "OH", "zip": "44622",
            "care_offerings": "Assisted Living"}


class FakeCRM:
    def __init__(self, rows):
        self.rows = {row["account_id"]: row for row in rows}
        self.writes = []
        self.fail_link = False
        self.before_write = lambda: None

    def accounts(self):
        return [dict(row) for row in self.rows.values()]

    def request(self, method, account_id=None, payload=None):
        if method == "GET":
            return dict(self.rows[account_id])
        self.before_write()
        self.writes.append((method, account_id, dict(payload)))
        if method == "POST":
            account_id = "N" + str(len(self.writes))
            self.rows[account_id] = {**account(account_id, payload["name"], payload["billing_street"]), **payload}
        elif "chow_current_account" in payload and self.fail_link:
            raise TimeoutError("CRM did not confirm link")
        else:
            self.rows[account_id].update(payload)
        return {"account_id": account_id}


class ReviewTest(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "review.db"
        self.locations = [
            location("Bellhaven Paid", "10 Main St"), location("Bellhaven Owed", "20 Main St"),
            location("Bellhaven New", "30 Main St"), location("Bellhaven Duplicate", "40 Main St"),
            location("Bellhaven No Revenue", "50 Main St"),
        ]
        self.crm = FakeCRM([
            account("B", PARENT_NAME, parent=""),
            account("P", "Bellhaven Paid", "10 Main St", parent="Old", revenue=500),
            account("O", "Older Name", "20 Main St", parent="Old", revenue=500, ar=20, note="Keep this."),
            account("D1", "Bellhaven Duplicate", "40 Main St", phone="3333333333"),
            account("D2", "Bellhaven Duplicate", "40 Main Street", phone="8888888888", note="Keep this, too."),
            account("Z", "Bellhaven No Revenue", "50 Main St", parent="Old", ar=10),
            account("S", "Bellhaven Absent", "60 Main St", revenue=300, ar=25),
        ])
        self.review = self.new_review()

    def new_review(self):
        accounts = self.crm.accounts()
        proposals, investigations = build_queue(
            self.locations, accounts, lambda a, b: match_locations(a, b, lambda _: "3333333333"),
        )
        review = Review(self.crm, self.database, "fake")
        review.publish_queue(proposals, investigations, len(self.locations), len(accounts), time_ns())
        return review

    def proposal(self, kind, account_id):
        return next(row for row in self.review.queue()["proposals"].values()
                    if row["kind"] == kind and row["account_id"] == account_id)

    def test_billing_rule_and_ownership_write_preserve_original_account(self):
        for account_id in ("P", "Z"):
            direct = self.proposal("Update parent", account_id)
            self.review.decide(direct["id"], "approve")
            self.assertEqual(self.crm.writes[-1], ("PATCH", account_id, {"parent_id": "B"}))
        chow = self.proposal("Change of ownership", "O")
        self.assertFalse(any(p["kind"] == "Update name" and p["account_id"] == "O"
                             for p in self.review.queue()["proposals"].values()))
        original = dict(self.crm.rows["O"])
        self.crm.before_write = lambda: self.assertTrue(any(
            key == chow["id"] and state == "Applying" for key, _, state, _, _ in self.review.decisions()
        ))
        self.review.decide(chow["id"], "approve")
        new_id = self.crm.rows["O"]["chow_current_account"]
        self.assertTrue(new_id)
        self.assertEqual(self.crm.rows["O"], {**original, "chow_current_account": new_id})
        self.assertEqual(self.crm.writes[-2:], [("POST", None, chow["successor"]), ("PATCH", "O", {"chow_current_account": new_id})])
        self.assertEqual(self.crm.rows[new_id]["name"], "Bellhaven Owed")
        self.assertEqual(self.crm.rows[new_id]["parent_id"], "B")
        self.assertEqual(self.crm.rows[new_id]["lifetime_revenue"], 0)

    def test_changed_billing_blocks_direct_reparent_and_existing_site_blocks_creation(self):
        direct = self.proposal("Update parent", "P")
        self.crm.rows["P"]["outstanding_ar"] = 1
        self.review.decide(direct["id"], "approve")
        create = self.proposal("Create account", "")
        self.crm.rows["X"] = account("X", "An account just created for the site", "30 Main St")
        self.review.decide(create["id"], "approve")
        self.assertEqual(self.crm.writes, [])
        self.assertEqual({state for _, _, state, _, _ in self.review.decisions()}, {"Blocked"})

    def test_failed_ownership_link_retries_without_creating_another_account(self):
        chow = self.proposal("Change of ownership", "O")
        self.crm.fail_link = True
        self.review.decide(chow["id"], "approve")
        self.assertEqual(self.crm.rows["O"]["chow_current_account"], "")
        self.assertEqual(self.review.decisions()[0][2], "Link pending")
        self.review.decide(chow["id"], "approve")
        self.assertEqual([method for method, _, _ in self.crm.writes], ["POST", "PATCH"])
        self.crm.fail_link = False
        self.review = self.new_review()
        self.review.decide(chow["id"], "finish_link")
        self.assertEqual(self.crm.rows["O"]["chow_current_account"], "N1")
        self.assertEqual([method for method, _, _ in self.crm.writes], ["POST", "PATCH", "PATCH"])
        self.assertEqual(self.review.decisions()[0][2], "Applied")

    def test_duplicate_and_stale_status_proposals_use_crm_conventions(self):
        duplicate = self.proposal("Mark duplicate", "D2")
        stale = self.proposal("Flag for review", "S")
        self.review.decide(duplicate["id"], "approve")
        self.review.decide(stale["id"], "approve")
        self.assertEqual(self.crm.rows["D2"]["duplicate_of_account"], "D1")
        self.assertEqual(self.crm.rows["D2"]["status"], "Inactive")
        self.assertTrue(self.crm.rows["D2"]["note"].startswith("Keep this, too."))
        self.assertEqual(self.crm.rows["D1"]["status"], "Active")
        self.assertEqual(self.crm.rows["S"]["status"], "Needs Review")
        self.assertEqual(self.crm.rows["S"]["parent_id"], "B")
        self.assertEqual(self.crm.rows["S"]["outstanding_ar"], 25)

    def test_daily_refresh_preserves_decisions_even_after_evidence_changes(self):
        rejected = self.proposal("Update parent", "P")
        approved = self.proposal("Update parent", "Z")
        self.review.decide(rejected["id"], "reject")
        self.review.decide(approved["id"], "approve")
        writes = list(self.crm.writes)
        self.crm.rows["P"]["outstanding_ar"] = 10  # The same ownership change would now require a CHOW.
        self.locations.append(location("Bellhaven New Addition", "70 Main St"))

        first = self.new_review().queue()
        second_review = self.new_review()
        second = second_review.queue()
        self.assertEqual(first, second)
        self.assertFalse(any(row["account_id"] in {"P", "Z"} for row in second["proposals"].values()))
        self.assertTrue(any(row["location"] == "Bellhaven New Addition" for row in second["proposals"].values()))
        with self.assertRaisesRegex(ValueError, "pending proposal"):
            second_review.decide(rejected["id"], "approve")
        self.assertEqual(self.crm.writes, writes)
        self.assertEqual({state for _, _, state, _, _ in second_review.decisions()}, {"Applied", "Rejected"})

        second_review.publish_queue({}, [], 0, 0, 0)
        self.assertEqual(second_review.queue(), second)

    def test_http_requires_approval_from_the_local_form(self):
        create = self.proposal("Create account", "")
        reject = self.proposal("Update parent", "P")
        with make_server(self.review, 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def submit(key, decision, *, csrf=None, origin=None):
                    fields = {"proposal": key, "decision": decision, "csrf": csrf or self.review.csrf}
                    return get("POST", "/decision", urlencode(fields), {
                        "Content-Type": "application/x-www-form-urlencoded", "Origin": origin or server.origin,
                    })[0]

                def get(method, path, body=None, headers=None):
                    with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as connection:
                        connection.request(method, path, body=body, headers=headers or {})
                        response = connection.getresponse()
                        return response.status, response.read().decode()

                status, page = get("GET", "/")
                self.assertEqual(status, 200)
                self.assertIn("Supporting evidence", page)
                self.assertIn("Create the successor account", page)
                self.assertEqual(get("GET", "/decision")[0], 404)
                self.assertEqual(submit(create["id"], "approve", origin="https://other.example"), 403)
                self.assertEqual(submit(create["id"], "approve", csrf="wrong"), 403)
                self.assertEqual(submit(reject["id"], "reject"), 303)
                self.assertEqual(submit(reject["id"], "approve"), 303)
                self.assertEqual(self.crm.writes, [])
                self.assertEqual(submit(create["id"], "approve"), 303)
                self.assertEqual(submit(create["id"], "approve"), 303)
                self.assertEqual(self.crm.writes, [("POST", None, create["changes"])])
                self.assertIn("Verified in CRM on account N1", get("GET", "/?view=history")[1])
            finally:
                server.shutdown()
                thread.join(timeout=3)
