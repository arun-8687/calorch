"""Azure Durable Functions orchestration for calorch.

Hybrid architecture:
  * Azure Durable Functions = orchestrator (flow control, retries,
    human-in-the-loop approval, timers, fan-out/fan-in)
  * LangGraph = multi-agent subgraphs (per-event preparation) running
    inside durable activities

Triggers: timer (scheduled run), HTTP (on-demand run / approval / status).

Usage (in function_app.py at the project root):
    import azure.functions as func
    from calorch.durable import register_blueprints

    app = func.FunctionApp()
    register_blueprints(app)
"""
from __future__ import annotations

import azure.functions as func

from .activities import get_blueprint as get_activities_blueprint
from .orchestrator import get_blueprint as get_orchestrator_blueprint


def register_blueprints(app: func.FunctionApp) -> None:
    """Register all Durable Functions blueprints on a FunctionApp."""
    app.register_blueprint(get_orchestrator_blueprint())
    app.register_blueprint(get_activities_blueprint())


__all__ = ["register_blueprints"]
