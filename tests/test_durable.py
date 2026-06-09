"""Tests for calorch.durable — timer-triggered Azure Durable Functions.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from calorch.state import CalendarEvent, ClassificationResult, EventType


# ---------------------------------------------------------------------------
# State adapter tests
# ---------------------------------------------------------------------------
class TestStateAdapter:
    def test_serialize_datetime(self):
        from calorch.durable.activities import _ensure_context
        dt = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)
        assert dt.isoformat() in ("2026-03-02T10:00:00+00:00", "2026-03-02T10:00:00Z")

    def test_calendar_event_roundtrip(self):
        ev = CalendarEvent(
            id="ev-001",
            subject="AAPL Earnings",
            start=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 3, 2, 11, 0, 0, tzinfo=timezone.utc),
        )
        data = ev.model_dump(mode="json")
        restored = CalendarEvent.model_validate(data)
        assert restored.id == "ev-001"
        assert restored.subject == "AAPL Earnings"


# ---------------------------------------------------------------------------
# Orchestrator existence
# ---------------------------------------------------------------------------
class TestOrchestrator:
    def test_orchestrator_function_exists(self):
        from calorch.durable.orchestrator import calorch_orchestrator
        assert calorch_orchestrator is not None

    def test_timer_trigger_exists(self):
        from calorch.durable.orchestrator import timer_start
        assert timer_start is not None

    def test_new_run_id_format(self):
        from calorch.durable.orchestrator import _new_run_id
        run_id = _new_run_id()
        assert len(run_id) == 16
        assert "T" in run_id
        assert run_id.endswith("Z")


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------
class TestActivities:
    def test_all_activities_exist(self):
        from calorch.durable.activities import activity_register
        assert len(activity_register) == 5
        names = [a._function._name for a in activity_register]
        assert "activity_scan_calendar" in names
        assert "activity_classify" in names
        assert "activity_agent" in names
        assert "activity_deliver" in names
        assert "activity_aggregate_briefing" in names


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
class TestBlueprintRegistration:
    def test_blueprints_can_be_created(self):
        import azure.durable_functions as df
        from calorch.durable.orchestrator import get_blueprint as get_orchestrator_bp
        from calorch.durable.activities import get_blueprint as get_activities_bp
        assert isinstance(get_orchestrator_bp(), df.Blueprint)
        assert isinstance(get_activities_bp(), df.Blueprint)

    def test_blueprints_can_be_registered(self):
        import azure.functions as func
        from calorch.durable import register_blueprints
        app = func.FunctionApp()
        register_blueprints(app)
