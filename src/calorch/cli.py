"""Command-line entry point.

Usage:
    python -m calorch.cli run   --start 2026-03-02 --end 2026-03-08 [--send] [--interrupt]
    python -m calorch.cli serve [--port 8000]
    python -m calorch.cli summary
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, UTC
from pathlib import Path

from calorch.config import get_settings
from calorch.graph import make_graph
from calorch.llm import get_chat_model
from calorch.nodes import Context, set_context
from calorch.state import OrchestratorState
from calorch.tools import (
    make_cik_lookup,
    make_enterprise_data_client,
    make_graph_client,
    make_onedrive_client,
    make_providers,
    make_repository,
)


log = logging.getLogger("calorch.cli")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def build_context(*, send_emails: bool, output_dir: Path) -> Context:
    s = get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    from calorch.blob_store import make_blob_store
    blob = make_blob_store(
        connection_string=s.azure_storage_connection_string,
        account_url=s.azure_storage_account_url,
        local_root=s.blob_local_root,
        input_container=s.blob_input_container,
        output_container=s.blob_output_container,
    )
    from calorch.knowledge import make_knowledge_store
    ctx = Context(
        graph=make_graph_client(s),
        onedrive=make_onedrive_client(s),
        repo=make_repository(s),
        enterprise=make_enterprise_data_client(s),
        llm=get_chat_model(s),
        output_dir=output_dir,
        send_emails=send_emails,
        providers=make_providers(s),
        cik_lookup=make_cik_lookup(s),
        blob_store=blob,
        knowledge=make_knowledge_store(s),
        rag_top_k=s.rag_top_k,
        knowledge_writeback=s.knowledge_writeback,
    )
    set_context(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    s = get_settings()
    output_dir = Path(args.out or s.output_dir)
    build_context(send_emails=args.send, output_dir=output_dir)

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    graph = make_graph()
    initial: OrchestratorState = {
        "window_start": start,
        "window_end": end,
        "use_mocks": s.use_mocks,
        "run_id": datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ"),
        "send_emails": args.send,
        "require_approval": args.interrupt,
    }
    cfg = {"configurable": {"thread_id": args.thread_id or initial["run_id"]}}

    print(f"[calorch] window {start} -> {end}  send={args.send}  interrupt={args.interrupt}")
    result = graph.invoke(initial, config=cfg)
    if result.get("__interrupt__"):
        print("\n[calorch] paused for approval after preparing email previews")
        print(f"[calorch] thread_id={cfg['configurable']['thread_id']}")

    # Summary
    print("\n=== SUMMARY ===")
    print(f"events: {len(result.get('events', []))}")
    print("classifications:")
    for ev_id, cls in result.get("classifications", {}).items():
        print(f"  · {ev_id[:8]}…  {cls.final_label.value:<22}  conf={cls.confidence:.2f}")
    print("\ndocuments:")
    for d in result.get("documents", {}).values():
        print(f"  · {d.path}  ({d.bytes} bytes, sha256={d.sha256[:12]}…)")
    print("\nemails:")
    for em in result.get("emails", {}).values():
        print(f"  · {em.status:<6}  to={em.to}  subj={em.subject!r}")
    if result.get("weekly_briefing"):
        wb = result["weekly_briefing"]
        print(f"\nweekly briefing: {wb.path}")
    if result.get("errors"):
        print("\nerrors:")
        for e in result["errors"]:
            print(f"  ! {e}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    s = get_settings()
    repo = make_repository(s)
    rows = repo.all()
    print(f"Repository contains {len(rows)} events")
    for r in rows[:20]:
        print(f"  · {r.get('event_id','?')[:8]}…  {r.get('event_type','?')}  "
              f"docx={r.get('docx_path','—')[:60]}  email={r.get('email_status','—')}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # Lightweight: just import the langgraph CLI to host the graph.
    from langgraph.cli import main as lg_cli
    sys.argv = ["langgraph", "up", "--port", str(args.port)]
    raise SystemExit(lg_cli())


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="calorch", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="Run the orchestrator over a date window")
    pr.add_argument("--start", required=True, help="ISO date (inclusive)")
    pr.add_argument("--end", required=True, help="ISO date (exclusive)")
    pr.add_argument("--send", action="store_true", help="Send emails (default: drafts only)")
    pr.add_argument("--interrupt", action="store_true", help="Pause before send for human review")
    pr.add_argument("--out", help="Output directory (default: OUTPUT_DIR from .env)")
    pr.add_argument("--thread-id", help="Thread id for resumption")
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("summary", help="Show repository contents")
    ps.set_defaults(func=cmd_summary)

    ps2 = sub.add_parser("serve", help="Run langgraph dev server")
    ps2.add_argument("--port", type=int, default=8000)
    ps2.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    from calorch.logging_config import configure_logging

    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
