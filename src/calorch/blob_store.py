"""Azure Blob Storage integration for input and output persistence.

Two blob categories:
  * **inputs/**  — raw provider responses (SEC EDGAR, AlphaSense)
  * **outputs/** — generated artefacts (DOCX, HTML, briefings)

Path conventions:
  inputs/{provider}/{key}.json       — e.g. inputs/sec/companyfacts/0000320193.json
  outputs/{run_id}/{event_id}/doc    — DOCX packet
  outputs/{run_id}/{event_id}/email  — HTML email
  outputs/{run_id}/briefing          — weekly briefing HTML

Three implementations:
  * ``AzureBlobStore``   — production; uses azure-storage-blob SDK
  * ``LocalBlobStore``   — dev/testing; mirrors blob paths on local disk
  * ``NullBlobStore``    — no-op; used when blob storage is not configured
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from calorch.telemetry import start_span

log = logging.getLogger("calorch.blob_store")

_BLOB_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class BlobStore(Protocol):
    # Configured container names so callers never hardcode literals.
    input_container: str
    output_container: str

    def upload_bytes(
        self,
        container: str,
        blob_name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        """Upload raw bytes. Returns the blob URL or local path."""
        ...

    def upload_json(
        self,
        container: str,
        blob_name: str,
        obj: Any,
        *,
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        """JSON-serialise *obj* and upload. Returns the blob URL or local path."""
        ...

    def upload_file(
        self,
        container: str,
        blob_name: str,
        local_path: Path,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        """Upload a local file. Returns the blob URL or local path."""
        ...

    def download_bytes(self, container: str, blob_name: str) -> bytes | None:
        """Download bytes. Returns None if blob does not exist."""
        ...

    def download_json(self, container: str, blob_name: str) -> Any | None:
        """Download and deserialise JSON. Returns None if blob does not exist."""
        ...

    def exists(self, container: str, blob_name: str) -> bool:
        """Check whether a blob exists."""
        ...

    def list_blobs(self, container: str, prefix: str = "") -> list[str]:
        """List blob names under *prefix*."""
        ...


# ---------------------------------------------------------------------------
# Azure Blob Storage (production)
# ---------------------------------------------------------------------------
class AzureBlobStore:
    """Azure Blob Storage client backed by ``azure-storage-blob``.

    Connection can be via:
      * Connection string  (``AZURE_STORAGE_CONNECTION_STRING``)
      * Account URL + DefaultAzureCredential (managed identity)
    """

    def __init__(
        self,
        *,
        connection_string: str | None = None,
        account_url: str | None = None,
        input_container: str = "calorch-inputs",
        output_container: str = "calorch-outputs",
    ) -> None:
        try:
            from azure.storage.blob import ContainerClient  # noqa: F401  (availability probe)
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise ImportError(
                "azure-storage-blob and azure-identity are required for "
                "AzureBlobStore. Install with: pip install calorch[azure]"
            ) from exc

        if connection_string:
            from azure.storage.blob import BlobServiceClient
            self._svc = BlobServiceClient.from_connection_string(connection_string)
        elif account_url:
            cred = DefaultAzureCredential()
            from azure.storage.blob import BlobServiceClient
            self._svc = BlobServiceClient(account_url, credential=cred)
        else:
            raise ValueError(
                "Either connection_string or account_url must be provided "
                "for AzureBlobStore."
            )

        self.input_container = input_container
        self.output_container = output_container
        self._input_container = input_container
        self._output_container = output_container
        self._containers_cache: set[str] = set()
        self._ensure_containers()

    def _container_client(self, container: str) -> Any:
        return self._svc.get_container_client(container)

    def _blob_client(self, container: str, blob_name: str) -> Any:
        self._ensure_container(container)
        return self._svc.get_blob_client(container=container, blob=blob_name)

    def _ensure_container(self, container: str) -> None:
        if container in self._containers_cache:
            return
        cc = self._container_client(container)
        try:
            cc.create_container()
        except Exception:
            pass  # already exists
        self._containers_cache.add(container)

    def _ensure_containers(self) -> None:
        for c in (self._input_container, self._output_container):
            self._ensure_container(c)

    def upload_bytes(
        self,
        container: str,
        blob_name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        with start_span(
            "calorch.blob.upload_bytes",
            container=container,
            blob_name=blob_name,
            size=len(data),
        ):
            bc = self._blob_client(container, blob_name)
            opts: dict[str, Any] = {"content_settings": {"content_type": content_type}}
            if metadata:
                opts["metadata"] = metadata
            if not overwrite:
                opts["validate_content"] = True
            bc.upload_blob(data, overwrite=overwrite, **{k: v for k, v in opts.items() if v})
            url = bc.url
            log.info("uploaded blob %s/%s (%d bytes)", container, blob_name, len(data))
            return url

    def upload_json(
        self,
        container: str,
        blob_name: str,
        obj: Any,
        *,
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        data = json.dumps(obj, indent=2, default=str, ensure_ascii=False).encode("utf-8")
        md = {"uploaded_at": datetime.now(tz=UTC).strftime(_BLOB_TIMESTAMP_FORMAT)}
        if metadata:
            md.update(metadata)
        return self.upload_bytes(
            container, blob_name, data,
            content_type="application/json", metadata=md, overwrite=overwrite,
        )

    def upload_file(
        self,
        container: str,
        blob_name: str,
        local_path: Path,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        with start_span(
            "calorch.blob.upload_file",
            container=container,
            blob_name=blob_name,
            path=str(local_path),
        ):
            data = local_path.read_bytes()
            md = {
                "uploaded_at": datetime.now(tz=UTC).strftime(_BLOB_TIMESTAMP_FORMAT),
                "original_filename": local_path.name,
            }
            if metadata:
                md.update(metadata)
            return self.upload_bytes(
                container, blob_name, data,
                content_type=content_type, metadata=md, overwrite=overwrite,
            )

    def download_bytes(self, container: str, blob_name: str) -> bytes | None:
        bc = self._blob_client(container, blob_name)
        try:
            return bc.download_blob().readall()
        except Exception as exc:
            import azure.storage.blob as _blob_mod
            import azure.core.exceptions as _core_exc
            if isinstance(exc, _core_exc.ResourceNotFoundError):
                return None
            if isinstance(exc, _blob_mod.CustomerFacingError):
                return None
            log.warning("download_bytes %s/%s failed: %s", container, blob_name, exc)
            return None

    def download_json(self, container: str, blob_name: str) -> Any | None:
        raw = self.download_bytes(container, blob_name)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def exists(self, container: str, blob_name: str) -> bool:
        bc = self._blob_client(container, blob_name)
        try:
            bc.get_blob_properties()
            return True
        except Exception:
            return False

    def list_blobs(self, container: str, prefix: str = "") -> list[str]:
        cc = self._container_client(container)
        return [b.name for b in cc.list_blobs(name_starts_with=prefix or None)]


# ---------------------------------------------------------------------------
# Local filesystem blob store (dev / testing)
# ---------------------------------------------------------------------------
class LocalBlobStore:
    """Mirrors blob paths on local disk under a root directory.

    Layout:  ``<root>/<container>/<blob_name>``
    """

    def __init__(
        self,
        root: Path,
        *,
        input_container: str = "calorch-inputs",
        output_container: str = "calorch-outputs",
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.input_container = input_container
        self.output_container = output_container

    def _path(self, container: str, blob_name: str) -> Path:
        p = self._root / container / blob_name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def upload_bytes(
        self,
        container: str,
        blob_name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        p = self._path(container, blob_name)
        if p.exists() and not overwrite:
            return p.as_uri()
        p.write_bytes(data)
        if metadata:
            meta_path = p.with_suffix(p.suffix + ".meta.json")
            meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        log.info("uploaded local blob %s/%s (%d bytes)", container, blob_name, len(data))
        return p.as_uri()

    def upload_json(
        self,
        container: str,
        blob_name: str,
        obj: Any,
        *,
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        md = {"uploaded_at": datetime.now(tz=UTC).strftime(_BLOB_TIMESTAMP_FORMAT)}
        if metadata:
            md.update(metadata)
        data = json.dumps(obj, indent=2, default=str, ensure_ascii=False).encode("utf-8")
        return self.upload_bytes(container, blob_name, data, content_type="application/json", metadata=md, overwrite=overwrite)

    def upload_file(
        self,
        container: str,
        blob_name: str,
        local_path: Path,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> str:
        p = self._path(container, blob_name)
        if p.exists() and not overwrite:
            return p.as_uri()
        shutil.copy2(local_path, p)
        md = {
            "uploaded_at": datetime.now(tz=UTC).strftime(_BLOB_TIMESTAMP_FORMAT),
            "original_filename": local_path.name,
        }
        if metadata:
            md.update(metadata)
        meta_path = p.with_suffix(p.suffix + ".meta.json")
        meta_path.write_text(json.dumps(md, indent=2), encoding="utf-8")
        log.info("uploaded local file blob %s/%s ← %s", container, blob_name, local_path)
        return p.as_uri()

    def download_bytes(self, container: str, blob_name: str) -> bytes | None:
        p = self._root / container / blob_name
        if p.exists():
            return p.read_bytes()
        return None

    def download_json(self, container: str, blob_name: str) -> Any | None:
        raw = self.download_bytes(container, blob_name)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def exists(self, container: str, blob_name: str) -> bool:
        return (self._root / container / blob_name).exists()

    def list_blobs(self, container: str, prefix: str = "") -> list[str]:
        base = self._root / container
        if not base.exists():
            return []
        result = []
        for p in base.rglob("*"):
            if p.is_file() and not p.name.endswith(".meta.json"):
                rel = str(p.relative_to(base)).replace(os.sep, "/")
                if prefix == "" or rel.startswith(prefix):
                    result.append(rel)
        return sorted(result)


# ---------------------------------------------------------------------------
# Null blob store (no-op — when blob storage is not configured)
# ---------------------------------------------------------------------------
class NullBlobStore:
    """No-op blob store. All uploads succeed (returning empty string),
    all downloads return None, all existence checks return False.
    """

    input_container = "calorch-inputs"
    output_container = "calorch-outputs"

    def upload_bytes(self, container, blob_name, data, *, content_type="application/octet-stream", metadata=None, overwrite=True) -> str:
        log.debug("null blob store: upload_bytes %s/%s (%d bytes) skipped", container, blob_name, len(data))
        return ""

    def upload_json(self, container, blob_name, obj, *, metadata=None, overwrite=True) -> str:
        log.debug("null blob store: upload_json %s/%s skipped", container, blob_name)
        return ""

    def upload_file(self, container, blob_name, local_path, *, content_type="application/octet-stream", metadata=None, overwrite=True) -> str:
        log.debug("null blob store: upload_file %s/%s skipped", container, blob_name)
        return ""

    def download_bytes(self, container, blob_name) -> bytes | None:
        return None

    def download_json(self, container, blob_name) -> Any | None:
        return None

    def exists(self, container, blob_name) -> bool:
        return False

    def list_blobs(self, container, prefix="") -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Path helpers — conventions for blob naming
# ---------------------------------------------------------------------------
def input_blob_path(provider: str, key: str) -> str:
    """Return the blob path for an input data file.

    Example: ``input_blob_path("sec", "companyfacts/0000320193")``
      → ``"inputs/sec/companyfacts/0000320193.json"``
    """
    safe_provider = provider.lower().replace(" ", "_")
    safe_key = key.strip("/")
    return f"inputs/{safe_provider}/{safe_key}.json"


def output_blob_path(run_id: str, event_id: str, filename: str) -> str:
    """Return the blob path for an output artefact.

    Example: ``output_blob_path("run-42", "ev-001", "packet.docx")``
      → ``"outputs/run-42/ev-001/packet.docx"``
    """
    safe_run = _safe_blob_name(run_id)
    safe_event = _safe_blob_name(event_id)
    safe_name = _safe_blob_name(filename)
    return f"outputs/{safe_run}/{safe_event}/{safe_name}"


def briefing_blob_path(run_id: str) -> str:
    """Return the blob path for the weekly briefing HTML.

    Example: ``briefing_blob_path("run-42")``
      → ``"outputs/run-42/briefing/weekly.html"``
    """
    safe_run = _safe_blob_name(run_id)
    return f"outputs/{safe_run}/briefing/weekly.html"


def _safe_blob_name(value: str) -> str:
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._ ")
    return safe or "unnamed"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_blob_store(
    *,
    connection_string: str | None = None,
    account_url: str | None = None,
    local_root: Path | None = None,
    input_container: str = "calorch-inputs",
    output_container: str = "calorch-outputs",
) -> BlobStore:
    """Create the appropriate BlobStore based on configuration.

    Priority:
      1. ``connection_string`` → AzureBlobStore (production)
      2. ``account_url`` → AzureBlobStore with managed identity
      3. ``local_root`` → LocalBlobStore (dev/testing)
      4. Otherwise → NullBlobStore
    """
    containers = {"input_container": input_container, "output_container": output_container}
    if connection_string:
        return AzureBlobStore(connection_string=connection_string, **containers)
    if account_url:
        return AzureBlobStore(account_url=account_url, **containers)
    if local_root:
        return LocalBlobStore(local_root, **containers)
    return NullBlobStore()