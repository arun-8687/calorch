"""Tests for the StateGraph assembly and full happy path."""
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from calorch.config import get_settings
from calorch.graph import make_graph
from calorch.llm import get_chat_model
from calorch.nodes import Context, deliver_event, set_context
from calorch.tools import (
    MockGraphClient,
    make_enterprise_data_client,
    make_graph_client,
    make_onedrive_client,
    make_repository,
)


@pytest.fixture
def tmp_output(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("REPO_BACKEND", "json")
    monkeypatch.setenv("REPO_PATH", str(tmp_path / "repo.json"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path


@pytest.fixture
def ctx(tmp_output):
    s = get_settings()
    from calorch.tools import make_providers, make_cik_lookup
    ctx = Context(
        graph=make_graph_client(s),
        onedrive=make_onedrive_client(s),
        repo=make_repository(s),
        enterprise=make_enterprise_data_client(s),
        llm=get_chat_model(s),
        output_dir=tmp_output,
        send_emails=False,
        providers=make_providers(s),
        cik_lookup=make_cik_lookup(s),
    )
    set_context(ctx)
    return ctx


def test_graph_compiles():
    g = make_graph()
    assert "scan_calendar" in g.nodes
    assert "prepare_event" in g.nodes
    assert "approval_gate" in g.nodes
    assert "deliver_event" in g.nodes
    assert "aggregate_briefing" in g.nodes


def test_end_to_end_runs_all_eight(ctx):
    g = make_graph()
    state = {
        "window_start": datetime(2026, 3, 2, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 9, tzinfo=timezone.utc),
        "use_mocks": True,
        "run_id": "test-run",
    }
    result = g.invoke(state, config={"configurable": {"thread_id": "test"}})
    assert len(result["events"]) == 8
    assert len(result["classifications"]) == 8
    # 8 DOCX files written
    assert len(result["documents"]) == 8
    docs = list(Path(ctx.output_dir, "runs", "test-run", "docs").glob("*.docx"))
    assert len(docs) == 8
    # 8 email drafts created
    assert len(result["emails"]) == 8
    assert all(em.status == "draft" for em in result["emails"].values())
    # No errors
    assert result.get("errors") == []
    # Briefing written
    assert result["weekly_briefing"] is not None
    assert Path(result["weekly_briefing"].path).exists()


def test_send_run_pauses_after_previews_then_resumes(ctx):
    from langgraph.types import Command

    g = make_graph()
    cfg = {"configurable": {"thread_id": "approval-test"}}
    state = {
        "window_start": datetime(2026, 3, 2, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 9, tzinfo=timezone.utc),
        "use_mocks": True,
        "run_id": "approval-test",
        "send_emails": True,
        "require_approval": True,
    }
    paused = g.invoke(state, config=cfg)
    assert paused.get("__interrupt__")
    assert len(paused["prepared_emails"]) == 8
    assert paused.get("emails", {}) == {}
    assert ctx.graph.sent == []

    result = g.invoke(Command(resume={"approved": True}), config=cfg)
    assert result["approval_status"] == "approved"
    assert len(result["emails"]) == 8
    assert all(em.status == "sent" for em in result["emails"].values())
    assert len(ctx.graph.sent) == 8


def test_rejected_send_run_never_delivers(ctx):
    from langgraph.types import Command

    g = make_graph()
    cfg = {"configurable": {"thread_id": "rejected-test"}}
    state = {
        "window_start": datetime(2026, 3, 2, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 9, tzinfo=timezone.utc),
        "use_mocks": True,
        "run_id": "rejected-test",
        "send_emails": True,
        "require_approval": True,
    }
    paused = g.invoke(state, config=cfg)
    assert paused.get("__interrupt__")

    result = g.invoke(Command(resume={"approved": False}), config=cfg)
    assert result["approval_status"] == "rejected"
    assert result.get("emails", {}) == {}
    assert ctx.graph.sent == []
    assert result["weekly_briefing"] is not None


def test_delivery_replay_is_idempotent(ctx):
    g = make_graph()
    state = {
        "window_start": datetime(2026, 3, 2, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 9, tzinfo=timezone.utc),
        "use_mocks": True,
        "run_id": "idempotency-test",
        "send_emails": False,
    }
    result = g.invoke(state, config={"configurable": {"thread_id": "idempotency-test"}})
    assert len(ctx.graph.drafts) == 8
    event = result["events"][0]
    preview = result["prepared_emails"][event.id]
    document = result["documents"][event.id]
    replay = deliver_event(
        {
            "event": event.model_dump(mode="json"),
            "classification": result["classifications"][event.id].model_dump(mode="json"),
            "preview": preview.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
            "onedrive_url": result["calendar_links"][event.id],
            "run_id": state["run_id"],
            "send_emails": False,
        }
    )
    assert replay["emails"][event.id].status == "draft"
    assert len(ctx.graph.drafts) == 8


def test_empty_calendar_still_writes_briefing(ctx):
    ctx.graph = MockGraphClient(fixtures=[])
    g = make_graph()
    state = {
        "window_start": datetime(2026, 3, 2, tzinfo=timezone.utc),
        "window_end": datetime(2026, 3, 9, tzinfo=timezone.utc),
        "use_mocks": True,
        "run_id": "empty-test",
    }
    result = g.invoke(state, config={"configurable": {"thread_id": "empty-test"}})
    assert result["events"] == []
    assert result.get("emails", {}) == {}
    assert result["weekly_briefing"].event_count == 0
