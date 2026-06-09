"""Tests for calorch.durable — Azure Durable Functions orchestration.

Covers:
  * the state adapter (serialize/deserialize round-trips)
  * the orchestrator generator logic, driven with a fake durable context
    (no Azure runtime needed)
  * blueprint registration
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from calorch.durable.state import (
    deserialize_state,
    parse_event,
    serialize_state,
)
from calorch.state import CalendarEvent, ClassificationResult, EventType


# ---------------------------------------------------------------------------
# State adapter
# ---------------------------------------------------------------------------
class TestStateAdapter:
    def test_serialize_datetime(self):
        dt = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)
        assert serialize_state(dt) == "2026-03-02T10:00:00+00:00"

    def test_serialize_calendar_event(self):
        ev = CalendarEvent(
            id="ev-001",
            subject="AAPL Q2 FY2026 Earnings Call",
            start=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 3, 2, 11, 0, 0, tzinfo=timezone.utc),
        )
        result = serialize_state(ev)
        assert result["id"] == "ev-001"
        assert result["start"] in ("2026-03-02T10:00:00+00:00", "2026-03-02T10:00:00Z")

    def test_serialize_nested_dict(self):
        data = {
            "event": CalendarEvent(
                id="ev-001",
                subject="test",
                start=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 3, 2, 11, 0, 0, tzinfo=timezone.utc),
            ),
            "count": 42,
        }
        result = serialize_state(data)
        assert result["event"]["id"] == "ev-001"
        assert result["count"] == 42

    def test_deserialize_datetime(self):
        result = deserialize_state("2026-03-02T10:00:00+00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026

    def test_deserialize_calendar_event(self):
        data = {
            "id": "ev-001",
            "subject": "test",
            "start": "2026-03-02T10:00:00+00:00",
            "end": "2026-03-02T11:00:00+00:00",
        }
        result = deserialize_state(data, CalendarEvent)
        assert isinstance(result, CalendarEvent)
        assert result.start.year == 2026

    def test_classification_roundtrip(self):
        cls = ClassificationResult(
            event_id="ev-001",
            pass1_label=EventType.EARNINGS_CALL,
            pass1_keyword_hits=5,
            final_label=EventType.EARNINGS_CALL,
            confidence=0.95,
            rationale="SEC form",
            routed_node="agent_earnings_call",
        )
        restored = deserialize_state(serialize_state(cls), ClassificationResult)
        assert restored.confidence == 0.95
        assert restored.final_label == EventType.EARNINGS_CALL

    def test_parse_event_from_json(self):
        ev = parse_event(
            {
                "id": "ev-001",
                "subject": "AAPL Earnings",
                "start": "2026-03-02T10:00:00Z",
                "end": "2026-03-02T11:00:00Z",
            }
        )
        assert ev.id == "ev-001"
        assert ev.start.tzinfo is not None


# ---------------------------------------------------------------------------
# Fake durable context — drives the orchestrator generator without Azure
# ---------------------------------------------------------------------------
class FakeTask:
    def __init__(self, kind: str, name: str = "", payload: Any = None):
        self.kind = kind
        self.name = name
        self.payload = payload
        self.result: Any = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeContext:
    def __init__(self, input_data: dict[str, Any], now: datetime | None = None):
        self._input = input_data
        self.current_utc_datetime = now or datetime(2026, 6, 8, 9, 0, 0, tzinfo=timezone.utc)
        self.activity_calls: list[FakeTask] = []
        self.timers: list[FakeTask] = []

    def get_input(self):
        return self._input

    def call_activity_with_retry(self, name, retry_options, input_=None):
        task = FakeTask("activity", name, input_)
        self.activity_calls.append(task)
        return task

    def task_all(self, tasks):
        return FakeTask("task_all", payload=tasks)

    def wait_for_external_event(self, name):
        return FakeTask("external_event", name)

    def create_timer(self, fire_at):
        timer = FakeTask("timer", payload=fire_at)
        self.timers.append(timer)
        return timer

    def task_any(self, tasks):
        return FakeTask("task_any", payload=tasks)


def drive(gen, responder: Callable[[FakeTask], Any]) -> dict[str, Any]:
    """Run the orchestrator generator to completion, answering each yield."""
    try:
        task = next(gen)
        while True:
            task = gen.send(responder(task))
    except StopIteration as stop:
        return stop.value


_EVENT = {
    "id": "ev-1",
    "subject": "AAPL Earnings",
    "start": "2026-06-10T10:00:00+00:00",
    "end": "2026-06-10T11:00:00+00:00",
}
_CLASSIFICATION = {"ev-1": {"event_id": "ev-1", "final_label": "earnings_call"}}
_AGENT_RESULT = {
    "documents": {"ev-1": {"path": "x.docx"}},
    "prepared_emails": {"ev-1": {"subject": "AAPL brief"}},
    "calendar_links": {"ev-1": "https://onedrive/x"},
    "errors": [],
    "log": ["agent: prepared ev-1"],
}
_DELIVER_RESULT = {
    "emails": {"ev-1": {"status": "sent"}},
    "followups": [{"event_id": "ev-1", "action": "follow up", "owner": "analyst"}],
    "errors": [],
    "log": ["delivered ev-1"],
}


def _happy_path_responder(approval: str | None = None):
    """Respond to orchestrator yields with canned activity results.

    approval: None (gate skipped), "approve", "reject", or "timeout".
    """

    def responder(task: FakeTask):
        if task.kind == "activity":
            return {
                "activity_scan_calendar": {"events": [_EVENT], "raw_events": [{"id": "ev-1", "_form": "10-Q"}]},
                "activity_classify": {"classifications": _CLASSIFICATION},
                "activity_aggregate_briefing": {"weekly_briefing": {"event_count": 1}},
            }[task.name]
        if task.kind == "task_all":
            inner = task.payload
            if inner and inner[0].name == "activity_agent":
                return [_AGENT_RESULT for _ in inner]
            return [_DELIVER_RESULT for _ in inner]
        if task.kind == "task_any":
            approval_task, timeout_task = task.payload
            if approval == "timeout":
                return timeout_task
            approval_task.result = {"approved": approval == "approve"}
            return approval_task
        raise AssertionError(f"unexpected task {task.kind}")

    return responder


class TestOrchestratorLogic:
    def _run(self, input_data: dict[str, Any], approval: str | None = None):
        from calorch.durable.orchestrator import run_orchestrator

        ctx = FakeContext(input_data)
        result = drive(run_orchestrator(ctx), _happy_path_responder(approval))
        return ctx, result

    def test_no_events_short_circuits(self):
        from calorch.durable.orchestrator import run_orchestrator

        ctx = FakeContext({"run_id": "r1"})

        def responder(task):
            assert task.name == "activity_scan_calendar"
            return {"events": [], "raw_events": []}

        result = drive(run_orchestrator(ctx), responder)
        assert result["event_count"] == 0
        assert result["status"] == "completed"
        assert len(ctx.activity_calls) == 1

    def test_run_id_defaults_to_orchestration_clock(self):
        """run_id must come from current_utc_datetime, never datetime.now()."""
        ctx, result = self._run({})
        assert result["run_id"] == "20260608T090000Z"

    def test_raw_events_passed_to_classify(self):
        """The SEC form fast-path needs raw_events in the classify input."""
        ctx, _ = self._run({"run_id": "r1"})
        classify = next(t for t in ctx.activity_calls if t.name == "activity_classify")
        assert classify.payload["raw_events"] == [{"id": "ev-1", "_form": "10-Q"}]

    def test_draft_mode_skips_approval_gate(self):
        ctx, result = self._run({"run_id": "r1", "send_emails": False})
        assert result["approval_status"] == "not_required"
        assert result["emails"] == ["ev-1"]
        assert ctx.timers == []

    def test_approval_approved_delivers(self):
        ctx, result = self._run(
            {"run_id": "r1", "send_emails": True, "require_approval": True},
            approval="approve",
        )
        assert result["approval_status"] == "approved"
        assert result["emails"] == ["ev-1"]
        assert ctx.timers[0].cancelled is True

    def test_approval_rejected_skips_delivery(self):
        ctx, result = self._run(
            {"run_id": "r1", "send_emails": True, "require_approval": True},
            approval="reject",
        )
        assert result["approval_status"] == "rejected"
        assert result["emails"] == []
        deliver_calls = [t for t in ctx.activity_calls if t.name == "activity_deliver"]
        assert deliver_calls == []

    def test_approval_timeout_skips_delivery(self):
        ctx, result = self._run(
            {"run_id": "r1", "send_emails": True, "require_approval": True},
            approval="timeout",
        )
        assert result["approval_status"] == "timed_out"
        assert result["emails"] == []

    def test_delivery_results_reach_briefing(self):
        """Emails and follow-ups from delivery must flow into the briefing."""
        ctx, _ = self._run({"run_id": "r1", "send_emails": True, "require_approval": False})
        briefing = next(
            t for t in ctx.activity_calls if t.name == "activity_aggregate_briefing"
        )
        assert briefing.payload["emails"] == {"ev-1": {"status": "sent"}}
        assert len(briefing.payload["followups"]) == 1
        assert briefing.payload["classifications"] == _CLASSIFICATION

    def test_briefing_in_output(self):
        _, result = self._run({"run_id": "r1"})
        assert result["weekly_briefing"] == {"event_count": 1}
        assert result["followup_count"] == 1


# ---------------------------------------------------------------------------
# Registration / wiring
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_all_activities_exist(self):
        from calorch.durable.activities import activity_register

        names = [a._function._name for a in activity_register]
        assert names == [
            "activity_scan_calendar",
            "activity_classify",
            "activity_agent",
            "activity_deliver",
            "activity_aggregate_briefing",
        ]

    def test_orchestrator_and_triggers_exist(self):
        from calorch.durable.orchestrator import (
            calorch_orchestrator,
            http_approval,
            http_start,
            http_status,
            timer_start,
        )

        for fn in (calorch_orchestrator, timer_start, http_start, http_approval, http_status):
            assert fn is not None

    def test_blueprints_can_be_registered(self):
        import azure.functions as func

        from calorch.durable import register_blueprints

        app = func.FunctionApp()
        register_blueprints(app)
        registered = {f.get_function_name() for f in app.get_functions()}
        assert "calorch_orchestrator" in registered
        assert "activity_agent" in registered
