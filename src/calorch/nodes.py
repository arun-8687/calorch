"""Graph node implementations.

Nodes are organised by stage:
  * ``calendar_*``      — Microsoft Graph integration
  * ``classify_*``      — Pass 1 (keywords) and Pass 2 (LLM)
  * ``route_*``         — conditional edge helpers
  * ``enrich_*``        — event-type-specific data fetch + analysis
  * ``deliver_*``       — DOCX + HTML + OneDrive + Calendar + Email + Repo

A module-level ``Context`` is set by the runner (CLI / HTTP entry) and
closed over by the node functions. This keeps the LangGraph state free
of runtime objects.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from langchain_core.runnables import RunnableConfig

from calorch.analysis import EventAnalysis, build_analysis
from calorch.renderers import (
    render_docx,
    render_html_email,
    sha256_bytes,
    write_text,
)
from calorch.state import (
    ClassificationResult,
    DocxArtifact,
    EmailArtifact,
    EventType,
    FollowUpItem,
    OrchestratorError,
    OrchestratorState,
    PreparedEmailArtifact,
    WeeklyBriefing,
)
from calorch.tools import (
    EnterpriseDataClient,
    GraphClient,
    OneDriveClient,
    Repository,
    sha256_file,
    to_calendar_event,
)

from calorch.telemetry import start_span

log = logging.getLogger("calorch.nodes")


# ---------------------------------------------------------------------------
# Runtime context — set by the runner, closed over by nodes.
# ---------------------------------------------------------------------------
@dataclass
class Context:
    graph: GraphClient
    onedrive: OneDriveClient
    repo: Repository
    enterprise: EnterpriseDataClient
    llm: Any                              # langchain BaseChatModel (or mock)
    output_dir: Path
    send_emails: bool = False             # if False, create drafts only
    to_addresses: list[str] | None = None  # override recipients
    providers: Any = None                 # calorch.providers.ProviderBundle (free sources: FRED, iXBRL, EFTS)
    cik_lookup: Any = None                # callable(ticker) -> CIK (for iXBRL/EFTS enrichment)
    blob_store: Any = None                # calorch.blob_store.BlobStore (or NullBlobStore)

    def out(self, name: str) -> Path:
        p = self.output_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


_CTX: Context | None = None


def set_context(ctx: Context) -> None:
    global _CTX
    _CTX = ctx


def _ctx(config: RunnableConfig | None = None) -> Context:
    """Return the runtime Context.

    If a LangGraph ``RunnableConfig`` is passed and contains a
    ``context`` key in ``configurable``, that takes precedence. Otherwise
    falls back to the module-level global (set by ``set_context``).

    This dual-source design allows a gradual migration from the global to
    config-injected context without breaking either path.
    """
    if config is not None:
        cfg = config.get("configurable", {}) if isinstance(config, dict) else {}
        ctx = cfg.get("context")
        if isinstance(ctx, Context):
            return ctx
    if _CTX is not None:
        return _CTX
    raise OrchestratorError(
        "Context not initialised. Call calorch.nodes.set_context(ctx) or "
        "pass it via configurable['context'] before invoking the graph."
    )


# ---------------------------------------------------------------------------
# Calendar stage
# ---------------------------------------------------------------------------
def scan_calendar(state: OrchestratorState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Pull events from Microsoft Graph API for the requested window.

    SEC EDGAR is NOT a calendar source.  SEC data is fetched during the
    enrichment phase (``prepare_event``) via the provider layer
    (iXBRL segments, EFTS guidance, ticker‑to‑CIK resolution).
    """
    c = _ctx(config)
    start = state["window_start"]
    end = state["window_end"]
    with start_span(
        "calorch.node.scan_calendar",
        window_start=start.isoformat(),
        window_end=end.isoformat(),
    ) as span:
        log.info("scanning calendar %s → %s", start, end)
        raw = c.graph.list_events(start, end)
        events = [to_calendar_event(r) for r in raw]
        log.info("found %d events", len(events))
        span.set_attribute("event_count", len(events))
        return {"raw_events": raw, "events": events, "log": [f"scan_calendar: {len(events)} events"]}


# ---------------------------------------------------------------------------
# Classification stage
# ---------------------------------------------------------------------------
def _keywords() -> dict[EventType, tuple[str, ...]]:
    """Pass-1 keywords, declared per agent in calorch.agents modules."""
    from calorch.agents import classification_keywords

    return classification_keywords()


