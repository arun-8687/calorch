"""Template engine — loads JSON templates and drives report generation.

All eight report types are defined as JSON templates in data/templates/.
This module resolves template variables, dispatches LLM calls, and
returns a structured EventAnalysis that the docx renderer can walk.

Usage:
    from calorch.templates import load_template, TemplateEngine
    tpl = load_template("earnings_call")
    analysis = TemplateEngine(tpl, llm_client=llm).build(
        context={"primary_ticker": "AAPL", "price": "$248.80"},
        data_tables={"last_quarter": {...}, "segments": [...]},
    )
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from calorch.llm_enrich import LlmEnricher, NoOpEnricher
from calorch.renderers import EventAnalysis
from calorch.state import EventType

log = logging.getLogger("calorch.templates")

# Resolve template directory from project root (not src/)
# __file__ is in src/calorch/, so go up two levels to project root
_TPL_DIR = Path(__file__).parent.parent.parent / "data" / "templates"


def load_template(event_type: str | EventType) -> dict[str, Any]:
    """Load a template JSON by event type name."""
    name = event_type.value if isinstance(event_type, EventType) else event_type
    path = _TPL_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


class TemplateEngine:
    """Builds an EventAnalysis from a JSON template + data + optional LLM."""

    def __init__(self, template: dict[str, Any], llm_client: Any | None = None) -> None:
        self._tpl = template
        self._enricher = LlmEnricher(llm_client) if llm_client else NoOpEnricher()

    def build(
        self,
        *,
        context: dict[str, Any],
        data_tables: dict[str, Any] | None = None,
        data_sources: list[dict[str, str]] | None = None,
    ) -> EventAnalysis:
        """Resolve the template into an EventAnalysis.

        Args:
            context: Flat dict of template variables (e.g. {"primary_ticker": "AAPL"}).
            data_tables: Dict of pre-built tables keyed by table id.
        """
        data_tables = data_tables or {}
        ev_type = self._tpl["event_type"]

        # Build title from template
        title_tpl = self._tpl.get("report_header", {}).get("title", "Brief")
        title = _fmt(title_tpl, context)

        a = EventAnalysis(
            event_id=context.get("event_id", ""),
            event_type=EventType(ev_type),
            title=title,
            tickers=context.get("tickers", []),
            confidence=context.get("confidence", 0.0),
            data_sources=data_sources or [],
        )

        # Metadata table (if present)
        meta = self._tpl.get("metadata_table")
        if meta:
            a.tables.append(self._build_meta_table(meta, context))

        # Walk sections
        for sec in self._tpl.get("sections", []):
            self._build_section(sec, a, context, data_tables)

        return a

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------
    def _build_section(
        self,
        sec: dict[str, Any],
        a: EventAnalysis,
        ctx: dict[str, Any],
        data_tables: dict[str, Any],
    ) -> None:
        source = sec.get("source", "static")
        sec_id = sec.get("id", "")

        if source == "llm":
            self._build_llm_section(sec, a, ctx)
        elif source == "data":
            self._build_data_section(sec, a, ctx, data_tables)
        elif source == "static":
            self._build_static_section(sec, a, ctx)
        elif source == "template":
            self._build_template_section(sec, a, ctx)

    def _build_llm_section(self, sec: dict[str, Any], a: EventAnalysis, ctx: dict[str, Any]) -> None:
        method = sec.get("llm_method", "enrich_headline")
        ticker = ctx.get("primary_ticker", "")
        addendum = sec.get("prompt_addendum", "")

        # Build LLM context
        llm_ctx = {k: v for k, v in ctx.items() if v is not None}
        if addendum:
            llm_ctx["_prompt_addendum"] = addendum

        enrich_fn = getattr(self._enricher, method, self._enricher.enrich_headline)
        bullets = enrich_fn(ticker=ticker, context=llm_ctx)

        if not bullets:
            fb = sec.get("fallback", [])
            bullets = [_fmt(b, ctx) for b in fb] if fb else []

        # Normalize bullets to strings (dicts from channel-check templates become formatted strings)
        str_bullets: list[str] = []
        for b in bullets:
            if isinstance(b, dict):
                # Format dict as a structured string (e.g. for channel check questions)
                parts = []
                for k, v in b.items():
                    if v:
                        parts.append(f"{k.capitalize()}: {v}")
                str_bullets.append(" | ".join(parts) if parts else str(b))
            else:
                str_bullets.append(str(b))
        if str_bullets:
            a.sections.append((_fmt(sec["title"], ctx), str_bullets))

    def _build_data_section(
        self,
        sec: dict[str, Any],
        a: EventAnalysis,
        ctx: dict[str, Any],
        data_tables: dict[str, Any],
    ) -> None:
        sec_title = _fmt(sec.get("title", ""), ctx)
        subtitle = _fmt(sec.get("subtitle", ""), ctx)
        rows_from = sec.get("rows_from")

        table_to_add: dict[str, Any] | None = None

        if rows_from and rows_from in data_tables:
            table_data = data_tables[rows_from]
            table_to_add = {
                "title": subtitle or "",
                "headers": table_data.get("headers", []),
                "rows": table_data.get("rows", []),
            }
        elif "rows" in sec:
            rows = []
            for row in sec["rows"]:
                label = _fmt(row.get("label", ""), ctx)
                value = _fmt(row.get("value", ""), ctx)
                if value and value != row.get("value", ""):
                    rows.append([label, value])
            if rows:
                table_to_add = {
                    "title": subtitle or "",
                    "headers": sec.get("headers", ["Metric", "Value"]),
                    "rows": rows,
                }
        elif "blank_rows" in sec:
            cols = len(sec.get("headers", []))
            blanks = [[""] * cols for _ in range(sec["blank_rows"])]
            table_to_add = {
                "title": subtitle or "",
                "headers": sec["headers"],
                "rows": blanks,
            }

        # Only add section heading if there's actual data to show.
        # The heading goes into the section as a sentinel — render_docx
        # will pair it with the table that immediately follows in the
        # tables list so they appear together.
        if table_to_add is not None and sec_title:
            a.sections.append((sec_title, ["__TABLE__"]))
            a.tables.append(table_to_add)

    def _build_static_section(self, sec: dict[str, Any], a: EventAnalysis, ctx: dict[str, Any]) -> None:
        content = sec.get("content", [])
        if content:
            a.sections.append((_fmt(sec["title"], ctx), [_fmt(c, ctx) for c in content]))
        # Handle subsections with questions
        subsections = sec.get("subsections", [])
        for sub in subsections:
            questions = sub.get("questions", [])
            if questions:
                a.sections.append((_fmt(sub["title"], ctx), [_fmt(q, ctx) for q in questions]))

    def _build_template_section(self, sec: dict[str, Any], a: EventAnalysis, ctx: dict[str, Any]) -> None:
        template = sec.get("template", "")
        items_key = sec.get("items_by_role", {})
        role = ctx.get("role", "default")
        items = items_key.get(role, items_key.get("default", []))
        content = [_fmt(template, {**ctx, "items": item}) for item in items]
        if content:
            a.sections.append((_fmt(sec["title"], ctx), content))

    def _build_meta_table(self, meta: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for row in meta.get("rows", []):
            label = _fmt(row.get("label", ""), ctx)
            value = _fmt(row.get("value", ""), ctx)
            if value and value != row.get("value", ""):
                rows.append([label, value])
        return {
            "title": "",
            "headers": meta.get("headers", ["Metric", "Value"]),
            "rows": rows,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _fmt(template: str, ctx: dict[str, Any]) -> str:
    """Format a template string with context variables.
    Missing keys are left as-is (e.g. '{missing}' stays literal)."""
    try:
        return template.format_map(_SafeDict(ctx))
    except Exception:
        return template


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
