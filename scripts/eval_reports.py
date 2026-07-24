"""CLI harness for scoring calorch prep-pack quality.

Builds an :class:`~calorch.analysis.EventAnalysis` for a fixed, offline
"golden set" -- one seed event per canonical :class:`~calorch.state.EventType`,
drawn from the packaged ``calorch/data/seed_events.json`` demo fixtures --
scores each with :func:`calorch.evaluation.score_report`, prints a markdown
summary + the aggregate, and writes a JSON report to
``OUTPUT_DIR/eval/report_<stamp>.json``.

Usage::

    python scripts/eval_reports.py [--output-dir DIR] [--stamp STAMP]

Exits non-zero if any golden-set report fails a deterministic check, so
this can gate CI.

By default this runs fully offline (``USE_MOCKS`` semantics): with no
Azure OpenAI / Opencode Go credentials in the environment,
``calorch.llm.get_chat_model()`` returns ``MockChatModel``, which this
script uses both as the report-builder LLM *and*, adapted to a plain
``str -> str`` callable, as the rubric judge. The mock model only emits a
keyword-classification JSON blob (it was built for the classifier, not
this harness), so with the default mock judge every rubric comes back as
``{"error": "judge JSON has no non-empty 'scores' object", ...}`` --
that failure is expected and documents the point: set
``AZURE_OPENAI_API_KEY``/``AZURE_OPENAI_ENDPOINT`` (or
``OPENCODE_GO_API_KEY``) so ``get_chat_model()`` returns a real chat
model before relying on the rubric scores this prints.

The golden set is also built with no provider bundle wired
(``providers=None``, ``cik_lookup=None``, matching how
``tests/test_agent_builders.py`` exercises the builders) so building it
never touches the network either. That means ``analysis.data_sources``
is empty for every golden report and the ``data_sources_present``
deterministic check fails for all of them by construction -- again,
expected for this offline demo, not a bug in the harness.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from calorch.analysis import build_analysis, tickers_from_subject
from calorch.config import get_settings
from calorch.evaluation import aggregate, score_report
from calorch.llm import get_chat_model
from calorch.state import ClassificationResult, EventType
from calorch.tools import MockGraphClient, make_enterprise_data_client, to_calendar_event

# One seed event per canonical event type -- see calorch/data/seed_events.json.
_GOLDEN_EVENT_TYPES: dict[str, EventType] = {
    "ev-001": EventType.EARNINGS_CALL,
    "ev-002": EventType.MANAGEMENT_MEETING,
    "ev-003": EventType.CONFERENCE,
    "ev-004": EventType.KOL_MEETING,
    "ev-005": EventType.CHANNEL_CHECK,
    "ev-006": EventType.PORTFOLIO_MEETING,
    "ev-007": EventType.INTERNAL_REVIEW,
    "ev-008": EventType.ANALYST_MEETING,
}


def _load_golden_events() -> list[tuple[Any, ClassificationResult]]:
    """(CalendarEvent, ClassificationResult) pairs for the golden set,
    reusing the packaged seed fixtures via MockGraphClient/to_calendar_event
    exactly as the demo/CLI harness does, with a fixed label per event
    (no classifier call needed -- the golden set's labels are ground
    truth by construction).
    """
    client = MockGraphClient()
    window_start = datetime(2026, 1, 1, tzinfo=UTC)
    window_end = datetime(2027, 1, 1, tzinfo=UTC)
    raw_by_id = {r["id"]: r for r in client.list_events(window_start, window_end)}

    pairs = []
    for event_id, event_type in _GOLDEN_EVENT_TYPES.items():
        raw = raw_by_id.get(event_id)
        if raw is None:
            continue
        ev = to_calendar_event(raw)
        cls = ClassificationResult(
            event_id=ev.id,
            pass1_label=event_type,
            pass1_keyword_hits=10,
            final_label=event_type,
            confidence=0.95,
            rationale="fixed golden-set label",
            routed_node=event_type.value,
        )
        pairs.append((ev, cls))
    return pairs


def build_golden_analyses(llm_call: Any, ed_client: Any) -> list[Any]:
    """Build one EventAnalysis per golden-set event."""
    analyses = []
    for ev, cls in _load_golden_events():
        payload_tickers = [ev.sec_ticker] if ev.sec_ticker else tickers_from_subject(ev.subject)
        ed = ed_client.fetch(ev.subject, tickers=payload_tickers)
        analysis = build_analysis(cls.final_label, ev, cls, ed, llm_call)
        analysis.confidence = cls.confidence
        analyses.append(analysis)
    return analyses


def _fmt_overall(rubric: dict[str, Any]) -> str:
    overall = rubric.get("overall") if isinstance(rubric, dict) else None
    if isinstance(overall, (int, float)) and not isinstance(overall, bool):
        return f"{overall:.1f}"
    return "n/a"


def print_summary(results: list[dict[str, Any]], agg: dict[str, Any]) -> None:
    print("| event_type | deterministic pass | overall |")
    print("| --- | --- | --- |")
    for r in results:
        print(
            f"| {r['event_type']} | {'yes' if r['deterministic_passed'] else 'no'} "
            f"| {_fmt_overall(r['rubric'])} |"
        )
    print()
    print("aggregate:")
    print(json.dumps(agg, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", default=None, help="Base output dir (default: OUTPUT_DIR setting)"
    )
    parser.add_argument(
        "--stamp",
        default="golden",
        help="Report-filename stamp, e.g. a date or run id (default: 'golden', for a "
        "deterministic filename)",
    )
    args = parser.parse_args(argv)

    # Force the enterprise-data client to its deterministic mock payload --
    # no SEC/AlphaSense network calls -- unless the caller already set these.
    os.environ.setdefault("USE_SEC", "false")
    os.environ.setdefault("USE_ALPHASENSE", "false")
    get_settings.cache_clear()

    settings = get_settings()
    output_dir = Path(args.output_dir) if args.output_dir else settings.output_dir

    llm = get_chat_model(settings)

    def judge_invoke(prompt: str) -> str:
        return llm.invoke(prompt).content

    ed_client = make_enterprise_data_client(settings)
    analyses = build_golden_analyses(llm, ed_client)

    results = [score_report(analysis, judge_invoke) for analysis in analyses]
    agg = aggregate(results)

    print_summary(results, agg)

    report_path = output_dir / "eval" / f"report_{args.stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({"results": results, "aggregate": agg}, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(f"\nwrote {report_path}")

    return 0 if all(r["deterministic_passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
