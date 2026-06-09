"""Multi-agent registry — one independent LangGraph agent per event type.

Each agent is a self-contained module that registers an
:class:`~calorch.agents.base.AgentSpec` (classification keywords,
analysis builder, optional custom subgraph). The orchestrator, the Send
fan-out, the keyword pre-filter and the analysis dispatch all consult
the registry, so nothing else needs to change when an agent is added.

How to add a new agent
======================
1. Add the new member to ``calorch.state.EventType`` (the classifier's
   typed vocabulary).
2. Create one module that registers the agent::

       # myproject/ipo_roadshow.py
       from calorch.agents import AgentSpec, register
       from calorch.state import EventType

       def build_ipo_roadshow(event, cls, ed, llm_call, *, providers=None, cik_lookup=None):
           ...  # return an EventAnalysis

       register(AgentSpec(
           event_type=EventType.IPO_ROADSHOW,
           analysis_builder=build_ipo_roadshow,
           keywords=("ipo", "roadshow", "s-1"),
       ))

3. Make sure the module is imported: drop it in
   ``calorch/agents/builtin/`` (and add it to that package's __init__),
   or list it in the ``CALORCH_AGENT_MODULES`` env var
   (comma-separated import paths) for deployment-specific agents.

Agents that need a richer shape than the default single prepare node
(extra tool nodes, inner loops) can pass ``graph_factory=`` to build
their own ``StateGraph``; it only has to honour AgentInput/AgentOutput.
"""
from __future__ import annotations

import importlib
import os

from .base import (
    AgentSpec,
    agent_node_names,
    classification_keywords,
    default_graph_factory,
    get_agent,
    iter_agents,
    make_agent_subgraph,
    register,
    unregister,
)

# Register the built-in agents.
from . import builtin  # noqa: F401  (import for registration side effects)


def _load_extra_agent_modules() -> None:
    """Import deployment-specific agent modules from CALORCH_AGENT_MODULES."""
    for path in os.getenv("CALORCH_AGENT_MODULES", "").split(","):
        path = path.strip()
        if path:
            importlib.import_module(path)


_load_extra_agent_modules()

__all__ = [
    "AgentSpec",
    "agent_node_names",
    "classification_keywords",
    "default_graph_factory",
    "get_agent",
    "iter_agents",
    "make_agent_subgraph",
    "register",
    "unregister",
]