def _keyword_score(blob: str) -> tuple[EventType, int, dict[EventType, int]]:
    counts: dict[EventType, int] = {}
    for ev, kws in _keywords().items():
        c = sum(blob.count(k) for k in kws)
        if c:
            counts[ev] = c
    if not counts:
        return EventType.UNKNOWN, 0, counts
    best, hits = max(counts.items(), key=lambda kv: kv[1])
    return best, hits, counts


def prefilter_keywords(state: OrchestratorState) -> dict[str, Any]:
    """Pass 1 — fast, deterministic, zero-cost label.

    For SEC-sourced events (those carrying ``_form`` in the raw payload),
    the form code itself is the strongest signal, so we use it directly.
    """
    with start_span(
        "calorch.node.prefilter_keywords",
        event_count=len(state.get("events", [])),
    ):
        # Index raw events by id for SEC form lookup
        raw_by_id: dict[str, dict[str, Any]] = {r["id"]: r for r in state.get("raw_events", [])}

        results: dict[str, ClassificationResult] = {}
        for ev in state["events"]:
            raw = raw_by_id.get(ev.id) or {}
            # ---- SEC fast-path ----
            sec_form = raw.get("_form")
            sec_items = raw.get("_items", "")
            if sec_form:
                from calorch.sec import classify_form
                sec_label = classify_form(sec_form, items=sec_items)
                try:
                    label = EventType(sec_label)
                except ValueError:
                    label = EventType.UNKNOWN
                results[ev.id] = ClassificationResult(
                    event_id=ev.id,
                    pass1_label=label,
                    pass1_keyword_hits=10,  # strong hint
                    rationale=f"SEC form {sec_form} → {sec_label}",
                )
                continue
            # ---- Outlook / generic fallback ----
            blob = f"{ev.subject}\n{ev.body_preview}\n{ev.location}".lower()
            label, hits, _ = _keyword_score(blob)
            results[ev.id] = ClassificationResult(
                event_id=ev.id,
                pass1_label=label,
                pass1_keyword_hits=hits,
            )
        return {
            "classifications": results,
            "log": [f"prefilter_keywords: {len(results)} events scored"],
        }


