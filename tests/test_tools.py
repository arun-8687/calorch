"""Tests for production adapter selection without live cloud credentials."""
from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from calorch.config import Settings, get_settings
from calorch.state import OrchestratorError
from calorch.tools import (
    CosmosRepository,
    GraphOneDriveClient,
    LocalOneDriveClient,
    _GraphClientReal,
    make_graph_client,
    make_onedrive_client,
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("REPO_PATH", str(tmp_path / "repo.json"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SEC_CACHE_DIR", str(tmp_path / "sec"))
    get_settings.cache_clear()
    return get_settings()


def _production(settings: Settings, **changes: Any) -> Settings:
    return replace(
        settings,
        use_mocks=False,
        graph_tenant_id="tenant",
        graph_client_id="client",
        graph_client_secret="secret",
        graph_user_id="analyst@example.com",
        **changes,
    )


def test_real_graph_client_requires_complete_credentials(settings: Settings):
    incomplete = replace(settings, use_mocks=False, graph_tenant_id=None)
    with pytest.raises(OrchestratorError, match="GRAPH_TENANT_ID"):
        make_graph_client(incomplete)


def test_real_onedrive_backend_is_selected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    uploads: list[tuple[str, str]] = []

    class FakeGraph:
        def __init__(self, _settings: Settings) -> None:
            pass

        def upload_file(self, drive_id: str, local_path: Path, remote_name: str) -> str:
            uploads.append((drive_id, remote_name))
            return f"https://sharepoint.example/{remote_name}"

    monkeypatch.setattr("calorch.tools._GraphClientReal", FakeGraph)
    client = make_onedrive_client(_production(settings, onedrive_drive_id="drive-1"))
    assert isinstance(client, GraphOneDriveClient)
    local = tmp_path / "brief.docx"
    local.write_bytes(b"docx")
    assert client.upload(local, "event.docx") == "https://sharepoint.example/event.docx"
    assert uploads == [("drive-1", "event.docx")]


def test_local_onedrive_upload_cannot_escape_archive(tmp_path: Path):
    local = tmp_path / "brief.docx"
    local.write_bytes(b"docx")
    archive = tmp_path / "archive"
    client = LocalOneDriveClient(archive)
    url = client.upload(local, "../escape.docx")
    assert (archive / "escape.docx").exists()
    assert not (tmp_path / "escape.docx").exists()
    assert url.endswith("/escape.docx")


def test_graph_calendar_view_follows_pagination(settings: Settings):
    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return self._payload

    class Http:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Any]] = []
            self.responses = [
                Response({"value": [{"id": "a"}], "@odata.nextLink": "https://next"}),
                Response({"value": [{"id": "b"}]}),
            ]

        def request(self, method: str, url: str, **kwargs: Any) -> Response:
            self.calls.append((method, url, kwargs.get("params")))
            return self.responses.pop(0)

    client = _GraphClientReal(_production(settings))
    client._token = "token"
    client._token_expires = float("inf")
    client._http = Http()
    events = client.list_events(
        start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    assert [event["id"] for event in events] == ["a", "b"]
    assert client._http.calls[1] == ("GET", "https://next", None)


def test_cosmos_repository_crud(monkeypatch: pytest.MonkeyPatch):
    class NotFound(Exception):
        pass

    class Container:
        def __init__(self) -> None:
            self.rows: dict[str, dict[str, Any]] = {}

        def read_item(self, item: str, partition_key: str) -> dict[str, Any]:
            if item not in self.rows:
                raise NotFound(item)
            return dict(self.rows[item])

        def upsert_item(self, row: dict[str, Any]) -> None:
            self.rows[row["id"]] = dict(row)

        def query_items(self, **_: Any):
            return list(self.rows.values())

    container = Container()

    class Client:
        def __init__(self, endpoint: str, credential: str) -> None:
            assert endpoint == "https://cosmos.example"
            assert credential == "key"

        def get_database_client(self, db: str):
            assert db == "calorch"
            return self

        def get_container_client(self, name: str):
            assert name == "events"
            return container

    azure = types.ModuleType("azure")
    cosmos = types.ModuleType("azure.cosmos")
    exceptions = types.ModuleType("azure.cosmos.exceptions")
    cosmos.CosmosClient = Client
    exceptions.CosmosResourceNotFoundError = NotFound
    azure.cosmos = cosmos
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.cosmos", cosmos)
    monkeypatch.setitem(sys.modules, "azure.cosmos.exceptions", exceptions)

    repo = CosmosRepository("https://cosmos.example", "key", "calorch", "events")
    assert repo.get("ev-1") is None
    repo.upsert("ev-1", {"subject": "First"})
    repo.upsert("ev-1", {"confidence": 0.9})
    assert repo.get("ev-1")["subject"] == "First"
    assert repo.get("ev-1")["confidence"] == 0.9
    assert len(repo.all()) == 1
