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
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from collections.abc import Iterable
from urllib.parse import quote

import httpx

from calorch.config import Settings

if TYPE_CHECKING:
    from calorch.providers import ProviderBundle
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
        # Stable per-logical-request id so a retried POST (e.g. createDraft) can
        # be deduplicated server-side rather than creating a duplicate.
        base_headers.setdefault("client-request-id", str(uuid.uuid4()))
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
                # Benign even without the token lock: a concurrent refresh just
                # re-acquires; worst case is one extra token fetch.
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
            "startDateTime": start.astimezone(UTC).isoformat(),
            "endDateTime": end.astimezone(UTC).isoformat(),
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

    Reads the packaged `calorch/data/seed_events.json` (if present) and returns copies of the
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
    # Seed events ship inside the package (pyproject package-data), so they
    # resolve from both a source checkout and a pip install.
    candidate = Path(__file__).resolve().parent / "data" / "seed_events.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return []


# ---------------------------------------------------------------------------
# OneDrive
# ---------------------------------------------------------------------------
def make_onedrive_client(settings: Settings) -> OneDriveClient:
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
# Repository (JSON for local/dev, Azure Table for production)
# ---------------------------------------------------------------------------
class Repository(Protocol):
    def upsert(self, event_id: str, doc: dict[str, Any]) -> None: ...
    def all(self) -> list[dict[str, Any]]: ...
    def get(self, event_id: str) -> dict[str, Any] | None: ...


class JsonRepository:
    """Thread-safe file-backed JSON repository.

    Parallel per-event workers (LangGraph Send fan-out) all hit this
    instance concurrently, so a single lock guards read-modify-write.
    For Azure Table, the service handles concurrency server-side.
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


# Table Storage keys forbid / \ # ? and control chars; sanitise event ids.
_TABLE_KEY_BAD = re.compile(r"[/\\#?\x00-\x1f\x7f-\x9f]")


def _table_key(event_id: str) -> str:
    return _TABLE_KEY_BAD.sub("_", event_id) or "_"


class TableRepository:
    """Azure Table Storage repository for delivery-idempotency records.

    One entity per event: ``PartitionKey = sanitised event_id``,
    ``RowKey = "delivery"``. Point reads/writes only — there is never more
    than one writer per event (each event is delivered by exactly one
    activity), so no cross-key contention. Backed by the function app's
    existing storage account; ~100x cheaper than Cosmos for this workload.

    Nested values are JSON-encoded into a single ``_doc`` column because
    Table entities are flat and typed.
    """

    _ROW_KEY = "delivery"

    def __init__(
        self,
        table_name: str,
        *,
        connection_string: str | None = None,
        account_url: str | None = None,
    ) -> None:
        try:
            from azure.data.tables import TableClient, UpdateMode
        except ImportError as exc:  # pragma: no cover - exercised in deployed image
            raise OrchestratorError(
                "REPO_BACKEND=table requires the `azure-data-tables` package "
                "(pip install calorch[azure])."
            ) from exc
        self._update_mode = UpdateMode.REPLACE

        if connection_string:
            self._table = TableClient.from_connection_string(connection_string, table_name=table_name)
        elif account_url:
            from azure.identity import DefaultAzureCredential

            self._table = TableClient(
                endpoint=account_url, table_name=table_name, credential=DefaultAzureCredential()
            )
        else:
            raise OrchestratorError(
                "REPO_BACKEND=table requires AZURE_STORAGE_CONNECTION_STRING or "
                "AZURE_STORAGE_ACCOUNT_URL."
            )
        try:
            self._table.create_table()
        except Exception:  # noqa: BLE001 - already exists
            pass

    def upsert(self, event_id: str, doc: dict[str, Any]) -> None:
        # Read-merge-replace is safe here because each event_id is written by
        # exactly one delivery activity (no concurrent writers to a key).
        existing = self.get(event_id) or {}
        merged = {**existing, **doc, "event_id": event_id, "updated_at": _now().isoformat()}
        entity = {
            "PartitionKey": _table_key(event_id),
            "RowKey": self._ROW_KEY,
            "_doc": json.dumps(merged, default=str),
        }
        self._table.upsert_entity(entity, mode=self._update_mode)

    def get(self, event_id: str) -> dict[str, Any] | None:
        try:
            from azure.core.exceptions import ResourceNotFoundError
        except ImportError:  # pragma: no cover
            ResourceNotFoundError = Exception  # type: ignore[assignment]
        try:
            entity = self._table.get_entity(partition_key=_table_key(event_id), row_key=self._ROW_KEY)
        except ResourceNotFoundError:
            return None
        raw = entity.get("_doc")
        return json.loads(raw) if raw else None

    def all(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entity in self._table.list_entities():
            raw = entity.get("_doc")
            if raw:
                rows.append(json.loads(raw))
        return rows


def make_repository(settings: Settings) -> Repository:
    if settings.repo_backend == "table" and not settings.use_mocks:
        return TableRepository(
            settings.repo_table_name,
            connection_string=settings.azure_storage_connection_string,
            account_url=settings.azure_storage_account_url,
        )
    return JsonRepository(settings.repo_path)


# ---------------------------------------------------------------------------
# Enterprise data (SEC EDGAR XBRL company facts)
# ---------------------------------------------------------------------------
class EnterpriseDataClient(Protocol):
    def fetch(self, topic: str, *, tickers: Iterable[str] = ()) -> dict[str, Any]: ...


class _EnterpriseDataClientImpl:
    """SEC EDGAR enterprise-data adapter.

    Pulls real SEC XBRL companyfacts (revenue / EPS / net income) whenever a
    SEC client is available, so the briefing table carries actual numbers; in
    demo mode (or when SEC is unavailable) it returns deterministic synthetic
    data of the same shape. The richer qualitative layer (guidance,
    transcripts, sentiment) comes from AlphaSense via the provider bundle, not
    this client.
    """

    def __init__(self, settings: Settings, sec: Any = None) -> None:
        self._s = settings
        self._sec = sec
        self._mock = settings.use_mocks or sec is None

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
        if sec_snap:
            return self._sec_payload(topic, tickers, sec_snap)
        return self._mock_payload(topic, tickers)

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
    return datetime.now(tz=UTC)


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
    "make_cik_lookup",
    "OneDriveClient",
    "LocalOneDriveClient",
    "GraphOneDriveClient",
    "make_onedrive_client",
    "Repository",
    "JsonRepository",
    "TableRepository",
    "make_repository",
    "EnterpriseDataClient",
    "make_enterprise_data_client",
    "make_providers",
    "to_calendar_event",
    "sha256_file",
]


def make_cik_lookup(settings: Settings):
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
def make_providers(settings: Settings) -> ProviderBundle:
    """Build the active provider bundle for this run.

    Sources:
      * SEC EDGAR  — fundamentals + segments (iXBRL), filing search (EFTS)
      * AlphaSense — narrative/guidance, transcripts/expert calls, sentiment

    The bundle is cheap to construct (no network); the underlying clients
    cache per-run. See ``calorch.providers.build_providers``.
    """
    from calorch.providers import build_providers as _build

    return _build(settings)
