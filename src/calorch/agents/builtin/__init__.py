"""Built-in event-type agents.

Importing this package registers every built-in agent. Each module is
self-contained: it owns its event type's classification keywords and
analysis builder, and registers an AgentSpec on import.
"""
from . import (  # noqa: F401  (imported for registration side effects)
    analyst_meeting,
    channel_check,
    conference,
    earnings_call,
    internal_review,
    kol_meeting,
    management_meeting,
    portfolio_meeting,
    unknown,
)
