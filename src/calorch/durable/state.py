"""State adapter for Azure Durable Functions.

Converts between LangGraph ``OrchestratorState`` objects (TypedDict +
Pydantic models) and JSON-serializable dicts suitable for ADF activity
input/output. All datetimes are ISO-8601 strings on the wire.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from calorch.state import CalendarEvent, ClassificationResult


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def serialize_state(state: Any) -> Any:
    """Convert a LangGraph state object to a JSON-serializable value.

    Handles:
      * Pydantic BaseModel → model_dump(mode="json")
      * datetime → ISO-8601 string
      * dict/list → recursed
      * str/int/float/bool/None → passthrough
    """
    if state is None:
        return None
    if isinstance(state, BaseModel):
        return state.model_dump(mode="json")
    if isinstance(state, datetime):
        return state.isoformat()
    if isinstance(state, list):
        return [serialize_state(item) for item in state]
    if isinstance(state, dict):
        return {k: serialize_state(v) for k, v in state.items()}
    if isinstance(state, (str, int, float, bool)):
        return state
    return str(state)


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------
def deserialize_state(data: Any, target_type: type | None = None) -> Any:
    """Convert a JSON-serializable value back to LangGraph state objects.

    If *target_type* is a Pydantic model class, reconstructs the model.
    ISO-8601 strings become datetimes; other values pass through.
    """
    if data is None:
        return None
    if isinstance(data, str):
        if "T" in data:
            try:
                return datetime.fromisoformat(data.replace("Z", "+00:00"))
            except ValueError:
                pass
        return data
    if isinstance(data, list):
        return [deserialize_state(item) for item in data]
    if isinstance(data, dict):
        if target_type and issubclass(target_type, BaseModel):
            return target_type.model_validate(data)
        return {k: deserialize_state(v) for k, v in data.items()}
    return data


# ---------------------------------------------------------------------------
# Typed helpers used by activities
# ---------------------------------------------------------------------------
def parse_iso(value: Any) -> Any:
    """Parse an ISO-8601 string to datetime; pass datetimes through."""
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def parse_event(data: Any) -> CalendarEvent:
    """Rehydrate a CalendarEvent from an activity-input dict."""
    if isinstance(data, CalendarEvent):
        return data
    data = dict(data)
    for key in ("start", "end"):
        data[key] = parse_iso(data.get(key))
    return CalendarEvent.model_validate(data)


def parse_classification(data: Any) -> ClassificationResult:
    """Rehydrate a ClassificationResult from an activity-input dict."""
    if isinstance(data, ClassificationResult):
        return data
    return ClassificationResult.model_validate(data)
