"""Runtime configuration loaded from environment variables.

Honours the contract in .env.example. The orchestrator falls back to mock
clients when credentials are absent and `USE_MOCKS=true` (default in demo).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv(name: str, default: str | None = None) -> list[str]:
    raw = _env(name, default)
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


@dataclass(frozen=True)
class Settings:
    azure_openai_api_key: str | None
    azure_openai_endpoint: str | None
    azure_openai_deployment: str
    azure_openai_api_version: str

    graph_tenant_id: str | None
    graph_client_id: str | None
    graph_client_secret: str | None
    graph_user_id: str

    onedrive_drive_id: str | None

    repo_backend: str            # "json" or "cosmos"
    repo_path: Path
    cosmos_endpoint: str | None
    cosmos_key: str | None
    cosmos_db: str
    cosmos_container: str

    factset_api_key: str | None
    bloomberg_blpapi_host: str | None
    lseg_client_id: str | None
    spglobal_api_key: str | None
    tiingo_api_key: str | None
    opencode_go_api_key: str | None
    opencode_go_model: str

    # Free data sources
    fred_api_key: str | None
    use_fred: bool
    use_ixbrl_segments: bool
    use_sec_efts: bool
    use_fed_h15: bool

    # SEC EDGAR
    sec_user_agent: str
    sec_cache_dir: Path
    sec_watchlist: list[str]
    sec_forms: list[str] | None

    use_mocks: bool
    output_dir: Path

    langsmith_api_key: str | None
    langsmith_project: str
    langsmith_tracing: bool
    checkpoint_postgres_uri: str | None
    calorch_api_key: str | None

    # Operational guards
    run_timeout_seconds: float
    max_concurrent_runs: int
    max_request_bytes: int
    cors_allowed_origins: list[str]
    rate_limit_per_minute: int
    # Azure Blob Storage
    azure_storage_connection_string: str | None
    azure_storage_account_url: str | None
    blob_input_container: str
    blob_output_container: str
    blob_local_root: Path | None  # LocalBlobStore root when Azure not configured

    audit_log_path: Path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        azure_openai_api_key=_env("AZURE_OPENAI_API_KEY"),
        azure_openai_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
        azure_openai_deployment=_env("AZURE_OPENAI_DEPLOYMENT", "gpt-4o") or "gpt-4o",
        azure_openai_api_version=_env("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
        or "2024-08-01-preview",
        graph_tenant_id=_env("GRAPH_TENANT_ID"),
        graph_client_id=_env("GRAPH_CLIENT_ID"),
        graph_client_secret=_env("GRAPH_CLIENT_SECRET"),
        graph_user_id=_env("GRAPH_USER_ID", "me") or "me",
        onedrive_drive_id=_env("ONEDRIVE_DRIVE_ID"),
        repo_backend=(_env("REPO_BACKEND", "json") or "json").lower(),
        repo_path=Path(_env("REPO_PATH", "./out/repository.json") or "./out/repository.json"),
        cosmos_endpoint=_env("COSMOS_ENDPOINT"),
        cosmos_key=_env("COSMOS_KEY"),
        cosmos_db=_env("COSMOS_DB", "calorch") or "calorch",
        cosmos_container=_env("COSMOS_CONTAINER", "events") or "events",
        factset_api_key=_env("FACTSET_API_KEY"),
        bloomberg_blpapi_host=_env("BLOOMBERG_BLPAPI_HOST"),
        lseg_client_id=_env("LSEG_CLIENT_ID"),
        spglobal_api_key=_env("SPGLOBAL_API_KEY"),
        tiingo_api_key=_env("TIINGO_API_KEY"),
        opencode_go_api_key=_env("OPENCODE_GO_API_KEY"),
        opencode_go_model=_env("OPENCODE_GO_MODEL", "glm-5.1") or "glm-5.1",
        fred_api_key=_env("FRED_API_KEY"),
        use_fred=_bool("USE_FRED", True),
        use_ixbrl_segments=_bool("USE_IXBRL_SEGMENTS", True),
        use_sec_efts=_bool("USE_SEC_EFTS", True),
        use_fed_h15=_bool("USE_FED_H15", True),
        sec_user_agent=_env("SEC_USER_AGENT", "Calorch Research calorch@example.com") or "Calorch Research calorch@example.com",
        sec_cache_dir=Path(_env("SEC_CACHE_DIR", "./.cache/sec") or "./.cache/sec"),
        sec_watchlist=_csv("SEC_WATCHLIST", "AAPL,MSFT,NVDA,GOOGL,AMZN,META,AVGO,JPM,TSLA,WMT"),
        sec_forms=_csv("SEC_FORMS", None) or None,
        use_mocks=_bool("USE_MOCKS", True),
        output_dir=Path(_env("OUTPUT_DIR", "./out") or "./out"),
        langsmith_api_key=_env("LANGSMITH_API_KEY"),
        langsmith_project=_env("LANGSMITH_PROJECT", "calorch") or "calorch",
        langsmith_tracing=_bool("LANGSMITH_TRACING", False),
        checkpoint_postgres_uri=_env("CHECKPOINT_POSTGRES_URI"),
        calorch_api_key=_env("CALORCH_API_KEY"),
        run_timeout_seconds=float(_env("RUN_TIMEOUT_SECONDS", "300") or "300"),
        max_concurrent_runs=int(_env("MAX_CONCURRENT_RUNS", "3") or "3"),
        max_request_bytes=int(_env("MAX_REQUEST_BYTES", "1048576") or "1048576"),
        cors_allowed_origins=_csv("CORS_ALLOWED_ORIGINS", ""),
        rate_limit_per_minute=int(_env("RATE_LIMIT_PER_MINUTE", "30") or "30"),
        audit_log_path=Path(_env("AUDIT_LOG_PATH", "./out/audit.jsonl") or "./out/audit.jsonl"),
        azure_storage_connection_string=_env("AZURE_STORAGE_CONNECTION_STRING"),
        azure_storage_account_url=_env("AZURE_STORAGE_ACCOUNT_URL"),
        blob_input_container=_env("BLOB_INPUT_CONTAINER", "calorch-inputs") or "calorch-inputs",
        blob_output_container=_env("BLOB_OUTPUT_CONTAINER", "calorch-outputs") or "calorch-outputs",
        blob_local_root=Path(_env("BLOB_LOCAL_ROOT", "./out/blobs")) if _env("BLOB_LOCAL_ROOT") else None,
    )
