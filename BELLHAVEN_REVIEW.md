# Bellhaven CRM review writeup

The scraper follows the community directory pagination and checks public navigation pages for additional location links. It reads each community’s name, address and care offerings from its detail page. A location enters the export when its published name contains `Bellhaven` as a whole word, ignoring case.

## Matching and evidence

The matcher reads every page of CRM accounts so that old names and wrong parents can still be found. It uses normalized street, city and state to identify a physical site. The account name and locality help with billing-only addresses, such as a PO Box. If several accounts qualify, a matching published phone or a unique current-name account under Bellhaven can identify the primary match. Unresolved clusters remain visible for investigation.

The app shows field-level name or parent edits and proposed accounts for unmatched sites. The card gives the before and proposed values and the evidence. Billing-address mismatches and care offerings that exceed the CRM’s single care-type field stay available for investigation.

## Ownership and billing

| Existing account | Approved action |
|---|---|
| Revenue history and positive outstanding A/R | Create an active account under the correct parent. Set `chow_current_account` on the original to the verified new ID. |
| No revenue history or no outstanding A/R | Change `parent_id` on the existing account. |

For a change of ownership (CHOW), the link is the only field submitted for the original account. The original retains its parent, status, note, and billing history. The app checks the billing fields again immediately before any ownership write.

Creation and linking are separate CRM requests. When the successor ID is known and the link fails, decision history records both IDs and offers **Retry link only**. The app verifies the successor before retrying the link. If creation can’t be verified, check the CRM before attempting another creation.

## Duplicate and stale records

- **Duplicates.** An approved duplicate change sets `duplicate_of_account` on the losing copy, marks it **Inactive**, and appends an explanatory note. Contacts remain on their current records for separate review. The site address establishes where the accounts belong, and corroborating evidence helps choose the survivor.
- **Stale records.** An account under Bellhaven that is absent from the current website can be proposed for **Needs Review** with a note. The account’s operator and status need confirmation before an ownership change.
- **Legacy candidates.** Same-address records under different operators can represent different periods. The app surfaces these for investigation when it lacks site-specific ownership or account history.

The app stores each approval or rejection before taking action. It verifies approved CRM writes by reading back the record. The daily refresh publishes a completed queue atomically and retains the existing decision history. Tests cover approval gating, duplicate marking, billing safeguards, partial CHOW recovery, and repeated refreshes.
