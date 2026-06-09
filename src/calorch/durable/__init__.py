"""Azure Durable Functions — timer-triggered orchestrator with LangGraph agents.
"""
from __future__ import annotations

import azure.durable_functions as df
import azure.functions as func

from .orchestrator import get_blueprint as get_orchestrator_bp
from .activities import get_blueprint as get_activities_bp


def register_blueprints(app: func.FunctionApp) -> None:
    """Register all Durable Functions blueprints on a FunctionApp."""
    app.register_blueprint(get_orchestrator_bp())
    app.register_blueprint(get_activities_bp())


__all__ = ["register_blueprints"]
