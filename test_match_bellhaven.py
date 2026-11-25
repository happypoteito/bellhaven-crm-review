from unittest import TestCase

from match_bellhaven import PARENT_NAME, match_locations


class BellhavenMatchingTest(TestCase):
    def test_matches_by_site_and_leaves_equally_plausible_records_open(self):
        def location(name, address):
            return {
                "name": name, "address": address, "city": "Lakeview", "state": "OH",
                "zip": "12345", "care_offerings": "Memory Support",
            }

        def account(id_, name, street, *, parent="B", city="Lakeview", phone=""):
            return {
                "account_id": id_, "name": name, "parent_id": parent,
                "parent_name": PARENT_NAME if parent == "B" else "Old Operator",
                "billing_street": street, "billing_city": city, "billing_state": "OH",
                "billing_zip": "12345", "care_type": "Memory Care", "status": "Active",
                "phone": phone, "lifetime_revenue": 1000, "outstanding_ar": 0,
            }

        locations = [
            location("Bellhaven Court", "10 NW Main St"),
            location("Bellhaven of Lakeview", "20 Lake Pike"),
            location("Bellhaven Heights", "30 Elm Rd"),
            location("Bellhaven House", "40 Maple Dr"),
        ]
        accounts = [
            account("B", PARENT_NAME, "", parent=""),
            account("A1", "Bellhaven Court", "10 Northwest Main Street", phone="(111) 111-1111"),
            account("A2", "Bellhaven Court", "10 NW Main St", phone="222-222-2222"),
            account("A3", "Lakeview Senior Campus", "20 Lake Pk", parent="H"),
            account("A4", "Lakeview Retirement Home", "20 Lake Pike", parent="C"),
            account("A5", "Bellhaven Heights", "30 Elm Rd", parent="H", city="Ridgeview"),
            account("A6", "Old Maple Home", "40 Maple Drive", parent="H"),
            account("A7", "Bellhaven of Ridgeview", "50 Main St", city="Ridgeview"),
            account("A8", "New Ridgeview Home", "50 Main Street", parent="C", city="Ridgeview"),
        ]
        matches, exceptions = match_locations(locations, accounts, lambda _: "111-111-1111")

        self.assertEqual([(row["classification"], row["crm_account_id"]) for row in matches], [
            ("Needs fix", "A1"), ("Needs review", ""), ("Missing in CRM", ""), ("Needs fix", "A6"),
        ])
        self.assertIn("Wrong parent", matches[3]["flags"])
        self.assertEqual({(row["classification"], row["crm_account_id"]) for row in exceptions}, {
            ("Likely duplicate", "A2"), ("Unresolved same-site candidate", "A3"),
            ("Unresolved same-site candidate", "A4"), ("Rejected lookalike", "A5"),
            ("CRM only under Bellhaven", "A7"), ("Possible successor", "A8"),
        })
