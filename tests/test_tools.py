"""Tests for production adapter selection without live cloud credentials."""
from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import pytest

from calorch.config import Settings, get_settings
from calorch.state import OrchestratorError
from calorch.tools import (
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
        start=datetime(2026, 6, 1, tzinfo=UTC),
        end=datetime(2026, 6, 2, tzinfo=UTC),
    )
    assert [event["id"] for event in events] == ["a", "b"]
    assert client._http.calls[1] == ("GET", "https://next", None)


def test_table_repository_crud(monkeypatch: pytest.MonkeyPatch):
    """TableRepository merges, round-trips nested JSON, and lists entities."""
    from azure.core.exceptions import ResourceNotFoundError

    class FakeTable:
        def __init__(self) -> None:
            self.rows: dict[tuple[str, str], dict[str, Any]] = {}

        def create_table(self) -> None:
            pass

        def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
            key = (partition_key, row_key)
            if key not in self.rows:
                raise ResourceNotFoundError(key)
            return dict(self.rows[key])

        def upsert_entity(self, entity: dict[str, Any], mode: Any = None) -> None:
            self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

        def list_entities(self):
            return list(self.rows.values())

    fake = FakeTable()

    class FakeTableClient:
        @classmethod
        def from_connection_string(cls, conn: str, table_name: str) -> FakeTable:
            assert conn == "UseDevelopmentStorage=true"
            return fake

    data_tables = types.ModuleType("azure.data.tables")
    data_tables.TableClient = FakeTableClient

    class UpdateMode:
        REPLACE = "replace"

    data_tables.UpdateMode = UpdateMode
    monkeypatch.setitem(sys.modules, "azure.data.tables", data_tables)

    from calorch.tools import TableRepository

    repo = TableRepository("calorchdelivery", connection_string="UseDevelopmentStorage=true")
    assert repo.get("ev-1") is None
    repo.upsert("ev-1", {"subject": "First", "to": ["a@x.com"]})
    repo.upsert("ev-1", {"confidence": 0.9})  # merge, not overwrite
    rec = repo.get("ev-1")
    assert rec["subject"] == "First"          # preserved across merge
    assert rec["confidence"] == 0.9
    assert rec["to"] == ["a@x.com"]           # nested value round-trips
    assert len(repo.all()) == 1


def test_table_key_sanitises_forbidden_chars():
    from calorch.tools import _table_key

    assert _table_key("ev/with#bad?chars") == "ev_with_bad_chars"
    assert _table_key("") == "_"