def llm_classify(state: OrchestratorState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Pass 2 — LLM classification (model-agnostic, no structured-output requirement).

    Instead of requiring ``with_structured_output`` / JSON mode (which DeepSeek
    and other providers may reject), we ask the LLM to output a JSON object in
    its plain text response and parse it out.  Falls back to Pass 1 keyword
    hints on any failure.
    """
    c = _ctx(config)
    results: dict[str, ClassificationResult] = dict(state.get("classifications", {}))
    with start_span("calorch.node.llm_classify", event_count=len(state["events"])) as span:
        for ev in state["events"]:
            prev = results.get(ev.id) or ClassificationResult(event_id=ev.id)
            # Trust the SEC form hint
            if prev.pass1_keyword_hits >= 10 and "SEC form" in (prev.rationale or ""):
                out = ClassificationResult(
                    event_id=ev.id,
                    pass1_label=prev.pass1_label,
                    pass1_keyword_hits=prev.pass1_keyword_hits,
                    final_label=prev.pass1_label,
                    confidence=0.95,
                    rationale=prev.rationale + " (trusted)",
                    routed_node=prev.pass1_label.value,
                )
                results[ev.id] = out
                continue
            blob = f"{ev.subject}\n{ev.body_preview}\nLocation: {ev.location}\n"
            hint_label = prev.pass1_label.value
            hint_hits = prev.pass1_keyword_hits

            system = (
                f"Classify this calendar event into exactly one of these types: "
                f"earnings_call, management_meeting, conference, kol_meeting, "
                f"channel_check, portfolio_meeting, internal_review, "
                f"analyst_meeting, unknown.\n"
                f"Keyword hint: {hint_label} (hits={hint_hits}).\n"
                f"Output ONLY a JSON object with fields: final_label (string), "
                f"confidence (0.0-1.0), rationale (short string).\n"
                f'Example: {{"final_label": "earnings_call", "confidence": 0.85, "rationale": "contains earnings keywords"}}'
            )
            user = f"Event:\n{blob}"

            try:
                with start_span(
                    "calorch.llm.invoke",
                    event_id=ev.id,
                    model=getattr(c.llm, "model_name", "unknown"),
                ):
                    from langchain_core.messages import SystemMessage, HumanMessage
                    resp = c.llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
                text = resp.content if hasattr(resp, "content") else str(resp)
                out = _parse_classification_json(text, ev.id, prev)
            except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
                log.warning("llm_classify network failure for %s: %s", ev.id, e)
                out = _pass1_fallback(ev.id, prev)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                log.warning("llm_classify parse failure for %s: %s", ev.id, e)
                out = _pass1_fallback(ev.id, prev)

            if not isinstance(out, ClassificationResult):
                out = ClassificationResult.model_validate({**out, "event_id": ev.id})
            out.event_id = ev.id
            out.routed_node = out.final_label.value
            results[ev.id] = out
    return {
        "classifications": results,
        "log": [f"llm_classify: classified {len(results)} events"],
    }


def _parse_classification_json(text: str, event_id: str, prev: ClassificationResult) -> ClassificationResult:
    """Extract and validate a ClassificationResult from raw LLM text."""
    import json, re

    # Try to extract the JSON object from the response
    # The model may wrap it in markdown fences or just output raw JSON
    cleaned = text.strip()
    # Remove markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the text with regex
        match = re.search(r"\{[^{}]*\}", cleaned)
        if match:
            try:
                raw = json.loads(match.group(0))
            except json.JSONDecodeError:
                return _pass1_fallback(event_id, prev)
        else:
            return _pass1_fallback(event_id, prev)

    # Validate and map fields
    label_str = str(raw.get("final_label", prev.pass1_label.value)).lower().strip()
    # Normalize to EventType
    label_map = {e.value: e for e in EventType}
    final_label = label_map.get(label_str, prev.pass1_label)

    confidence = float(raw.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    rationale = str(raw.get("rationale", f"Keyword hint: {prev.pass1_label.value}"))

    return ClassificationResult(
        event_id=event_id,
        pass1_label=prev.pass1_label,
        pass1_keyword_hits=prev.pass1_keyword_hits,
        final_label=final_label,
        confidence=confidence,
        rationale=rationale,
        routed_node=final_label.value,
    )


def _pass1_fallback(event_id: str, prev: ClassificationResult) -> ClassificationResult:
    return ClassificationResult(
        event_id=event_id,
        final_label=prev.pass1_label,
        confidence=0.4,
        rationale=f"LLM failed; using keyword hint ({prev.pass1_label.value})",
        routed_node=prev.pass1_label.value,
    )


# ---------------------------------------------------------------------------
# Routing / fan-out
# ---------------------------------------------------------------------------
def fan_out_prepare_events(state: OrchestratorState) -> list[Any] | str:
    """Return one agent subgraph per classified event.

    Each event is routed to a type-specific agent subgraph (e.g.
    ``agent_earnings_call``) via ``Send()``.  The subgraph runs the full
    preparation pipeline (data fetch → analysis → render) and returns
    per-event artifacts that the parent graph merges with its reducers.
    """
    from langgraph.types import Send

    from calorch.agents import get_agent

    sends: list[Send] = []
    for ev in state["events"]:
        cls = state["classifications"][ev.id]
        agent_node = get_agent(cls.final_label).node_name
        sends.append(
            Send(
                agent_node,
                {
                    "event": ev.model_dump(mode="json"),
                    "classification": cls.model_dump(mode="json"),
                    "run_id": state.get("run_id", ""),
                },
            )
        )
    return sends or "approval_gate"


# ---------------------------------------------------------------------------
# Preparation stage (parallel via Send).
# ---------------------------------------------------------------------------
def prepare_event(payload: dict[str, Any], config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    from calorch.state import CalendarEvent

    c = _ctx(config)
    ev = CalendarEvent.model_validate(payload["event"])
    cls = ClassificationResult.model_validate(payload["classification"])
    run_name = _safe_artifact_name(str(payload.get("run_id", "run")))
    event_name = _safe_artifact_name(ev.id)

    with start_span(
        "calorch.node.prepare_event",
        event_id=ev.id,
        event_type=cls.final_label.value,
        confidence=cls.confidence,
    ) as span:
        return _prepare_event_inner(
            c, ev, cls, run_name, event_name, span
        )


def _prepare_event_inner(c, ev, cls, run_name, event_name, span):
    log.info("prepare start event=%s type=%s conf=%.2f", ev.id, cls.final_label.value, cls.confidence)
    errors: list[str] = []
    documents: dict[str, DocxArtifact] = {}
    prepared_emails: dict[str, PreparedEmailArtifact] = {}
    calendar_links: dict[str, str] = {}
    log_lines: list[str] = []

    # --- 1) enterprise data ---
    payload_tickers = _tickers(ev.subject)
    if ev.sec_ticker:
        payload_tickers = [ev.sec_ticker]
    try:
        ed = c.enterprise.fetch(ev.subject, tickers=payload_tickers)
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("enterprise data network failure for %s: %s", ev.id, e)
        ed = {"source": "fallback-mock", "snapshots": {}, "as_of": _now().isoformat()}
        errors.append(f"enterprise_fetch:{ev.id}:{e!r}")
    except (ValueError, KeyError, TypeError) as e:
        log.warning("enterprise data parse failure for %s: %s", ev.id, e)
        ed = {"source": "fallback-mock", "snapshots": {}, "as_of": _now().isoformat()}
        errors.append(f"enterprise_fetch:{ev.id}:{e!r}")

    # --- 1b) persist input data to blob storage ---
    if c.blob_store:
        try:
            from calorch.blob_store import input_blob_path
            input_key = input_blob_path("enterprise", f"{run_name}/{event_name}")
            c.blob_store.upload_json("calorch-inputs", input_key, ed, metadata={"event_id": ev.id, "run_id": run_name})
        except (OSError, ValueError) as e:
            log.warning("blob input upload failed for %s: %s", ev.id, e)

    # --- 2) analysis & DOCX ---
    analysis = None
    try:
        analysis = build_analysis(
            cls.final_label, ev, cls, ed, c.llm,
            providers=c.providers, cik_lookup=c.cik_lookup,
        )
        analysis.confidence = cls.confidence
        doc_path = c.out(f"runs/{run_name}/docs/{event_name}.docx")
        render_docx(analysis, ev, doc_path)
        documents[ev.id] = DocxArtifact(
            event_id=ev.id,
            path=str(doc_path),
            sha256=sha256_file(doc_path),
            bytes=doc_path.stat().st_size,
        )
        # --- 2b) persist DOCX to blob storage ---
        if c.blob_store:
            try:
                from calorch.blob_store import output_blob_path
                doc_blob = output_blob_path(run_name, event_name, f"{event_name}.docx")
                blob_url = c.blob_store.upload_file(
                    "calorch-outputs", doc_blob, doc_path,
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    metadata={"event_id": ev.id, "run_id": run_name, "event_type": cls.final_label.value},
                )
                documents[ev.id] = DocxArtifact(
                    event_id=ev.id,
                    path=str(doc_path),
                    sha256=documents[ev.id].sha256,
                    bytes=documents[ev.id].bytes,
                    blob_url=blob_url,
                )
            except (OSError, ValueError) as e:
                log.warning("blob DOCX upload failed for %s: %s", ev.id, e)
        # --- 2c) persist analysis JSON to blob storage ---
        if c.blob_store and analysis is not None:
            try:
                from calorch.blob_store import output_blob_path
                analysis_blob = output_blob_path(run_name, event_name, f"{event_name}_analysis.json")
                analysis_dict = {
                    "event_id": analysis.event_id,
                    "event_type": analysis.event_type.value,
                    "title": analysis.title,
                    "sections": analysis.sections,
                    "tables": analysis.tables,
                    "tickers": analysis.tickers,
                    "source_attribution": analysis.source_attribution,
                    "role_focus": analysis.role_focus,
                    "confidence": analysis.confidence,
                    "data_sources": analysis.data_sources,
                }
                c.blob_store.upload_json(
                    "calorch-outputs", analysis_blob, analysis_dict,
                    metadata={"event_id": ev.id, "run_id": run_name, "event_type": cls.final_label.value},
                )
            except (OSError, ValueError) as e:
                log.warning("blob analysis JSON upload failed for %s: %s", ev.id, e)
    except (httpx.HTTPError, ConnectionError, TimeoutError, OSError) as e:
        log.exception("docx generation I/O failure for %s", ev.id)
        errors.append(f"docx:{ev.id}:{e!r}")
    except (ValueError, KeyError, TypeError) as e:
        log.exception("docx generation data failure for %s", ev.id)
        errors.append(f"docx:{ev.id}:{e!r}")

    # --- 3) OneDrive upload ---
    onedrive_url: str | None = None
    try:
        if ev.id in documents:
            onedrive_url = c.onedrive.upload(
                Path(documents[ev.id].path),
                remote_name=f"{run_name}-{event_name}.docx",
            )
            calendar_links[ev.id] = onedrive_url
    except (httpx.HTTPError, ConnectionError, TimeoutError, OSError) as e:
        log.warning("onedrive upload failed for %s: %s", ev.id, e)
        errors.append(f"onedrive:{ev.id}:{e!r}")
    # For SEC events the canonical link is the EDGAR document — prefer that.
    sec_link = ev.web_link or None

    # --- 4) HTML email preview ---
    try:
        if analysis is None:
            raise RuntimeError("analysis not generated — skipping email preview")
        # Prefer the EDGAR web link over the local OneDrive URL for SEC events
        link_for_email = sec_link or onedrive_url
        link_label = "View filing on EDGAR" if sec_link and not onedrive_url else "Open DOCX"
        html_body = render_html_email(analysis, ev, link_for_email, link_label=link_label)
        html_path = c.out(f"runs/{run_name}/emails/{event_name}.html")
        write_text(html_path, html_body)

        recipients = c.to_addresses or [a for a in ev.attendees if a] or ["research@firm.example"]
        subject = f"[{cls.final_label.value.replace('_', ' ').title()}] {ev.subject}"
        prepared_emails[ev.id] = PreparedEmailArtifact(
            event_id=ev.id,
            to=recipients,
            subject=subject,
            html_path=str(html_path),
            html=html_body,
            attachment_path=documents[ev.id].path if ev.id in documents else None,
            document_url=link_for_email,
        )
        # --- 4b) persist HTML email to blob storage ---
        if c.blob_store:
            try:
                from calorch.blob_store import output_blob_path
                email_blob = output_blob_path(run_name, event_name, f"{event_name}.html")
                email_blob_url = c.blob_store.upload_file(
                    "calorch-outputs", email_blob, html_path,
                    content_type="text/html",
                    metadata={"event_id": ev.id, "run_id": run_name},
                )
                prepared_emails[ev.id] = PreparedEmailArtifact(
                    event_id=ev.id,
                    to=recipients,
                    subject=subject,
                    html_path=str(html_path),
                    html=html_body,
                    attachment_path=documents[ev.id].path if ev.id in documents else None,
                    document_url=link_for_email,
                    blob_url=email_blob_url,
                )
            except (OSError, ValueError) as e:
                log.warning("blob HTML upload failed for %s: %s", ev.id, e)
    except (httpx.HTTPError, ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
        log.exception("email preview generation failed for %s", ev.id)
        errors.append(f"email_preview:{ev.id}:{e!r}")

    log_lines.append(
        f"prepare done event={ev.id} type={cls.final_label.value} "
        f"doc={'yes' if ev.id in documents else 'no'} "
        f"preview={'yes' if ev.id in prepared_emails else 'no'}"
    )
    span.set_attribute("errors", len(errors))
    span.set_attribute("has_doc", ev.id in documents)
    span.set_attribute("has_preview", ev.id in prepared_emails)

    return {
        "documents": documents,
        "prepared_emails": prepared_emails,
        "calendar_links": calendar_links,
        "errors": errors,
        "log": log_lines,
    }


# ---------------------------------------------------------------------------
# Approval stage — no side effects before interrupt().
# ---------------------------------------------------------------------------
def approval_gate(state: OrchestratorState) -> dict[str, Any]:
    """Pause a send run after previews exist and before external delivery."""
    send_emails = bool(state.get("send_emails", False))
    require_approval = bool(state.get("require_approval", False))
    with start_span(
        "calorch.node.approval_gate",
        send_emails=send_emails,
        require_approval=require_approval,
        preview_count=len(state.get("prepared_emails", {})),
    ) as span:
        if not send_emails or not require_approval:
            return {
                "delivery_approved": True,
                "approval_status": "not_required",
                "log": ["approval_gate: approval not required"],
            }

        from langgraph.types import interrupt

        decision = interrupt(
            {
                "question": "Approve sending the prepared research emails?",
                "run_id": state.get("run_id", ""),
                "event_count": len(state.get("prepared_emails", {})),
                "emails": [
                    {
                        "event_id": preview.event_id,
                        "to": preview.to,
                        "subject": preview.subject,
                        "html_path": preview.html_path,
                        "attachment_path": preview.attachment_path,
                    }
                    for preview in state.get("prepared_emails", {}).values()
                ],
            }
        )
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        status = "approved" if approved else "rejected"
        span.set_attribute("decision", status)
        return {
            "delivery_approved": approved,
            "approval_status": status,
            "log": [f"approval_gate: {status}"],
        }


def fan_out_delivery(state: OrchestratorState) -> list[Any] | str:
    """Dispatch approved previews to idempotent delivery branches."""
    from langgraph.types import Send

    if state.get("delivery_approved") is False:
        return "aggregate_briefing"

    events_by_id = {event.id: event for event in state.get("events", [])}
    sends: list[Send] = []
    for event_id, preview in state.get("prepared_emails", {}).items():
        event = events_by_id[event_id]
        cls = state["classifications"][event_id]
        document = state.get("documents", {}).get(event_id)
        sends.append(
            Send(
                "deliver_event",
                {
                    "event": event.model_dump(mode="json"),
                    "classification": cls.model_dump(mode="json"),
                    "preview": preview.model_dump(mode="json"),
                    "document": document.model_dump(mode="json") if document else None,
                    "onedrive_url": state.get("calendar_links", {}).get(event_id),
                    "run_id": state.get("run_id", ""),
                    "send_emails": bool(state.get("send_emails", False)),
                },
            )
        )
    return sends or "aggregate_briefing"


# ---------------------------------------------------------------------------
# Delivery stage (parallel via Send, after approval).
# ---------------------------------------------------------------------------
def deliver_event(payload: dict[str, Any], config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    from calorch.state import CalendarEvent

    c = _ctx(config)
    ev = CalendarEvent.model_validate(payload["event"])
    cls = ClassificationResult.model_validate(payload["classification"])
    preview = PreparedEmailArtifact.model_validate(payload["preview"])
    document = DocxArtifact.model_validate(payload["document"]) if payload.get("document") else None
    onedrive_url = payload.get("onedrive_url")
    send_emails = bool(payload.get("send_emails", False))
    run_id = str(payload.get("run_id", ""))
    delivery_key = f"{run_id}:{ev.id}"
    with start_span(
        "calorch.node.deliver_event",
        event_id=ev.id,
        event_type=cls.final_label.value,
        send_emails=send_emails,
    ) as span:
        return _deliver_event_inner(
            c, ev, cls, preview, document, onedrive_url, send_emails, run_id, delivery_key, span
        )


def _deliver_event_inner(c, ev, cls, preview, document, onedrive_url, send_emails, run_id, delivery_key, span):
    errors: list[str] = []
    emails: dict[str, EmailArtifact] = {}

    # A LangGraph resume or worker retry must not send the same message twice.
    existing = c.repo.get(ev.id)
    already_delivered = (
        existing
        and existing.get("delivery_key") == delivery_key
        and existing.get("email_status") in {"draft", "sent"}
        and existing.get("graph_message_id")
    )
    if already_delivered:
        emails[ev.id] = EmailArtifact(
            event_id=ev.id,
            to=preview.to,
            subject=preview.subject,
            html_path=preview.html_path,
            attachment_path=preview.attachment_path,
            status=existing["email_status"],
            graph_message_id=existing["graph_message_id"],
        )
        return {
            "emails": emails,
            "followups": [_followup_for(ev, cls)],
            "log": [f"deliver skipped duplicate event={ev.id} key={delivery_key}"],
        }

    attachment: tuple[str, bytes] | None = None
    if document and Path(document.path).exists():
        attachment = (f"{ev.id}.docx", Path(document.path).read_bytes())

    message_id = (
        existing.get("graph_message_id")
        if existing
        and existing.get("delivery_key") == delivery_key
        and existing.get("email_status") == "prepared"
        else None
    )
    repo_status = "failed"
    try:
        if not message_id:
            message_id = c.graph.create_draft(
                to=preview.to,
                subject=preview.subject,
                html=preview.html,
                attachment_b64=attachment,
            )
        if send_emails:
            # Persist the stable draft id before sending. If the worker dies
            # after Graph accepts /send, replaying that draft id is harmless.
            c.repo.upsert(
                ev.id,
                _delivery_record(
                    ev, cls, document, onedrive_url, delivery_key, "prepared", message_id
                ),
            )
            c.graph.send_draft(message_id)
            status = "sent"
            repo_status = "sent"
        else:
            status = "draft"
            repo_status = "draft"
        emails[ev.id] = EmailArtifact(
            event_id=ev.id,
            to=preview.to,
            subject=preview.subject,
            html_path=preview.html_path,
            attachment_path=preview.attachment_path,
            status=status,
            graph_message_id=message_id,
        )
    except (httpx.HTTPError, ConnectionError, TimeoutError, OSError) as e:
        log.exception("email delivery failed for %s", ev.id)
        errors.append(f"email:{ev.id}:{e!r}")
        status = "failed"
        repo_status = "prepared" if send_emails and message_id else "failed"

    sec_link = ev.web_link or None
    try:
        if onedrive_url or sec_link:
            link = onedrive_url or sec_link
            label = "Open DOCX" if onedrive_url else "View filing on EDGAR"
            body = {
                "body": {
                    "contentType": "HTML",
                    "content": f"<p>Brief ready: <a href=\"{link}\">{label}</a></p>",
                }
            }
            c.graph.patch_event(ev.id, body)
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("patch_event failed for %s: %s", ev.id, e)
        errors.append(f"patch_event:{ev.id}:{e!r}")

    try:
        c.repo.upsert(
            ev.id,
            _delivery_record(
                ev, cls, document, onedrive_url, delivery_key, repo_status, message_id
            ),
        )
    except (OSError, ValueError, KeyError) as e:
        log.warning("repo.upsert failed for %s: %s", ev.id, e)
        errors.append(f"repo:{ev.id}:{e!r}")

    span.set_attribute("errors", len(errors))
    span.set_attribute("email_status", status)
    return {
        "emails": emails,
        "followups": [_followup_for(ev, cls)],
        "errors": errors,
        "log": [f"deliver done event={ev.id} email={status}"],
    }


def _delivery_record(
    ev: Any,
    cls: ClassificationResult,
    document: DocxArtifact | None,
    onedrive_url: str | None,
    delivery_key: str,
    email_status: str,
    message_id: str | None,
) -> dict[str, Any]:
    return {
        "subject": ev.subject,
        "event_type": cls.final_label.value,
        "confidence": cls.confidence,
        "rationale": cls.rationale,
        "docx_path": document.path if document else "",
        "email_status": email_status,
        "graph_message_id": message_id,
        "delivery_key": delivery_key,
        "onedrive_url": onedrive_url,
        "calendar_event_id": ev.id,
        "when": ev.start.isoformat(),
    }


def _followup_for(ev: Any, cls: ClassificationResult) -> FollowUpItem:
    return FollowUpItem(
        event_id=ev.id,
        action=(
            "Send updated model after print" if cls.final_label is EventType.EARNINGS_CALL
            else "Update coverage note" if cls.final_label is EventType.MANAGEMENT_MEETING
            else "Log take-aways in weekly briefing" if cls.final_label is EventType.CONFERENCE
            else "Schedule follow-up with expert" if cls.final_label is EventType.KOL_MEETING
            else "Compile survey responses" if cls.final_label is EventType.CHANNEL_CHECK
            else "Update holdings & catalysts" if cls.final_label is EventType.PORTFOLIO_MEETING
            else "Refresh coverage universe" if cls.final_label is EventType.INTERNAL_REVIEW
            else "Add debate points to thesis"
        ),
        owner="research-analyst",
        due=ev.end + timedelta(days=2),
        notes=f"Auto-created from {cls.final_label.value} workflow",
    )


# Common false positives — words that look like tickers but aren't.
# Also includes SEC form codes and common business abbreviations.
_TICKER_FALSE_POSITIVES = frozenset({
    # Roles / people
    "CEO", "CFO", "CRO", "CTO", "CIO", "COO", "CMO", "CPO", "CSO", "CCO",
    "VP", "SVP", "EVP", "MD", "KOL", "IC",
    # Geography
    "USA", "US", "EU", "UK", "APAC", "EMEA", "LATAM", "CHINA", "JAPAN",
    # Industries / concepts
    "AI", "ML", "DL", "LLM", "GPU", "CPU", "TPU", "API", "SaaS", "PaaS", "IaaS",
    "ML", "RAG", "RL", "CV", "NLP", "AGI", "ASI",
    "EV", "AV", "ADAS", "OEM", "SEMI", "PCB", "IC", "SOC", "IP", "ASSP",
    "TMT", "HC", "FIG", "TECH", "FIN", "REIT",
    # Financial / regulatory
    "EPS", "EBIT", "EBITDA", "PEG", "NAV", "AUM", "IRR", "NPV", "DCF", "WACC",
    "YTD", "YOY", "QOQ", "TTM", "LTM", "NTM", "FCF", "OCI", "CAPEX", "OPEX",
    "OPEC", "ESG", "SEC", "EDGAR", "XBRL", "EFTS", "CIK",
    "IPO", "MNA", "SPAC", "LBO", "MBO", "RSU", "ESOP", "SOX",
    # Other
    "FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY26", "FY27", "FY28",
    "KCAL", "BIT", "CATL", "NXP",  # company names that aren't on our watchlist
})

# Valid tickers — built from the default watchlist + common additions.
_VALID_TICKERS = frozenset({
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "JPM", "TSLA", "WMT",
    "AMD", "CRM", "SNOW", "PLTR", "NOW", "UBER", "ORCL", "SHOP", "MRVL",
    "XOM", "CVX", "COP", "SLB", "F", "RIVN", "UNH",
    "V", "MA", "BAC", "WFC", "GS", "MS", "C", "BX", "SCHW",
    "LLY", "JNJ", "PFE", "ABBV", "MRK", "TMO", "DHR", "ABT", "BMY", "AMGN",
    "COST", "HD", "NKE", "SBUX", "MCD", "LOW", "TJX", "TGT",
    "CAT", "BA", "GE", "HON", "UPS", "RTX", "LMT", "DE", "MMM",
    "DIS", "NFLX", "CMCSA", "CHTR", "PARA", "WBD",
    "XLI", "XLK", "XLV", "XLF", "XLE", "XLY", "XLU", "XLP", "XLB", "XLC", "XLRE",
})


def _tickers(subject: str) -> list[str]:
    """Extract likely tickers from subject/body text.

    Only returns tickers that appear in ``_VALID_TICKERS`` to avoid
    false positives on words like "AI", "EV", "SVP", etc.
    """
    out: list[str] = []
    for tok in re.findall(r"\b[A-Z]{1,5}\b", subject):
        if tok in _TICKER_FALSE_POSITIVES:
            continue
        if tok not in _VALID_TICKERS:
            continue
        if tok not in out:
            out.append(tok)
    return out


def _safe_artifact_name(value: str) -> str:
    """Make untrusted event ids safe for local and OneDrive filenames."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "artifact"


# ---------------------------------------------------------------------------
# Weekly briefing
# ---------------------------------------------------------------------------
def aggregate_briefing(state: OrchestratorState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Cross-event aggregation — weekly briefing summary."""
    c = _ctx(config)
    with start_span(
        "calorch.node.aggregate_briefing",
        event_count=len(state.get("events", [])),
    ) as span:
        by_type: dict[str, int] = {}
        sent = 0
        drafts = 0
        failed: list[str] = []
        for ev_id, em in state.get("emails", {}).items():
            if em.status == "sent":
                sent += 1
            elif em.status == "draft":
                drafts += 1
            else:
                failed.append(ev_id)
        for cls in state.get("classifications", {}).values():
            by_type[cls.final_label.value] = by_type.get(cls.final_label.value, 0) + 1

        body = (
            f"Processed {len(state.get('events', []))} events "
            f"({sent} sent / {drafts} drafts / {len(failed)} failed). "
            f"Type mix: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
        )

        sections = [
            ("Executive summary", [body]),
            ("By event type", [f"• {k}: {v}" for k, v in sorted(by_type.items())]),
            ("Top follow-ups", [f"• {f.action} (owner: {f.owner})" for f in state.get("followups", [])[:5]]),
            ("Open issues", state.get("errors", []) or ["(none)"]),
        ]
        out_path = c.out("briefings/weekly.html")
        html = f"""<!doctype html><html><head><meta charset='utf-8'></head><body>
<h1>Weekly briefing &mdash; {state['window_start'].date()} to {state['window_end'].date()}</h1>
{''.join(f'<h2>{h}</h2><ul>{"".join(f"<li>{x}</li>" for x in items)}</ul>' for h, items in sections)}
</body></html>"""
        write_text(out_path, html)
        span.set_attribute("sent", sent)
        span.set_attribute("drafts", drafts)
        span.set_attribute("failed", len(failed))

        # --- persist briefing to blob storage ---
        briefing_blob_url = ""
        if c.blob_store:
            try:
                from calorch.blob_store import briefing_blob_path
                run_id = str(state.get("run_id", "run"))
                bpath = briefing_blob_path(run_id)
                briefing_blob_url = c.blob_store.upload_file(
                    "calorch-outputs", bpath, out_path,
                    content_type="text/html",
                    metadata={"run_id": run_id},
                )
            except (OSError, ValueError) as e:
                log.warning("blob briefing upload failed: %s", e)

        return {
            "weekly_briefing": WeeklyBriefing(
                week_start=state["window_start"],
                week_end=state["window_end"],
                sections=[{"heading": h, "body": "\n".join(items)} for h, items in sections],
                event_count=len(state.get("events", [])),
                path=str(out_path),
                blob_url=briefing_blob_url,
            ),
            "log": [f"aggregate_briefing: {body}"],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
