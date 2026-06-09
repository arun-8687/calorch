"""Typed state schema, enums and Pydantic models for the orchestrator.

Mirrors the eight event types defined in the Architecture Decision Record
(Comparative_Analysis_Enterprise.docx, §"3. Event Routing (8 Types)").
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ---------------------------------------------------------------------------
# Enums (typed enum return values for GPT-4o Structured Output)
# ---------------------------------------------------------------------------
class EventType(str, Enum):
    EARNINGS_CALL = "earnings_call"
    MANAGEMENT_MEETING = "management_meeting"
    CONFERENCE = "conference"
    KOL_MEETING = "kol_meeting"
    CHANNEL_CHECK = "channel_check"
    PORTFOLIO_MEETING = "portfolio_meeting"
    INTERNAL_REVIEW = "internal_review"
    ANALYST_MEETING = "analyst_meeting"
    UNKNOWN = "unknown"


# Maps EventType to descriptive labels. The preparation branch dispatches
# internally via ``build_analysis()``. These labels are kept for logging and
# classification traceability; they are not graph node names.
EVENT_TYPE_TO_NODE: dict[EventType, str] = {
    EventType.EARNINGS_CALL: "earnings_call",
    EventType.MANAGEMENT_MEETING: "management_meeting",
    EventType.CONFERENCE: "conference",
    EventType.KOL_MEETING: "kol_meeting",
    EventType.CHANNEL_CHECK: "channel_check",
    EventType.PORTFOLIO_MEETING: "portfolio_meeting",
    EventType.INTERNAL_REVIEW: "internal_review",
    EventType.ANALYST_MEETING: "analyst_meeting",
    EventType.UNKNOWN: "unknown",
}


# Maps EventType to the agent subgraph node name in the main graph.
EVENT_TYPE_TO_AGENT: dict[EventType, str] = {
    EventType.EARNINGS_CALL: "agent_earnings_call",
    EventType.MANAGEMENT_MEETING: "agent_management_meeting",
    EventType.CONFERENCE: "agent_conference",
    EventType.KOL_MEETING: "agent_kol_meeting",
    EventType.CHANNEL_CHECK: "agent_channel_check",
    EventType.PORTFOLIO_MEETING: "agent_portfolio_meeting",
    EventType.INTERNAL_REVIEW: "agent_internal_review",
    EventType.ANALYST_MEETING: "agent_analyst_meeting",
    EventType.UNKNOWN: "agent_unknown",
}


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------
class CalendarEvent(BaseModel):
    """An Outlook calendar event (Microsoft Graph shape, subset).

    When sourced from SEC EDGAR, ``sec_*`` fields carry the underlying
    filing metadata (form code, CIK, accession, etc.) so downstream
    nodes can use them without re-querying EDGAR.
    """

    id: str
    subject: str
    body_preview: str = ""
    start: datetime
    end: datetime
    organizer: str = ""
    attendees: list[str] = Field(default_factory=list)
    location: str = ""
    is_online: bool = False
    web_link: str = ""

    # ---- SEC EDGAR-only fields (ignored when None) ----
    sec_ticker: str | None = None
    sec_cik: str | None = None
    sec_form: str | None = None
    sec_accession: str | None = None
    sec_filing_date: str | None = None
    sec_company: str | None = None
    sec_items: str | None = None


class ClassificationResult(BaseModel):
    """Output of the two-pass classifier.

    Pass 1: keyword scoring produces a coarse label + count.
    Pass 2: GPT-4o Structured Output produces the final typed enum and
    a calibrated float confidence, plus the rationale.
    """

    event_id: str
    pass1_label: EventType = EventType.UNKNOWN
    pass1_keyword_hits: int = 0
    final_label: EventType = EventType.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str = ""
    routed_node: str = "handle_unknown"


class DocxArtifact(BaseModel):
    event_id: str
    path: str
    sha256: str
    bytes: int
    blob_url: str = ""


class EmailArtifact(BaseModel):
    event_id: str
    to: list[str]
    subject: str
    html_path: str
    attachment_path: Optional[str] = None
    status: Literal["draft", "sent", "failed"] = "draft"
    graph_message_id: Optional[str] = None


class PreparedEmailArtifact(BaseModel):
    """Reviewable email payload produced before external mail delivery."""

    event_id: str
    to: list[str]
    subject: str
    html_path: str
    html: str
    attachment_path: Optional[str] = None
    document_url: Optional[str] = None
    blob_url: str = ""


class FollowUpItem(BaseModel):
    event_id: str
    action: str
    owner: str
    due: Optional[datetime] = None
    notes: str = ""


class BriefingSection(BaseModel):
    heading: str
    body: str


class WeeklyBriefing(BaseModel):
    week_start: datetime
    week_end: datetime
    sections: list[BriefingSection] = Field(default_factory=list)
    event_count: int = 0
    path: str = ""
    blob_url: str = ""


# ---------------------------------------------------------------------------
# LangGraph state (TypedDict + reducers)
# ---------------------------------------------------------------------------
def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Reducer for per-event maps: right wins for overlapping keys."""
    return {**left, **right}


