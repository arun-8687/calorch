"""Tools layer — Microsoft Graph, OneDrive, repository and enterprise data.

The orchestrator calls into this layer via thin adapters. In demo mode the
adapters return deterministic mock data so the graph runs end-to-end without
Azure or M365 credentials.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import quote

import httpx

from calorch.config import Settings
from calorch.state import CalendarEvent, OrchestratorError


# ---------------------------------------------------------------------------
# Microsoft Graph client (real)
# ---------------------------------------------------------------------------
class GraphClient(Protocol):
    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...
    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def send_mail(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> str: ...
    def create_draft(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> str: ...
    def send_draft(self, message_id: str) -> str: ...


def make_graph_client(settings: Settings) -> GraphClient:
    if settings.use_mocks:
        return MockGraphClient()
    _require_graph_settings(settings)
    return _GraphClientReal(settings)


class _GraphClientReal:
    """Thin client for the Graph REST API.

    Uses client-credentials flow with the configured Entra ID app registration.
    """

    _GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._token_lock = threading.Lock()
        self._http = httpx.Client(timeout=30.0)

    def _acquire_token(self) -> str:
        if self._token and time.time() < self._token_expires - 30:
            return self._token
        with self._token_lock:
            if self._token and time.time() < self._token_expires - 30:
                return self._token
            url = f"https://login.microsoftonline.com/{self._s.graph_tenant_id}/oauth2/v2.0/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": self._s.graph_client_id,
                "client_secret": self._s.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            }
            r = self._http.post(url, data=data)
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + int(body.get("expires_in", 3600))
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._acquire_token()}"}

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Call Graph with bounded retries for throttling and transient failures."""
        base_headers = kwargs.pop("headers", {})
        last_error: Exception | None = None
        for attempt in range(4):
            headers = {**self._headers(), **base_headers}
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == 3:
                    raise
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code == 401 and attempt < 3:
                self._token = None
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 8)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response
        raise OrchestratorError(f"Graph request failed after retries: {last_error!r}")

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        url = f"{self._GRAPH}/users/{self._s.graph_user_id}/calendar/calendarView"
        params = {
            "startDateTime": start.astimezone(timezone.utc).isoformat(),
            "endDateTime": end.astimezone(timezone.utc).isoformat(),
            "$select": "id,subject,bodyPreview,start,end,organizer,attendees,location,isOnlineMeeting,webLink",
            "$top": "100",
        }
        events: list[dict[str, Any]] = []
        while url:
            r = self._request("GET", url, params=params)
            payload = r.json()
            events.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink", "")
            params = None
        return events

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._GRAPH}/users/{self._s.graph_user_id}/events/{event_id}"
        r = self._request("PATCH", url, headers={"Content-Type": "application/json"}, json=body)
        return r.json()

    def _build_message(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        }
        if attachment_b64:
            name, b = attachment_b64
            msg["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "contentBytes": base64.b64encode(b).decode("ascii"),
                }
            ]
        return msg

    def send_mail(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> str:
        url = f"{self._GRAPH}/users/{self._s.graph_user_id}/sendMail"
        msg = self._build_message(to=to, subject=subject, html=html, attachment_b64=attachment_b64)
        self._request("POST", url, json={"message": msg, "saveToSentItems": True})
        return "sent:" + uuid.uuid4().hex

    def create_draft(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> str:
        url = f"{self._GRAPH}/users/{self._s.graph_user_id}/messages"
        msg = self._build_message(to=to, subject=subject, html=html, attachment_b64=attachment_b64)
        r = self._request("POST", url, json=msg)
        return r.json().get("id", "draft:" + uuid.uuid4().hex)

    def send_draft(self, message_id: str) -> str:
        url = f"{self._GRAPH}/users/{self._s.graph_user_id}/messages/{message_id}/send"
        try:
            self._request("POST", url)
        except httpx.HTTPStatusError as exc:
            # A persisted draft disappearing after /send is the expected replay
            # shape: Graph moved it to Sent Items before our repository update.
            if exc.response.status_code != 404:
                raise
        return message_id

    def upload_file(self, drive_id: str, local_path: Path, remote_name: str) -> str:
        """Upload a small artifact to a stable /calorch path in OneDrive."""
        folder_url = f"{self._GRAPH}/drives/{drive_id}/root:/calorch"
        try:
            self._request("GET", folder_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            self._request(
                "POST",
                f"{self._GRAPH}/drives/{drive_id}/root/children",
                json={
                    "name": "calorch",
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "replace",
                },
            )
        safe_name = quote(_safe_remote_name(remote_name), safe="")
        url = f"{self._GRAPH}/drives/{drive_id}/root:/calorch/{safe_name}:/content"
        r = self._request(
            "PUT",
            url,
            headers={"Content-Type": "application/octet-stream"},
            content=local_path.read_bytes(),
        )
        payload = r.json()
        return payload.get("webUrl") or payload.get("@microsoft.graph.downloadUrl") or url


# ---------------------------------------------------------------------------
# Microsoft Graph client (mock) — deterministic, no network.
# ---------------------------------------------------------------------------
class MockGraphClient:
    """In-memory stand-in for Microsoft Graph.

    Reads `data/seed_events.json` (if present) and returns copies of the
    fixture events, so the graph has something to chew on in demo mode.
    """

    def __init__(self, fixtures: list[dict[str, Any]] | None = None) -> None:
        self._fixtures = fixtures if fixtures is not None else _load_default_fixtures()
        self._sent: list[dict[str, Any]] = []
        self._drafts: list[dict[str, Any]] = []
        self._patches: dict[str, dict[str, Any]] = {}

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        out = []
        for ev in self._fixtures:
            ev_start = _parse_dt(ev["start"])
            ev_end = _parse_dt(ev["end"])
            if ev_start < end and ev_end > start:
                out.append(json.loads(json.dumps(ev)))  # deep copy
        return out

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._patches.setdefault(event_id, {}).update(body)
        return {"id": event_id, **body}

    def send_mail(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> str:
        mid = "sent:" + uuid.uuid4().hex
        self._sent.append({"id": mid, "to": to, "subject": subject, "html": html, "ts": time.time()})
        return mid

    def create_draft(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        attachment_b64: tuple[str, bytes] | None,
    ) -> str:
        mid = "draft:" + uuid.uuid4().hex
        self._drafts.append({"id": mid, "to": to, "subject": subject, "html": html, "ts": time.time()})
        return mid

    def send_draft(self, message_id: str) -> str:
        for draft in self._drafts:
            if draft["id"] == message_id:
                self._sent.append({**draft, "id": message_id, "ts": time.time()})
                self._drafts.remove(draft)
                return message_id
        # Treat a repeated send as an idempotent replay.
        if any(sent["id"] == message_id for sent in self._sent):
            return message_id
        raise KeyError(f"draft {message_id!r} not found")

    # -- inspection helpers for tests/demo --
    @property
    def sent(self) -> list[dict[str, Any]]:
        return list(self._sent)

    @property
    def drafts(self) -> list[dict[str, Any]]:
        return list(self._drafts)

    @property
    def patches(self) -> dict[str, dict[str, Any]]:
        return dict(self._patches)


def _parse_dt(s: Any) -> datetime:
    """Parse a Microsoft Graph date-time value (string or {dateTime: ...})."""
    if isinstance(s, dict):
        s = s.get("dateTime") or s.get("dateTimeOffset") or s.get("date_time") or ""
    if not isinstance(s, str):
        raise TypeError(f"Cannot parse datetime from {s!r}")
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _load_default_fixtures() -> list[dict[str, Any]]:
    # Walk up from src/calorch/tools.py to find the data/ directory in the
    # project root, regardless of the current working directory.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data" / "seed_events.json"
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return []


# ---------------------------------------------------------------------------
# OneDrive
# ---------------------------------------------------------------------------
def make_onedrive_client(settings: Settings) -> "OneDriveClient":
    if settings.use_mocks or not settings.onedrive_drive_id:
        return LocalOneDriveClient(settings.output_dir / "onedrive")
    _require_graph_settings(settings)
    return GraphOneDriveClient(settings)


class OneDriveClient(Protocol):
    def upload(self, local_path: Path, remote_name: str) -> str:
        """Returns a web URL the recipient can open."""


class LocalOneDriveClient:
    """Writes uploads to ./out/onedrive and returns a `file://` URL.

    In production this would `PUT /drives/{id}/items/.../content` and return
    a `https://*.sharepoint.com/...` sharing URL.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def upload(self, local_path: Path, remote_name: str) -> str:
        dest = self._root / _safe_remote_name(remote_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return dest.resolve().as_uri()


class GraphOneDriveClient:
    """OneDrive archive backed by the Microsoft Graph drive API."""

    def __init__(self, settings: Settings) -> None:
        self._drive_id = settings.onedrive_drive_id or ""
        self._graph = _GraphClientReal(settings)

    def upload(self, local_path: Path, remote_name: str) -> str:
        return self._graph.upload_file(self._drive_id, local_path, remote_name)


# ---------------------------------------------------------------------------
# Repository (JSON or Cosmos-shaped)
# ---------------------------------------------------------------------------
class Repository(Protocol):
    def upsert(self, event_id: str, doc: dict[str, Any]) -> None: ...
    def all(self) -> list[dict[str, Any]]: ...
    def get(self, event_id: str) -> dict[str, Any] | None: ...


class JsonRepository:
    """Thread-safe file-backed JSON repository.

    Parallel per-event workers (LangGraph Send fan-out) all hit this
    instance concurrently, so a single lock guards read-modify-write.
    For Cosmos, the SDK handles concurrency on the server.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("[]", encoding="utf-8")

    def _write_locked(self, rows: list[dict[str, Any]]) -> None:
        """Write rows to file. Caller must hold self._lock."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    def upsert(self, event_id: str, doc: dict[str, Any]) -> None:
        """Thread-safe read-modify-write. Lock held for the entire transaction."""
        with self._lock:
            rows = json.loads(self._path.read_text(encoding="utf-8") or "[]")
            for i, r in enumerate(rows):
                if r.get("event_id") == event_id:
                    rows[i] = {**r, **doc, "event_id": event_id, "updated_at": _now().isoformat()}
                    self._write_locked(rows)
                    return
            rows.append({**doc, "event_id": event_id, "updated_at": _now().isoformat()})
            self._write_locked(rows)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(self._path.read_text(encoding="utf-8") or "[]")

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        for r in rows:
            if r.get("event_id") == event_id:
                return r
        return None


class CosmosRepository:
    """Cosmos DB repository using ``event_id`` as the partition key."""

    def __init__(self, endpoint: str, key: str, db: str, container: str) -> None:
        if not endpoint or not key:
            raise OrchestratorError("COSMOS_ENDPOINT and COSMOS_KEY are required for REPO_BACKEND=cosmos.")
        try:
            from azure.cosmos import CosmosClient
        except ImportError as exc:  # pragma: no cover - exercised in deployed image
            raise OrchestratorError(
                "REPO_BACKEND=cosmos requires the `azure-cosmos` package."
            ) from exc
        client = CosmosClient(endpoint, credential=key)
        database = client.get_database_client(db)
        self._container = database.get_container_client(container)

    def upsert(self, event_id: str, doc: dict[str, Any]) -> None:
        existing = self.get(event_id) or {}
        self._container.upsert_item(
            {
                **existing,
                **doc,
                "id": event_id,
                "event_id": event_id,
                "updated_at": _now().isoformat(),
            }
        )

    def all(self) -> list[dict[str, Any]]:
        return list(
            self._container.query_items(
                query="SELECT * FROM c",
                enable_cross_partition_query=True,
            )
        )

    def get(self, event_id: str) -> dict[str, Any] | None:
        try:
            return self._container.read_item(item=event_id, partition_key=event_id)
        except Exception as exc:
            try:
                from azure.cosmos.exceptions import CosmosResourceNotFoundError
            except ImportError:  # pragma: no cover - import already validated
                raise
            if isinstance(exc, CosmosResourceNotFoundError):
                return None
            raise


def make_repository(settings: Settings) -> Repository:
    if settings.repo_backend == "cosmos" and not settings.use_mocks:
        return CosmosRepository(
            settings.cosmos_endpoint or "",
            settings.cosmos_key or "",
            settings.cosmos_db,
            settings.cosmos_container,
        )
    return JsonRepository(settings.repo_path)


# ---------------------------------------------------------------------------
# Enterprise data (FactSet / Bloomberg / LSEG / S&P)
# ---------------------------------------------------------------------------
class EnterpriseDataClient(Protocol):
    def fetch(self, topic: str, *, tickers: Iterable[str] = ()) -> dict[str, Any]: ...


@dataclass
class _FactSetFacts:
    consensus: dict[str, Any]
    guidance: str
    transcript_excerpt: str


class _EnterpriseDataClientImpl:
    """Consolidated FactSet/Bloomberg/LSEG/S&P/SEC adapter.

    In demo mode it returns deterministic synthetic data shaped like what a
    real provider would return. In production each branch hits its SDK
    (Open:FactSet, BLPAPI, lseg-data, Xpressfeed) and the `httpx` async
    client is used for HTTP fallbacks. SEC XBRL companyfacts are merged
    in whenever a real SEC client is available, so the briefing table
    always carries actual revenue / EPS / net income.
    """

    def __init__(self, settings: Settings, sec: Any = None) -> None:
        self._s = settings
        self._sec = sec
        has_real_source = bool(
            settings.factset_api_key
            or settings.bloomberg_blpapi_host
            or settings.lseg_client_id
            or settings.spglobal_api_key
            or sec is not None
        )
        self._mock = settings.use_mocks or not has_real_source

    def fetch(self, topic: str, *, tickers: Iterable[str] = ()) -> dict[str, Any]:
        tickers = [t.upper() for t in tickers] or ["AAPL", "MSFT"]
        # Pull real SEC XBRL companyfacts first; fall back to mock if unavailable.
        sec_snap: dict[str, dict[str, Any]] = {}
        if self._sec is not None:
            for t in tickers:
                try:
                    facts = self._sec.latest_financials(t)
                    if facts:
                        sec_snap[t] = facts
                except Exception:
                    continue
        if self._mock and not sec_snap:
            return self._mock_payload(topic, tickers)
        if sec_snap:
            return self._sec_payload(topic, tickers, sec_snap)
        return self._live_payload(topic, tickers)

    def _sec_payload(
        self, topic: str, tickers: list[str], sec_snap: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        snap: dict[str, dict[str, Any]] = {}
        for t, f in sec_snap.items():
            rev = f.get("revenue")
            ni = f.get("net_income")
            eps = f.get("eps_diluted")
            snap[t] = {
                "company": f.get("company"),
                "cik": f.get("cik"),
                "revenue": f"{int(rev):,}" if isinstance(rev, (int, float)) else "—",
                "revenue_period": f.get("revenue_period") or "—",
                "net_income": f"{int(ni):,}" if isinstance(ni, (int, float)) else "—",
                "eps_diluted": f"{eps:.2f}" if isinstance(eps, (int, float)) else "—",
                "form": f.get("eps_form") or f.get("revenue_form") or "—",
            }
        return {
            "source": "sec-edgar-xbrl",
            "topic": topic,
            "as_of": _now().isoformat(),
            "snapshots": snap,
            "guidance": "Per latest 10-K/10-Q on EDGAR; no forward guidance available.",
            "transcript_excerpt": (
                "Refer to the most recent 10-K Item 7 / 10-Q Item 2 MD&A for narrative commentary."
            ),
        }

    def _mock_payload(self, topic: str, tickers: list[str]) -> dict[str, Any]:
        snap = {
            t: {
                "price": round(50 + hash((t, topic)) % 400 + 0.0, 2),
                "consensus_eps_q": round(1.0 + (hash((t, "eps")) % 100) / 50, 2),
                "consensus_rev_q": round(1e9 + (hash((t, "rev")) % 50_000_000_000), 0),
                "fy1_pe": round(15 + (hash((t, "pe")) % 30), 1),
                "ytd_return": round(-10 + (hash((t, "ytd")) % 40), 1),
            }
            for t in tickers
        }
        return {
            "source": "mock-enterprise-data",
            "topic": topic,
            "as_of": _now().isoformat(),
            "snapshots": snap,
            "guidance": "Mgmt guides FY revenue +6-8% YoY; gross margin 100-150bps expansion.",
            "transcript_excerpt": (
                "We continue to see robust demand in our core franchise and remain on track "
                "to deliver the high end of our full-year guidance range."
            ),
        }

    def _live_payload(self, topic: str, tickers: list[str]) -> dict[str, Any]:
        # Each provider is hit conditionally. Only FactSet is shown; the
        # rest follow the same pattern (BLPAPI, lseg-data, Xpressfeed).
        out: dict[str, Any] = {"source": "live", "topic": topic, "as_of": _now().isoformat()}
        if self._s.factset_api_key:
            out["factset"] = self._factset(topic, tickers)
        if self._s.bloomberg_blpapi_host:
            out["bloomberg"] = self._bloomberg(topic, tickers)
        if self._s.lseg_client_id:
            out["lseg"] = self._lseg(topic, tickers)
        if self._s.spglobal_api_key:
            out["sp_capital_iq"] = self._spcapitaliq(topic, tickers)
        return out

    def _factset(self, topic: str, tickers: list[str]) -> dict[str, Any]:
        # Real call would be:
        #   from factset import FactSet
        #   client = FactSet(api_key=self._s.factset_api_key)
        #   return client.fundamentals(tickers, fields=("FE_EST_EPS", "FE_EST_REV"))
        return {"vendor": "factset", "endpoint": "Open:FactSet fundamentals", "tickers": tickers, "topic": topic}

    def _bloomberg(self, topic: str, tickers: list[str]) -> dict[str, Any]:
        return {"vendor": "bloomberg", "endpoint": "BLPAPI //BQL", "tickers": tickers, "topic": topic}

    def _lseg(self, topic: str, tickers: list[str]) -> dict[str, Any]:
        return {"vendor": "lseg", "endpoint": "Datastream RDTH", "tickers": tickers, "topic": topic}

    def _spcapitaliq(self, topic: str, tickers: list[str]) -> dict[str, Any]:
        return {"vendor": "sp_capital_iq", "endpoint": "Xpressfeed v3", "tickers": tickers, "topic": topic}


def make_enterprise_data_client(settings: Settings) -> EnterpriseDataClient:
    sec = None
    if settings.use_mocks is False or _env_true("USE_SEC", True):
        try:
            from calorch.sec import SecEdgarClient

            sec = SecEdgarClient(
                user_agent=settings.sec_user_agent,
                cache_dir=settings.sec_cache_dir,
            )
        except Exception:
            sec = None
    return _EnterpriseDataClientImpl(settings, sec=sec)


def _env_true(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _require_graph_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("GRAPH_TENANT_ID", settings.graph_tenant_id),
            ("GRAPH_CLIENT_ID", settings.graph_client_id),
            ("GRAPH_CLIENT_SECRET", settings.graph_client_secret),
            ("GRAPH_USER_ID", settings.graph_user_id),
        )
        if not value
    ]
    if missing:
        raise OrchestratorError(
            "Microsoft Graph configuration is incomplete: missing " + ", ".join(missing)
        )


def _safe_remote_name(remote_name: str) -> str:
    """Keep uploads in a single archive folder even for untrusted event ids."""
    name = Path(remote_name).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return safe or "artifact"


def to_calendar_event(raw: dict[str, Any]) -> CalendarEvent:
    return CalendarEvent(
        id=raw["id"],
        subject=raw.get("subject", "(no subject)"),
        body_preview=raw.get("bodyPreview", ""),
        start=_parse_dt(raw["start"]),
        end=_parse_dt(raw["end"]),
        organizer=(raw.get("organizer") or {}).get("emailAddress", {}).get("name", ""),
        attendees=[
            a.get("emailAddress", {}).get("address", "")
            for a in raw.get("attendees", [])
            if a.get("emailAddress", {}).get("address")
        ],
        location=(raw.get("location") or {}).get("displayName", ""),
        is_online=bool(raw.get("isOnlineMeeting")),
        web_link=raw.get("webLink", ""),
        sec_ticker=raw.get("_ticker"),
        sec_cik=raw.get("_cik"),
        sec_form=raw.get("_form"),
        sec_accession=raw.get("_accession"),
        sec_filing_date=raw.get("_filingDate"),
        sec_company=raw.get("_company"),
        sec_items=raw.get("_items"),
    )


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Re-exports for graph module
# ---------------------------------------------------------------------------
__all__ = [
    "GraphClient",
    "MockGraphClient",
    "make_graph_client",
    "make_sec_calendar_client",
    "make_cik_lookup",
    "OneDriveClient",
    "LocalOneDriveClient",
    "GraphOneDriveClient",
    "make_onedrive_client",
    "Repository",
    "JsonRepository",
    "CosmosRepository",
    "make_repository",
    "EnterpriseDataClient",
    "make_enterprise_data_client",
    "make_providers",
    "to_calendar_event",
    "sha256_file",
]


# ---------------------------------------------------------------------------
# SEC-backed calendar source
# ---------------------------------------------------------------------------
def make_sec_calendar_client(settings: "Settings"):  # type: ignore[name-defined]
    """Build an adapter that presents SEC EDGAR filings as calendar events.

    Imported lazily so the ``calorch.tools`` module does not have a hard
    runtime dependency on ``httpx`` for users only using mocks.
    """
    from calorch.sec import SecAsCalendarClient, SecEdgarClient

    sec = SecEdgarClient(
        user_agent=settings.sec_user_agent,
        cache_dir=settings.sec_cache_dir,
    )
    return SecAsCalendarClient(sec, watchlist=settings.sec_watchlist, forms=settings.sec_forms), sec


def make_cik_lookup(settings: "Settings"):
    """Build a callable ``cik_for(ticker) -> str | None``.

    This is the smallest piece of SEC we need to do ticker → CIK
    resolution for the free-source enrichments (iXBRL segments, EFTS
    guidance). Cached via the underlying ``SecEdgarClient``.
    """
    from calorch.sec import SecEdgarClient

    sec = SecEdgarClient(
        user_agent=settings.sec_user_agent,
        cache_dir=settings.sec_cache_dir,
    )
    return sec.cik_for


# ---------------------------------------------------------------------------
# Provider bundle — config-driven dispatch for data sources.
# ---------------------------------------------------------------------------
def make_providers(settings: Settings) -> "ProviderBundle":  # type: ignore[name-defined]
    """Build the active provider bundle for this run.

    Resolution order per provider:
      * Macro:     FRED (preferred, falls back to no-key) + FOMC H.15
      * Segments:  SEC iXBRL (real parser) or stub
      * Narrative: SEC EFTS (real search) or stub
      * Price:     stub only (no free enterprise-grade source)
      * Consensus: stub only (no free source — requires terminal)

    The bundle is cheap to construct (no network); the underlying clients
    cache per-run. Swap to a paid provider by setting the relevant env var.
    """
    from calorch.providers import build_providers as _build

    return _build(settings)
