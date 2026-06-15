"""Azure Functions entry point for timer-triggered durable orchestration.
"""
from __future__ import annotations

import azure.functions as func

from calorch.logging_config import configure_logging
from calorch.durable import register_blueprints

# Install calorch's structured logging (PII/secret redaction) on every worker
# process before any logger is used. Honours LOG_FORMAT / LOG_LEVEL env vars.
configure_logging()

app = func.FunctionApp()
register_blueprints(app)
