"""Approval workflow UX — email notification + tokenized review page.

When a send run pauses at the approval gate, ``activity_request_approval``
emails the approvers a run summary plus a link to the review page:

  GET  /api/review/{instance_id}?token=…    — renders the prepared email
       previews (from blob storage) with Approve / Reject buttons
  POST /api/decision/{instance_id}          — raises the ``approval`` event

Both endpoints are ``ANONYMOUS`` + verified against a **per-run one-time
token**: the orchestrator generates it with ``context.new_uuid()`` and
stores its SHA-256 in the orchestration's ``custom_status``. No function
key ever appears in an email, and the token dies with the run.

Scanner safety: the emailed link is a GET that only *reads*. Outlook
SafeLinks and other mail scanners prefetch GET URLs — if approve were a
GET link, a scanner could silently approve a send run. Decisions are
therefore POST-only forms on the review page.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
from typing import Any

log = logging.getLogger("calorch.durable.approval")


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------
def token_hash(token: str) -> str:
    """SHA-256 hex of the one-time approval token (stored in custom_status)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(custom_status: Any, token: str) -> bool:
    """Timing-safe check of *token* against the orchestration custom_status."""
    if not token or not isinstance(custom_status, dict):
        return False
    expected = custom_status.get("token_sha256")
    if not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(expected, token_hash(token))


def approval_state(custom_status: Any) -> str:
    """The gate state recorded in custom_status ('' when absent)."""
    if isinstance(custom_status, dict):
        return str(custom_status.get("approval", ""))
    return ""


# ---------------------------------------------------------------------------
# Approval-request email (sent by activity_request_approval)
# ---------------------------------------------------------------------------
def build_approval_email(
    *,
    run_id: str,
    prepared: list[dict[str, Any]],
    review_url: str,
    timeout_hours: float,
) -> tuple[str, str]:
    """Return ``(subject, html_body)`` for the approval-request email.

    ``prepared`` is the slim summary the orchestrator carries:
    ``[{event_id, subject, to}, …]``. All event-derived values are escaped.
    """
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(p.get('event_id', '')))}</td>"
        f"<td>{html.escape(str(p.get('subject', '')))}</td>"
        f"<td>{html.escape(', '.join(p.get('to', []) or []))}</td>"
        "</tr>"
        for p in prepared
    )
    safe_url = html.escape(review_url, quote=True)
    body = f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif">
<h2>calorch run {html.escape(run_id)} is waiting for approval</h2>
<p>{len(prepared)} research email(s) are prepared and will be sent on approval.
The run auto-rejects in {timeout_hours:g} hours if no decision is made.</p>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>Event</th><th>Subject</th><th>Recipients</th></tr>
{rows}
</table>
<p><a href="{safe_url}">Review the prepared emails and approve or reject</a></p>
<p style="color:#6b7280;font-size:12px">This link is read-only; the decision
buttons are on the review page. The link contains a one-time token valid only
for this run.</p>
</body></html>"""
    subject = f"[calorch] Approval needed — {len(prepared)} email(s) ready (run {run_id})"
    return subject, body


# ---------------------------------------------------------------------------
# Review page (rendered by GET /api/review/{instance_id})
# ---------------------------------------------------------------------------
def load_previews(run_id: str) -> list[tuple[str, str]]:
    """Load the run's per-event email previews from blob storage.

    Returns ``[(event_id, preview_html), …]``. Falls back to an empty list
    when blob storage is not configured (the review page then shows the
    decision buttons without inline previews).
    """
    from calorch.blob_store import _safe_blob_name, make_blob_store
    from calorch.config import get_settings

    s = get_settings()
    store = make_blob_store(
        connection_string=s.azure_storage_connection_string,
        account_url=s.azure_storage_account_url,
        local_root=s.blob_local_root,
        input_container=s.blob_input_container,
        output_container=s.blob_output_container,
    )
    prefix = f"outputs/{_safe_blob_name(run_id)}/"
    previews: list[tuple[str, str]] = []
    try:
        for name in store.list_blobs(store.output_container, prefix):
            # Per-event previews look like outputs/{run}/{event}/{event}.html;
            # skip DOCX, _analysis.json and the run briefing.
            parts = name.split("/")
            if len(parts) == 4 and parts[3] == f"{parts[2]}.html":
                data = store.download_bytes(store.output_container, name)
                if data:
                    previews.append((parts[2], data.decode("utf-8", errors="replace")))
    except Exception as exc:  # noqa: BLE001 - previews are best-effort
        log.warning("loading previews for %s failed: %s", run_id, exc)
    return sorted(previews)


def render_review_page(
    *,
    instance_id: str,
    token: str,
    previews: list[tuple[str, str]],
    state: str,
    decision_url: str,
) -> str:
    """Render the approval review page.

    Decision buttons are POST forms (never GET links — see module docstring)
    and only shown while the gate is still pending. Preview HTML is embedded
    via sandboxed ``iframe srcdoc`` so preview content cannot script this page.
    """
    if state == "pending":
        controls = f"""<form method="post" action="{html.escape(decision_url, quote=True)}">
<input type="hidden" name="token" value="{html.escape(token, quote=True)}">
<button name="decision" value="approve" style="background:#16a34a;color:#fff;padding:10px 24px;border:0;border-radius:6px;font-size:15px;margin-right:12px">Approve &amp; send</button>
<button name="decision" value="reject" style="background:#dc2626;color:#fff;padding:10px 24px;border:0;border-radius:6px;font-size:15px">Reject</button>
</form>"""
    else:
        controls = (
            f"<p><strong>This run is no longer awaiting approval"
            f"{f' (state: {html.escape(state)})' if state else ''}.</strong></p>"
        )

    if previews:
        frames = "".join(
            f"<h3>Event {html.escape(ev_id)}</h3>"
            f'<iframe srcdoc="{html.escape(body, quote=True)}" sandbox '
            'style="width:100%;height:420px;border:1px solid #e5e7eb;border-radius:6px"></iframe>'
            for ev_id, body in previews
        )
    else:
        frames = "<p>(No previews available in blob storage — review the run artifacts directly.)</p>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>calorch approval — {html.escape(instance_id)}</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;max-width:860px;margin:24px auto;padding:0 16px">
<h1>Approval review — run {html.escape(instance_id)}</h1>
<p>{len(previews)} prepared email(s). Approving sends them via Microsoft Graph;
rejecting completes the run without sending.</p>
{controls}
{frames}
</body></html>"""


def render_decision_page(instance_id: str, approved: bool) -> str:
    """Confirmation page returned after a decision is recorded."""
    verdict = "approved — the emails are being sent" if approved else "rejected — nothing will be sent"
    return f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;margin:48px auto">
<h1>Decision recorded</h1>
<p>Run {html.escape(instance_id)} was <strong>{verdict}</strong>.</p>
<p>You can close this page.</p>
</body></html>"""