def _append(left: list[Any], right: list[Any]) -> list[Any]:
    """Reducer for append-only lists (errors, follow-ups, log lines)."""
    if not left:
        return list(right or [])
    if not right:
        return list(left)
    return [*left, *right]


def _append_unique_followups(
    left: list[FollowUpItem], right: list[FollowUpItem]
) -> list[FollowUpItem]:
    """Keep one follow-up per event when a delivery branch is replayed."""
    merged: dict[str, FollowUpItem] = {item.event_id: item for item in left or []}
    for item in right or []:
        merged[item.event_id] = item
    return list(merged.values())


class OrchestratorState(TypedDict, total=False):
    # ---- run controls ----
    window_start: datetime
    window_end: datetime
    use_mocks: bool
    run_id: str
    send_emails: bool
    require_approval: bool
    delivery_approved: bool
    approval_status: Literal["not_required", "pending", "approved", "rejected"]

    # ---- inputs ----
    raw_events: list[dict[str, Any]]          # raw Microsoft Graph payload
    events: list[CalendarEvent]                # typed

    # ---- classification ----
    classifications: Annotated[dict[str, ClassificationResult], _merge_dicts]

    # ---- artefacts (per event) ----
    documents: Annotated[dict[str, DocxArtifact], _merge_dicts]
    prepared_emails: Annotated[dict[str, PreparedEmailArtifact], _merge_dicts]
    emails: Annotated[dict[str, EmailArtifact], _merge_dicts]
    calendar_links: Annotated[dict[str, str], _merge_dicts]

    # ---- aggregations ----
    followups: Annotated[list[FollowUpItem], _append_unique_followups]
    weekly_briefing: Optional[WeeklyBriefing]

    # ---- observability ----
    errors: Annotated[list[str], _append]
    log: Annotated[list[str], _append]


# ---------------------------------------------------------------------------
# Agent subgraph state (for multi-agent orchestration)
# ---------------------------------------------------------------------------
class AgentInput(TypedDict):
    """Input keys passed to an agent subgraph via Send()."""
    event: dict[str, Any]
    classification: dict[str, Any]
    run_id: str


class AgentOutput(TypedDict):
    """Output keys returned by an agent subgraph to the parent graph."""
    documents: dict[str, DocxArtifact]
    prepared_emails: dict[str, PreparedEmailArtifact]
    calendar_links: dict[str, str]
    errors: list[str]
    log: list[str]


class AgentState(AgentInput, AgentOutput):
    """Full internal state of an agent subgraph."""
    pass


# ---------------------------------------------------------------------------
# Errors raised by tools/nodes
# ---------------------------------------------------------------------------
class OrchestratorError(RuntimeError):
    pass
