"""Tests for the approval UX — token auth, email, review page, decision flow."""
from __future__ import annotations

from types import SimpleNamespace

from calorch.durable.approval import (
    approval_state,
    build_approval_email,
    render_decision_page,
    render_review_page,
    token_hash,
    verify_token,
)

TOKEN = "00000000-0000-0000-0000-000000000001"
PENDING = {"approval": "pending", "token_sha256": token_hash(TOKEN)}


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------
class TestToken:
    def test_accepts_correct_token(self):
        assert verify_token(PENDING, TOKEN) is True

    def test_rejects_wrong_missing_or_malformed(self):
        assert verify_token(PENDING, "wrong-token") is False
        assert verify_token(PENDING, "") is False
        assert verify_token(None, TOKEN) is False
        assert verify_token("not-a-dict", TOKEN) is False
        assert verify_token({}, TOKEN) is False
        assert verify_token({"token_sha256": ""}, TOKEN) is False

    def test_approval_state(self):
        assert approval_state(PENDING) == "pending"
        assert approval_state({"approval": "approved"}) == "approved"
        assert approval_state(None) == ""


# ---------------------------------------------------------------------------
# Approval-request email
# ---------------------------------------------------------------------------
class TestApprovalEmail:
    def test_email_contains_summary_and_review_link(self):
        subject, body = build_approval_email(
            run_id="r1",
            prepared=[{"event_id": "ev-1", "subject": "AAPL brief", "to": ["a@x.com"]}],
            review_url="https://app.example/api/review/r1?token=t",
            timeout_hours=24,
        )
        assert "1 email(s)" in subject and "r1" in subject
        assert "AAPL brief" in body and "a@x.com" in body
        assert 'href="https://app.example/api/review/r1?token=t"' in body

    def test_email_escapes_event_derived_values(self):
        _, body = build_approval_email(
            run_id="r1",
            prepared=[{"event_id": "ev-1", "subject": "<script>x</script>", "to": []}],
            review_url="https://app.example/r",
            timeout_hours=24,
        )
        assert "<script>" not in body
        assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------
class TestReviewPage:
    def test_pending_page_has_post_forms_never_get_decision_links(self):
        page = render_review_page(
            instance_id="r1", token=TOKEN,
            previews=[("ev-1", "<html>preview</html>")],
            state="pending", decision_url="/api/decision/r1",
        )
        assert 'method="post"' in page
        assert 'action="/api/decision/r1"' in page
        # decisions must never be GET links (mail scanners prefetch GETs)
        assert 'href="/api/decision' not in page
        # preview embedded in a sandboxed iframe with escaped srcdoc
        assert "sandbox" in page and "srcdoc=" in page
        assert "<html>preview</html>" not in page  # raw preview not inline

    def test_non_pending_page_hides_decision_forms(self):
        page = render_review_page(
            instance_id="r1", token=TOKEN, previews=[],
            state="approved", decision_url="/api/decision/r1",
        )
        assert 'method="post"' not in page
        assert "no longer awaiting approval" in page

    def test_escapes_preview_content_in_srcdoc(self):
        page = render_review_page(
            instance_id="r1", token=TOKEN,
            previews=[("ev-1", '"><script>alert(1)</script>')],
            state="pending", decision_url="/api/decision/r1",
        )
        assert "<script>alert(1)</script>" not in page


# ---------------------------------------------------------------------------
# HTTP handler logic (plain helpers — no Azure runtime)
# ---------------------------------------------------------------------------
def _status(custom):
    class RS:
        name = "Running"

    return SimpleNamespace(runtime_status=RS(), custom_status=custom)


