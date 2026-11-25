# Bellhaven CRM Review

An approval-gated workflow for reconciling the public community directory of a fictional senior living operator with its CRM sandbox. The daily job reads the website and CRM. A reviewer approves each CRM write in the local app.

## Run locally

Use Python 3.9 or later. The scripts use the standard library. Live CRM features require a token for the Bellhaven exercise sandbox.

```sh
python3 review_bellhaven.py
```

Enter the token at the hidden terminal prompt, then open [the local review queue](http://127.0.0.1:8765/). On first launch the app collects the website and CRM records. Later launches open the last completed refresh. Decisions and the current queue are stored in a local SQLite database.

| Command | Result |
|---|---|
| `python3 scrape_bellhaven.py` | Export community names containing the whole word `Bellhaven` to `bellhaven_locations.csv`. |
| `python3 match_bellhaven.py` | Compare the location CSV with all CRM accounts and export the matches and exceptions. |
| `python3 -B -m unittest -v` | Exercise scraping, matching, approvals, billing rules, and repeat refreshes with test data. |

The checked-in [location CSV](bellhaven_locations.csv) uses the public website. The [match report](examples/bellhaven_crm_matches.csv) and [exceptions report](examples/bellhaven_crm_exceptions.csv) in `examples/` use synthetic CRM records.

## Daily refresh

The [sample cron entry](bellhaven_review.cron) runs at 6:00 a.m. local time. Set the checkout and Python paths for your machine and provide the token through `BELLHAVEN_CRM_TOKEN` or `--token-file`. If using a file, keep it outside the checkout with file mode `600`.

```sh
python3 review_bellhaven.py --refresh-only --token-file "$HOME/.config/bellhaven/crm-token"
```

The refresh uses the same SQLite database as the review app. Run it twice and reload the app to verify that the same pending proposals appear and decision history is preserved. The read-only refresh suppresses previously decided changes by the correction they represent, even if the supporting evidence later changes. A failed refresh leaves the last completed queue available.

See the [review writeup](BELLHAVEN_REVIEW.md) for matching criteria, billing safeguards, and what happens when an approved write needs follow-up.
