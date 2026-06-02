"""Real run against SEC EDGAR.

Pulls recent filings for a watchlist over a date window, runs the full
orchestrator, and writes DOCX + HTML + briefing + repository artefacts.

Usage:
    python scripts/run_sec.py                       # default: last 14 days
    python scripts/run_sec.py --days 7              # last 7 days
    python scripts/run_sec.py --start 2026-05-01 --end 2026-05-31
    python scripts/run_sec.py --watchlist AAPL,MSFT,NVDA
    python scripts/run_sec.py --send                # actually send emails
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from calorch.config import get_settings
from calorch.graph import make_graph
from calorch.llm import get_chat_model
from calorch.nodes import Context, set_context
from calorch.sec import SecEdgarClient
from calorch.state import OrchestratorState
from calorch.tools import (
    LocalOneDriveClient,
    JsonRepository,
    make_graph_client,
    make_sec_calendar_client,
    to_calendar_event,
    _EnterpriseDataClientImpl,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="ISO date (inclusive)")
    parser.add_argument("--end", help="ISO date (exclusive)")
    parser.add_argument("--days", type=int, default=14, help="if --start/--end not given, look back N days")
    parser.add_argument("--watchlist", default=None, help="comma-separated tickers (overrides env)")
    parser.add_argument("--forms", default=None, help="comma-separated form types (overrides env)")
    parser.add_argument("--send", action="store_true", help="Send emails (default: drafts only)")
    parser.add_argument("--out", default=None)
    parser.add_argument("--env", default=".env.sec", help="env file to load")
    args = parser.parse_args()

    if args.env and Path(args.env).exists():
        load_dotenv(args.env, override=True)
    get_settings.cache_clear()

    s = get_settings()
    if args.watchlist:
        s = s.__class__(**{**s.__dict__, "sec_watchlist": [t.strip() for t in args.watchlist.split(",") if t.strip()]})
        get_settings.cache_clear()
    if args.forms:
        s = s.__class__(**{**s.__dict__, "sec_forms": [t.strip() for t in args.forms.split(",") if t.strip()]})
        get_settings.cache_clear()

    out_dir = Path(args.out or s.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=args.days)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    log = logging.getLogger("calorch.sec-run")

    # ---- SEC client + adapter ----
    log.info("watchlist: %s", s.sec_watchlist)
    log.info("forms: %s", s.sec_forms or "(all)")
    sec = SecEdgarClient(user_agent=s.sec_user_agent, cache_dir=s.sec_cache_dir)
    calendar_adapter, _ = make_sec_calendar_client(s)

    # ---- enterprise data: real SEC XBRL only ----
    enterprise = _EnterpriseDataClientImpl(s, sec=sec)

    # ---- context (no Graph, no OneDrive) ----
    ctx = Context(
        graph=calendar_adapter,
        onedrive=LocalOneDriveClient(out_dir / "onedrive"),
        repo=JsonRepository(out_dir / "repository.json"),
        enterprise=enterprise,
        llm=get_chat_model(s),
        output_dir=out_dir,
        send_emails=args.send,
    )
    set_context(ctx)

    # ---- run ----
    graph = make_graph()
    initial: OrchestratorState = {
        "window_start": start,
        "window_end": end,
        "use_mocks": False,
        "run_id": "sec-" + datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "send_emails": args.send,
    }
    log.info("running orchestrator over %s → %s", start, end)
    result = graph.invoke(initial, config={"configurable": {"thread_id": initial["run_id"]}})

    # ---- summary ----
    events = result.get("events", [])
    classifications = result.get("classifications", {})
    documents = result.get("documents", {})
    emails = result.get("emails", {})
    followups = result.get("followups", [])
    errors = result.get("errors", [])

    log.info("processed %d SEC filings, %d DOCX, %d emails, %d follow-ups, %d errors",
             len(events), len(documents), len(emails), len(followups), len(errors))

    print("\n=== SEC RUN SUMMARY ===")
    print(f"window      : {start.date()} -> {end.date()}")
    print(f"filings     : {len(events)}")
    print(f"by form     :")
    by_form: dict[str, int] = {}
    for raw in result.get("raw_events", []):
        by_form[raw.get("_form", "?")] = by_form.get(raw.get("_form", "?"), 0) + 1
    for f, n in sorted(by_form.items(), key=lambda x: -x[1]):
        print(f"  · {f:<8}  {n}")
    print(f"\nclassifications:")
    for ev_id, cls in classifications.items():
        raw = next((r for r in result.get("raw_events", []) if r.get("id") == ev_id), {})
        ticker = raw.get("_ticker", "?")
        form = raw.get("_form", "?")
        items = raw.get("_items", "")
        print(f"  · {ticker:<6} {form:<8}  →  {cls.final_label.value:<22}  conf={cls.confidence:.2f}  items={items[:40]}")
    print(f"\ndocuments: {len(documents)}  emails: {len(emails)}  follow-ups: {len(followups)}  errors: {len(errors)}")
    if result.get("weekly_briefing"):
        print(f"briefing  : {result['weekly_briefing'].path}")

    # JSON dump for the run
    summary = {
        "run_id": initial["run_id"],
        "window": [start.isoformat(), end.isoformat()],
        "watchlist": s.sec_watchlist,
        "events": [e.model_dump(mode="json") for e in events],
        "classifications": {k: v.model_dump(mode="json") for k, v in classifications.items()},
        "documents": {k: v.model_dump(mode="json") for k, v in documents.items()},
        "emails": {k: v.model_dump(mode="json") for k, v in emails.items()},
        "followups": [f.model_dump(mode="json") for f in followups],
        "errors": errors,
        "weekly_briefing_path": result.get("weekly_briefing").path if result.get("weekly_briefing") else None,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s", out_dir / "summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
