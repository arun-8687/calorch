"""Azure Durable Functions — app entry point.

Timer-triggered orchestrator with LangGraph agents as activities.
"""
from __future__ import annotations

import os
from pathlib import Path

from calorch.config import get_settings
from calorch.nodes import Context, set_context
from calorch.tools import (
    make_cik_lookup,
    make_enterprise_data_client,
    make_graph_client,
    make_onedrive_client,
    make_providers,
    make_repository,
)
from calorch.llm import get_chat_model


def _build_context() -> Context:
    s = get_settings()
    out_dir = Path(os.getenv("OUTPUT_DIR", "./out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    from calorch.blob_store import make_blob_store
    blob = make_blob_store(
        connection_string=s.azure_storage_connection_string,
        account_url=s.azure_storage_account_url,
        local_root=s.blob_local_root,
    )
    ctx = Context(
        graph=make_graph_client(s),
        onedrive=make_onedrive_client(s),
        repo=make_repository(s),
        enterprise=make_enterprise_data_client(s),
        llm=get_chat_model(s),
        output_dir=out_dir,
        send_emails=False,
        providers=make_providers(s),
        cik_lookup=make_cik_lookup(s),
        blob_store=blob,
    )
    set_context(ctx)
    return ctx
