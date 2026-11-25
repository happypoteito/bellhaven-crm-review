"""Render the Bellhaven review queue as a server-side HTML page."""

from datetime import datetime
from html import escape
import json

from match_bellhaven import BASE_URL, PARENT_NAME


FIELD_LABELS = {
    "name": "Account name", "parent_id": "Parent account", "billing_street": "Billing street",
    "billing_city": "Billing city", "billing_state": "Billing state", "billing_zip": "Billing ZIP",
    "care_type": "Care type", "status": "Status", "note": "CRM note",
    "duplicate_of_account": "Duplicate of account", "chow_current_account": "Current account link",
}


def change_table(proposal, changes, before_values):
    rows = []
    for field, value in changes.items():
        before = before_values.get(field) or "—"
        if field == "parent_id":
            if before_values.get(field):
                before = f'{proposal["source"]["crm_parent"]} ({before})'
            elif field in before_values:
                before = "No parent"
            value = f"{PARENT_NAME} ({value})"
        rows.append(f'<tr><th scope="row">{FIELD_LABELS[field]}</th>'
                    f'<td>{escape(before)}</td><td>{escape(value)}</td></tr>')
    return ('<table><thead><tr><th scope="col">Field</th><th scope="col">Before</th>'
            '<th scope="col">Proposed</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


def decision_form(csrf, identifier, buttons):
    return f'''<form action="/decision" method="post">
      <input type="hidden" name="csrf" value="{escape(csrf)}">
      <input type="hidden" name="proposal" value="{identifier}">{buttons}</form>'''


def proposal_card(proposal, csrf, decision=None):
    source = proposal["source"]
    identifier = escape(proposal["id"])
    name = escape(proposal["location"])
    kind = escape(proposal["kind"])
    crm = f'CRM account {escape(proposal["account_id"])}' if proposal["account_id"] else "New CRM account"
    evidence = escape(source["evidence"])
    pairs = [(label, source.get(key)) for label, key in (
        ("Website address", "website_address"), ("Website care", "website_care_offerings"),
        ("CRM billing address", "crm_billing_address"), ("CRM parent at review", "crm_parent"),
    )]
    sources = "".join(f'<div><dt>{label}</dt><dd>{escape(value)}</dd></div>' for label, value in pairs if value)
    if "successor" in proposal:
        new_values = change_table(proposal, proposal["successor"], {})
        link_values = change_table(proposal, {"chow_current_account": proposal.get("successor_id", "ID of the new account")}, {})
        comparison = (f'<h3 class="step">1. Create the successor account</h3><div class="comparison">{new_values}</div>'
                      f'<h3 class="step">2. Link the original account</h3><div class="comparison">{link_values}</div>')
    else:
        comparison = f'<div class="comparison">{change_table(proposal, proposal["changes"], proposal["before"])}</div>'
    if decision:
        state, detail, decided_at = decision
        time = datetime.fromisoformat(decided_at).astimezone().strftime("%b %d, %Y at %I:%M %p")
        if state == "Applying":
            state = "Approval recorded"
            detail = (detail + " Check the CRM if this operation was interrupted.").strip()
        state_class = {"Applied": "success", "Rejected": "neutral"}.get(state, "warning")
        retry = ""
        if state == "Link pending":
            retry = decision_form(csrf, identifier, '<button type="submit" name="decision" value="finish_link" class="primary">Retry link only</button>')
        footer = f'<footer><span class="badge {state_class}">{escape(state)}</span>' \
                 f'<div><div>{escape(detail)}</div><time datetime="{escape(decided_at)}">{escape(time)}</time></div>{retry}</footer>'
    else:
        buttons = f'''<button type="submit" name="decision" value="reject" aria-label="Reject {kind} for {name}">Reject</button>
          <button type="submit" name="decision" value="approve" class="primary"
            aria-label="Approve and apply {kind} for {name}">Approve &amp; apply</button>'''
        footer = f'<footer class="actions">{decision_form(csrf, identifier, buttons)}</footer>'

    return f'''<article class="card" id="{identifier}">
      <header><div><h2>{name}</h2><p class="metadata">{crm}</p></div><span class="badge">{kind}</span></header>
      {comparison}<div class="evidence"><h3>Supporting evidence</h3><p>{evidence}</p><dl>{sources}</dl></div>
      {footer}</article>'''


def investigation_card(record):
    account_name = record["crm_account_name"]
    location = record["website_location"]
    key = escape(record["crm_account_id"])
    category = escape(record["classification"] if record["classification"] != "Needs fix" else "Needs investigation")
    revenue = record.get("outstanding_ar", 0)
    balance = f'<div><dt>Outstanding A/R</dt><dd>${revenue:,.2f}</dd></div>' if revenue else ""
    flag = f'<p class="issue">{escape(record["flags"])}</p>' if record["flags"] else ""
    crm = f'<div><dt>CRM record</dt><dd>{escape(account_name)} · {key}</dd></div>' if account_name else ""
    address = (f'<div><dt>CRM billing address</dt><dd>{escape(record["crm_billing_address"])}</dd></div>'
               if record["crm_billing_address"] else "")
    website = (f'<div><dt>Website address</dt><dd>{escape(record["website_address"])}</dd></div>'
               if record["website_address"] else "")
    parent = f'<div><dt>CRM parent</dt><dd>{escape(record["crm_parent"])}</dd></div>' if record["crm_parent"] else ""
    care = (f'<div><dt>Website care</dt><dd>{escape(record["website_care_offerings"])}</dd></div>'
            if record["website_care_offerings"] else "")
    related = record.get("related_crm_account_id")
    link = f'<div><dt>Related CRM account</dt><dd>{escape(related)}</dd></div>' if related else ""
    next_step = f'<p><strong>Follow-up.</strong> {escape(record["review_action"])}</p>' if record["review_action"] else ""
    return f'''<article class="card investigation"><header><h2>{escape(location or account_name)}</h2>
      <span class="badge warning">{category}</span></header>
      <div class="evidence">{flag}<p>{escape(record["evidence"])}</p>
      <dl>{crm}{parent}{website}{address}{care}{link}{balance}</dl>{next_step}</div></article>'''


def render(review, view):
    snapshot = review.queue()
    history = review.decisions()
    pending = review.pending(snapshot, history)
    investigations = snapshot["investigations"]
    tabs = {
        "pending": ("Pending", len(pending)), "investigate": ("Needs investigation", len(investigations)),
        "history": ("Decision history", len(history)),
    }
    if view not in tabs:
        view = "pending"
    if view == "history":
        cards = [proposal_card(json.loads(proposal), review.csrf, (state, detail, time))
                 for _, proposal, state, detail, time in history]
        empty = "No decisions recorded."
    elif view == "investigate":
        cards = [investigation_card(record) for record in investigations]
        empty = "No records need investigation."
    else:
        cards = [proposal_card(proposal, review.csrf) for proposal in pending]
        empty = "No proposals awaiting review."
    navigation = "".join(
        f'<a href="/?view={key}"' + (' aria-current="page"' if key == view else '')
        + f'>{label}<span>{count}</span></a>' for key, (label, count) in tabs.items()
    )
    content = "".join(cards) if cards else f'<p class="empty">{empty}</p>'
    instruction = {
        "pending": "Review each field and its evidence. Approval applies the displayed change to the sandbox CRM.",
        "investigate": "These records need more evidence before a CRM change can be proposed.",
        "history": "Review decisions and their CRM verification results.",
    }[view]
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Bellhaven CRM Review</title><link rel="stylesheet" href="/style.css"></head>
      <body><header class="topbar"><div class="shell"><a class="brand" href="/">Bellhaven <span>CRM review</span></a>
      <a class="source-link" href="{BASE_URL}/communities">Website directory</a></div></header>
      <main class="shell"><div class="intro"><h1>Review queue</h1>
      <p>{snapshot["location_count"]} website locations <span aria-hidden="true">·</span> {snapshot["account_count"]} CRM accounts</p></div>
      <nav aria-label="Review status">{navigation}</nav><p class="instruction">{instruction}</p>
      <div class="queue">{content}</div></main></body></html>'''
