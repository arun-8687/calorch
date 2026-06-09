"""Azure Durable Functions orchestration for calorch.

Hybrid architecture:
  * Azure Durable Functions = orchestrator (flow control, retry, approval, timers)
  * LangGraph = agent subgraphs (per-event preparation, state management)

The ADF orchestrator sequences high-level activities:
  1. scan_calendar → activity
  2. classify → activity
  3. fan-out: each event → its LangGraph agent subgraph (parallel)
  4. approval gate → external event
  5. fan-out: deliver each event (parallel)
  6. aggregate briefing → activity

Usage:
    # In function_app.py (project root):
    from calorch.azure_durable import register_blueprints
    import azure.functions as func
    
    app = func.FunctionApp()
    register_blueprints(app)
"""
from __future__ import annotations

import azure.durable_functions as df
import azure.functions as func

from .orchestrator import get_blueprint as get_orchestrator_blueprint
from .activities import get_blueprint as get_activities_blueprint


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
def register_blueprints(app: func.FunctionApp) -> None:
    """Register all Durable Functions on a FunctionApp."""
    app.register_blueprint(get_orchestrator_blueprint())
    app.register_blueprint(get_activities_blueprint())


def get_blueprint() -> df.Blueprint:
    """Return a combined Blueprint (for testing)."""
    # Note: In production, use register_blueprints to register both separately
    return get_orchestrator_blueprint()


__all__ = [
    "register_blueprints",
    "get_blueprint",
]
