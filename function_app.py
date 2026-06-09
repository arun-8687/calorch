"""Azure Functions entry point for timer-triggered durable orchestration.
"""
from __future__ import annotations

import azure.functions as func

from calorch.durable import register_blueprints

app = func.FunctionApp()
register_blueprints(app)
