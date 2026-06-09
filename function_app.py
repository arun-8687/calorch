"""Azure Functions entry point for calorch.

This file is the entry point for the Azure Functions runtime.
It registers the Durable Functions Blueprint from calorch.azure_durable.
"""
from __future__ import annotations

import azure.functions as func

from calorch.azure_durable import register_blueprints

# Create the FunctionApp
app = func.FunctionApp()

# Register all Durable Functions (orchestrator, activities, HTTP triggers)
register_blueprints(app)
