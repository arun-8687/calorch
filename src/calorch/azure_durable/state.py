"""State adapter for Azure Durable Functions.

Converts between LangGraph ``OrchestratorState`` (TypedDict + Pydantic models)
and JSON-serializable dicts suitable for ADF activity input/output.

All datetime objects are ISO-8601 strings. Pydantic models use ``model_dump()``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def serialize_state(state: Any) -> dict[str, Any]:
    """Convert a LangGraph state object to a JSON-serializable dict.

    Handles:
      * Pydantic BaseModel → model_dump(mode="json")
      * datetime → ISO-8601 string
      * dict/list/str/int/float/bool/None → passthrough
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
    """Convert a JSON-serializable dict back to LangGraph state objects.

    If *target_type* is a Pydantic model class, reconstructs the model.
    Otherwise returns a plain dict/list.
    """
    if data is None:
        return None
    if isinstance(data, str):
        # Try to parse ISO-8601 datetime
        try:
            if "T" in data:
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
