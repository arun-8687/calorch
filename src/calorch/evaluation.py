"""Report-quality evaluation harness.

Two independent layers score an :class:`~calorch.analysis.EventAnalysis`
prep pack:

* **Deterministic checks** (:func:`deterministic_checks`) — cheap,
  no-LLM regex/structural scans that catch the concrete failure modes
  this codebase has hit before: unresolved ``{template}`` placeholders,
  bare "no data" dashes leaking into a rendered cell, known-fabricated
  strings from a past incident (a made-up analyst name, a made-up
  index level, ...), and tables carrying numbers with no traceable
  source.
* **LLM-judged rubric** (:func:`evaluate_analysis`) — asks a judge model
  (any ``str -> str`` callable) to grade the rendered pack against a
  fixed rubric (sourcing, trend accuracy, hallucination, guidance
  relevance, completeness, fabrication) and returns 1-5 scores.

:func:`score_report` combines both for one analysis; :func:`aggregate`
rolls a list of :func:`score_report` outputs up into summary stats
(used by ``scripts/eval_reports.py`` to gate CI on deterministic
failures and report mean rubric scores).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from calorch.analysis import EventAnalysis

# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
_BARE_DASH = {"—", "-"}

DEFAULT_BLOCKLIST: tuple[str, ...] = (
    "Dr. Sarah Chen",
    "8:00 PM IST",
    "S&P 500",
    "47 names",
    "12 initiations",
)
"""Known-fabricated strings from past incidents (invented KOL persona,
invented meeting time, a macro index/coverage-count nobody sourced)."""


def _table_pairing(
    analysis: EventAnalysis,
) -> list[tuple[str | None, dict[str, Any] | None, list[str] | None]]:
    """Walk ``analysis.sections`` in document order, pairing each
    ``__TABLE__`` sentinel section with the next unconsumed table --
    exactly the interleaving :mod:`calorch.renderers` uses for DOCX/HTML.

    Yields one entry per section as ``(heading, table, bullets)`` with
    exactly one of ``table``/``bullets`` non-``None``. Tables left over
    after every section has been walked (a template's metadata table is
    unconditionally prepended to ``analysis.tables`` with no paired
    section, and is consumed by whichever data section comes first --
    see the comment in ``calorch.renderers._render_html_email_inner``)
    are yielded afterwards as ``(None, table, None)``.
    """
    ti = 0
    entries: list[tuple[str | None, dict[str, Any] | None, list[str] | None]] = []
    for heading, items in analysis.sections:
        if items == ["__TABLE__"]:
            table = analysis.tables[ti] if ti < len(analysis.tables) else None
            ti += 1
            entries.append((heading, table, None))
        else:
            entries.append((heading, None, items))
    while ti < len(analysis.tables):
        entries.append((None, analysis.tables[ti], None))
        ti += 1
    return entries


def _text_locations(analysis: EventAnalysis) -> list[tuple[str, str]]:
    """``(location, text)`` pairs covering every piece of prose/label in
    the pack -- used by the placeholder and blocklist checks."""
    out: list[tuple[str, str]] = [("title", analysis.title)]
    for si, (heading, items) in enumerate(analysis.sections):
        out.append((f"section[{si}] heading", heading))
        if items != ["__TABLE__"]:
            for bi, bullet in enumerate(items):
                out.append((f"section[{si}] {heading!r} bullet[{bi}]", str(bullet)))
    for ti, table in enumerate(analysis.tables):
        tlabel = _table_label(ti, table)
        for h in table.get("headers") or []:
            out.append((f"{tlabel} header", str(h)))
        for ri, row in enumerate(table.get("rows") or []):
            for ci, cell in enumerate(row):
                out.append((f"{tlabel} row[{ri}] col[{ci}]", str(cell)))
        note = table.get("source_note")
        if note:
            out.append((f"{tlabel} source_note", str(note)))
    for di, src in enumerate(analysis.data_sources):
        for k in ("source_name", "status", "detail"):
            v = src.get(k)
            if v:
                out.append((f"data_sources[{di}].{k}", str(v)))
    return out


def _table_label(ti: int, table: dict[str, Any]) -> str:
    title = table.get("title")
    return f"table[{ti}] {title!r}" if title else f"table[{ti}]"


def _result(check: str, violations: list[str]) -> dict[str, Any]:
    return {
        "check": check,
        "passed": not violations,
        "detail": "ok" if not violations else "; ".join(violations),
    }


def _check_no_bare_dash(analysis: EventAnalysis) -> dict[str, Any]:
    """Flag rows whose value cells are ALL bare dashes (a fully-empty row that
    slipped past the engine's suppression). A "—" alongside real values is
    legitimate — e.g. no year-over-year figure for the oldest quarter shown in
    a multi-column trend table — so partial-dash rows are not violations."""
    violations = []
    for ti, table in enumerate(analysis.tables):
        tlabel = _table_label(ti, table)
        for ri, row in enumerate(table.get("rows") or []):
            # Value cells = everything past the first (label) column; a
            # single-column row is treated as its own value.
            value_cells = [str(c).strip() for c in (row[1:] if len(row) > 1 else row)]
            if not value_cells:
                continue
            has_real = any(v and v not in _BARE_DASH for v in value_cells)
            has_dash = any(v in _BARE_DASH for v in value_cells)
            if has_dash and not has_real:
                violations.append(f"{tlabel} row[{ri}] value cells are all bare dashes")
    return _result("no_bare_dash_cells", violations)


def _check_no_placeholders(analysis: EventAnalysis) -> dict[str, Any]:
    violations = [
        f"{loc}: unresolved placeholder in {text!r}"
        for loc, text in _text_locations(analysis)
        if _PLACEHOLDER_RE.search(text)
    ]
    return _result("no_unresolved_placeholders", violations)


def _check_no_blocklisted(analysis: EventAnalysis, blocklist: Sequence[str]) -> dict[str, Any]:
    violations = []
    for loc, text in _text_locations(analysis):
        for bad in blocklist:
            if bad in text:
                violations.append(f"{loc}: contains blocklisted string {bad!r}")
    return _result("no_blocklisted_fabrications", violations)


def _check_table_sourcing(analysis: EventAnalysis) -> dict[str, Any]:
    violations = []
    for heading, table, _bullets in _table_pairing(analysis):
        if table is None:
            continue
        has_numbers = any(
            re.search(r"\d", str(cell)) for row in (table.get("rows") or []) for cell in row
        )
        if not has_numbers:
            continue
        if table.get("source_note"):
            continue
        if heading and "source" in heading.lower():
            continue
        title = table.get("title")
        tlabel = title or heading or "(untitled table)"
        violations.append(
            f"table {tlabel!r} carries numeric data but has no source_note and its "
            f"section heading ({heading!r}) doesn't name a source"
        )
    return _result("tables_have_source_attribution", violations)


def _check_data_sources(analysis: EventAnalysis) -> dict[str, Any]:
    violations = [] if analysis.data_sources else ["analysis.data_sources is empty"]
    return _result("data_sources_present", violations)


def deterministic_checks(
    analysis: EventAnalysis, *, blocklist: Sequence[str] = DEFAULT_BLOCKLIST
) -> list[dict[str, Any]]:
    """Run every no-LLM check over ``analysis``.

    Returns one ``{check, passed, detail}`` dict per check category
    (five categories today). ``detail`` is ``"ok"`` when the check
    passes, else a ``"; "``-joined list of every violation found, each
    naming its location (section/table/row/col) so a failure is
    locatable without re-reading the whole pack.
    """
    return [
        _check_no_bare_dash(analysis),
        _check_no_placeholders(analysis),
        _check_no_blocklisted(analysis, blocklist),
        _check_table_sourcing(analysis),
        _check_data_sources(analysis),
    ]


# ---------------------------------------------------------------------------
# Rendering: EventAnalysis -> plain text (for the judge prompt)
# ---------------------------------------------------------------------------
def _render_table_lines(table: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    title = table.get("title")
    if title:
        lines.append(f"### {title}")
    headers = table.get("headers") or []
    if headers:
        lines.append("| " + " | ".join(str(h) for h in headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in table.get("rows") or []:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    note = table.get("source_note")
    if note:
        lines.append(f"_Source: {note}_")
    return lines


def render_analysis_text(analysis: EventAnalysis) -> str:
    """Flatten an :class:`EventAnalysis` into a plain-text, markdown-ish
    rendering: title, source attribution, then every section interleaved
    with its table (reusing the ``__TABLE__`` pairing logic), then any
    unpaired trailing tables, then the Data Sources list.
    """
    lines = [f"# {analysis.title}"]
    if analysis.source_attribution:
        lines.append(analysis.source_attribution)
    lines.append("")
    for heading, table, bullets in _table_pairing(analysis):
        if heading:
            lines.append(f"## {heading}")
        if table is not None:
            lines.extend(_render_table_lines(table))
        elif bullets:
            lines.extend(f"- {b}" for b in bullets)
        lines.append("")
    if analysis.data_sources:
        lines.append("## Data Sources")
        for s in analysis.data_sources:
            lines.append(f"- {s.get('source_name', '')}: {s.get('status', '')} — {s.get('detail', '')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# LLM-judged rubric
# ---------------------------------------------------------------------------
RUBRIC: list[dict[str, str]] = [
    {"key": "sourcing", "question": "Is every quantitative claim attributable to a stated source?"},
    {"key": "trend_accuracy", "question": "Do stated trends/deltas match the numbers in the tables?"},
    {"key": "no_hallucination", "question": "Any figure/claim not supported by the tables or excerpts?"},
    {"key": "guidance_relevance", "question": "Are guidance excerpts actually about forward guidance/outlook?"},
    {"key": "completeness", "question": "Does it cover the financials an analyst needs for this event type?"},
    {"key": "no_fabrication", "question": "Any invented person, firm, price, or macro figure?"},
]


def _judge_prompt(rendered: str, *, ticker: str, event_type: str) -> str:
    rubric_lines = "\n".join(f"- {c['key']}: {c['question']}" for c in RUBRIC)
    scores_example = "{" + ", ".join(f'"{c["key"]}": <1-5>' for c in RUBRIC) + "}"
    return (
        "You are grading an equity-research prep pack for factual quality.\n"
        f"Ticker: {ticker or '(none)'}. Event type: {event_type or '(unknown)'}.\n\n"
        "--- REPORT ---\n"
        f"{rendered}\n"
        "--- END REPORT ---\n\n"
        "Score the report on each of these criteria, from 1 (fails badly) to 5 "
        "(fully satisfies):\n"
        f"{rubric_lines}\n\n"
        "Respond with ONLY a JSON object of exactly this shape (no prose, no markdown "
        "code fences):\n"
        f'{{"scores": {scores_example}, '
        '"justifications": {"<key>": "<one sentence>", ...}, "overall": <1-5>}\n'
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _extract_balanced_json(text: str) -> str | None:
    """Scan for the first balanced ``{...}`` span, honouring quoted
    strings, so trailing/leading prose around the JSON (or nested
    objects like ``scores``) don't break extraction."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction: strip markdown fences, try a
    straight parse, then fall back to a balanced-brace scan so extra
    prose around the JSON doesn't break parsing. Never raises."""
    cleaned = _strip_fences(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for candidate_text in (cleaned, text):
        candidate = _extract_balanced_json(candidate_text)
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def evaluate_analysis(
    analysis: EventAnalysis, judge_invoke: Callable[[str], str], *, ticker: str = ""
) -> dict[str, Any]:
    """Ask ``judge_invoke`` to grade ``analysis`` against :data:`RUBRIC`.

    ``judge_invoke`` is a plain ``str -> str`` callable -- callers adapt
    whatever chat model they have (e.g. ``lambda p: model.invoke(p).content``).

    Returns ``{"scores": {key: 1-5, ...}, "justifications": {...}, "overall": 1-5}``
    on success. On any failure -- ``judge_invoke`` raising, the response not
    containing a parseable JSON object, or the JSON missing a usable
    ``scores`` object -- returns ``{"error": "...", "raw": "..."}`` and never
    raises.
    """
    event_type = getattr(analysis.event_type, "value", analysis.event_type)
    rendered = render_analysis_text(analysis)
    prompt = _judge_prompt(rendered, ticker=ticker, event_type=str(event_type))
    try:
        raw = judge_invoke(prompt)
    except Exception as e:  # judge_invoke is caller-supplied; never let it crash scoring
        return {"error": f"judge_invoke raised {e!r}"}
    if not isinstance(raw, str):
        raw = str(raw)

    parsed = _parse_json_object(raw)
    if parsed is None:
        return {"error": "could not parse a JSON object out of the judge response", "raw": raw}

    scores_raw = parsed.get("scores")
    if not isinstance(scores_raw, dict) or not scores_raw:
        return {"error": "judge JSON has no non-empty 'scores' object", "raw": raw}

    scores: dict[str, float] = {}
    for criterion in RUBRIC:
        key = criterion["key"]
        v = scores_raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            scores[key] = float(v)
    if not scores:
        return {"error": "judge JSON 'scores' had none of the rubric keys", "raw": raw}

    justifications_raw = parsed.get("justifications")
    justifications = (
        {str(k): str(v) for k, v in justifications_raw.items()}
        if isinstance(justifications_raw, dict)
        else {}
    )

    overall = parsed.get("overall")
    if not isinstance(overall, (int, float)) or isinstance(overall, bool):
        overall = sum(scores.values()) / len(scores)

    return {"scores": scores, "justifications": justifications, "overall": float(overall)}


# ---------------------------------------------------------------------------
# Combine + aggregate
# ---------------------------------------------------------------------------
def score_report(
    analysis: EventAnalysis,
    judge_invoke: Callable[[str], str],
    *,
    ticker: str = "",
    blocklist: Sequence[str] = DEFAULT_BLOCKLIST,
) -> dict[str, Any]:
    """Run the deterministic checks and the LLM rubric for one analysis."""
    checks = deterministic_checks(analysis, blocklist=blocklist)
    rubric = evaluate_analysis(analysis, judge_invoke, ticker=ticker)
    event_type = getattr(analysis.event_type, "value", analysis.event_type)
    resolved_ticker = ticker or (analysis.tickers[0] if analysis.tickers else "")
    return {
        "deterministic": checks,
        "deterministic_passed": all(c["passed"] for c in checks),
        "rubric": rubric,
        "ticker": resolved_ticker,
        "event_type": str(event_type),
    }


def aggregate(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Roll a list of :func:`score_report` outputs into summary stats:
    mean rubric score per criterion + mean overall (over reports whose
    rubric parsed successfully), and deterministic-check failure counts.
    """
    key_totals: dict[str, list[float]] = {}
    overall_totals: list[float] = []
    deterministic_failures = 0
    reports_with_deterministic_failures = 0
    rubric_errors = 0

    for r in results:
        checks = r.get("deterministic") or []
        failed = [c for c in checks if not c.get("passed", True)]
        deterministic_failures += len(failed)
        if failed:
            reports_with_deterministic_failures += 1

        rubric = r.get("rubric") or {}
        if not isinstance(rubric, dict) or "error" in rubric:
            rubric_errors += 1
            continue
        for key, value in (rubric.get("scores") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                key_totals.setdefault(key, []).append(float(value))
        overall = rubric.get("overall")
        if isinstance(overall, (int, float)) and not isinstance(overall, bool):
            overall_totals.append(float(overall))

    mean_scores = {k: sum(v) / len(v) for k, v in key_totals.items()}
    mean_overall = sum(overall_totals) / len(overall_totals) if overall_totals else None

    return {
        "n_reports": len(results),
        "mean_scores": mean_scores,
        "mean_overall": mean_overall,
        "deterministic_failures": deterministic_failures,
        "reports_with_deterministic_failures": reports_with_deterministic_failures,
        "rubric_errors": rubric_errors,
    }