class TestHandlers:
    def test_review_404_unknown_instance(self):
        from calorch.durable.orchestrator import _review_response

        assert _review_response("r1", TOKEN, None).status_code == 404

    def test_review_403_bad_token(self):
        from calorch.durable.orchestrator import _review_response

        assert _review_response("r1", "bad", _status(PENDING)).status_code == 403

    def test_review_renders_page_with_valid_token(self, monkeypatch):
        import calorch.durable.orchestrator as orch

        monkeypatch.setattr(orch, "load_previews", lambda run_id: [("ev-1", "<p>hi</p>")])
        resp = orch._review_response("r1", TOKEN, _status(PENDING))
        assert resp.status_code == 200
        assert b"Approve" in resp.get_body()

    def test_decision_check_paths(self):
        from calorch.durable.orchestrator import _decision_check

        body = f"token={TOKEN}&decision=approve".encode()
        # unknown instance
        err, _ = _decision_check("r1", body, None)
        assert err.status_code == 404
        # bad token
        err, _ = _decision_check("r1", b"token=bad&decision=approve", _status(PENDING))
        assert err.status_code == 403
        # not pending any more
        err, _ = _decision_check(
            "r1", body, _status({"approval": "approved", "token_sha256": token_hash(TOKEN)})
        )
        assert err.status_code == 409
        # bad decision value
        err, _ = _decision_check("r1", f"token={TOKEN}&decision=maybe".encode(), _status(PENDING))
        assert err.status_code == 400
        # happy paths
        err, approved = _decision_check("r1", body, _status(PENDING))
        assert err is None and approved is True
        err, approved = _decision_check(
            "r1", f"token={TOKEN}&decision=reject".encode(), _status(PENDING)
        )
        assert err is None and approved is False


# ---------------------------------------------------------------------------
# Notification activity
# ---------------------------------------------------------------------------
class TestNotifyActivity:
    def _input(self):
        return {
            "run_id": "r1",
            "instance_id": "inst-1",
            "token": TOKEN,
            "prepared": [{"event_id": "ev-1", "subject": "AAPL brief", "to": ["a@x.com"]}],
            "timeout_hours": 24,
        }

    def test_sends_email_with_review_link(self, monkeypatch, tmp_path):
        monkeypatch.setenv("USE_MOCKS", "true")
        monkeypatch.setenv("APPROVER_EMAILS", "approver@firm.example")
        monkeypatch.setenv("APPROVAL_BASE_URL", "https://func.example")
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
        monkeypatch.setenv("REPO_PATH", str(tmp_path / "repo.json"))
        from calorch.config import get_settings

        get_settings.cache_clear()
        try:
            from calorch.durable.activities import _request_approval_impl
            from calorch.nodes import _ctx

            out = _request_approval_impl(self._input())
            assert out["notified"] == ["approver@firm.example"]
            sent = _ctx().graph._sent[-1]
            assert sent["to"] == ["approver@firm.example"]
            assert f"https://func.example/api/review/inst-1?token={TOKEN}" in sent["html"]
        finally:
            get_settings.cache_clear()

    def test_noop_without_approvers(self, monkeypatch, tmp_path):
        monkeypatch.setenv("USE_MOCKS", "true")
        monkeypatch.delenv("APPROVER_EMAILS", raising=False)
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
        monkeypatch.setenv("REPO_PATH", str(tmp_path / "repo.json"))
        from calorch.config import get_settings

        get_settings.cache_clear()
        try:
            from calorch.durable.activities import _request_approval_impl

            out = _request_approval_impl(self._input())
            assert out["notified"] == []
            assert "skipped" in out["log"][0]
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Preview loading from blob storage
# ---------------------------------------------------------------------------
class TestLoadPreviews:
    def test_loads_only_per_event_previews(self, monkeypatch, tmp_path):
        monkeypatch.setenv("USE_MOCKS", "true")
        monkeypatch.setenv("BLOB_LOCAL_ROOT", str(tmp_path / "blobs"))
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
        from calorch.config import get_settings

        get_settings.cache_clear()
        try:
            from calorch.blob_store import make_blob_store
            from calorch.durable.approval import load_previews

            s = get_settings()
            store = make_blob_store(local_root=s.blob_local_root)
            store.upload_bytes(store.output_container, "outputs/r1/ev-1/ev-1.html", b"<p>one</p>")
            store.upload_bytes(store.output_container, "outputs/r1/ev-2/ev-2.html", b"<p>two</p>")
            store.upload_bytes(store.output_container, "outputs/r1/ev-1/ev-1_analysis.json", b"{}")
            store.upload_bytes(store.output_container, "outputs/r1/ev-1/ev-1.docx", b"bin")
            store.upload_bytes(store.output_container, "outputs/other/ev-9/ev-9.html", b"<p>no</p>")

            previews = load_previews("r1")
            assert previews == [("ev-1", "<p>one</p>"), ("ev-2", "<p>two</p>")]
        finally:
            get_settings.cache_clear()


def test_decision_page_mentions_outcome():
    assert "approved" in render_decision_page("r1", True)
    assert "rejected" in render_decision_page("r1", False)
