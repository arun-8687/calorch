"""Tests for calorch.blob_store — Local, Null, and path helpers."""
import json
from unittest.mock import patch, MagicMock


from calorch.blob_store import (
    AzureBlobStore,
    LocalBlobStore,
    NullBlobStore,
    briefing_blob_path,
    input_blob_path,
    make_blob_store,
    output_blob_path,
)


# ---------------------------------------------------------------------------
# NullBlobStore
# ---------------------------------------------------------------------------
class TestNullBlobStore:
    def test_upload_bytes_returns_empty(self):
        nb = NullBlobStore()
        assert nb.upload_bytes("c", "b", b"data") == ""

    def test_upload_json_returns_empty(self):
        nb = NullBlobStore()
        assert nb.upload_json("c", "b", {"k": "v"}) == ""

    def test_upload_file_returns_empty(self, tmp_path):
        nb = NullBlobStore()
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert nb.upload_file("c", "b", f) == ""

    def test_download_returns_none(self):
        nb = NullBlobStore()
        assert nb.download_bytes("c", "b") is None
        assert nb.download_json("c", "b") is None

    def test_exists_returns_false(self):
        nb = NullBlobStore()
        assert nb.exists("c", "b") is False

    def test_list_returns_empty(self):
        nb = NullBlobStore()
        assert nb.list_blobs("c") == []


# ---------------------------------------------------------------------------
# LocalBlobStore
# ---------------------------------------------------------------------------
class TestLocalBlobStore:
    def test_upload_and_download_bytes(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        url = store.upload_bytes("inputs", "sec/test.json", b'{"ok": true}',
                                 content_type="application/json")
        assert url.startswith("file://")
        raw = store.download_bytes("inputs", "sec/test.json")
        assert raw is not None
        assert json.loads(raw) == {"ok": True}

    def test_upload_and_download_json(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        url = store.upload_json("inputs", "sentiment/AAPL.json",
                                {"mean_sentiment": 0.2}, metadata={"source": "alphasense"})
        assert url != ""
        data = store.download_json("inputs", "sentiment/AAPL.json")
        assert data is not None
        assert data["mean_sentiment"] == 0.2

    def test_upload_file(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        local = tmp_path / "local" / "report.docx"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"PK-docx-content")
        url = store.upload_file("outputs", "run-1/ev-001/report.docx", local,
                                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert url != ""
        raw = store.download_bytes("outputs", "run-1/ev-001/report.docx")
        assert raw == b"PK-docx-content"

    def test_exists(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        assert store.exists("c", "x") is False
        store.upload_bytes("c", "x", b"hi")
        assert store.exists("c", "x") is True

    def test_list_blobs(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        store.upload_bytes("c", "a/1.json", b"1")
        store.upload_bytes("c", "a/2.json", b"2")
        store.upload_bytes("c", "b/3.json", b"3")
        names = store.list_blobs("c")
        assert "a/1.json" in names
        assert "a/2.json" in names
        assert "b/3.json" in names
        prefixed = store.list_blobs("c", prefix="a/")
        assert len(prefixed) == 2

    def test_download_nonexistent_returns_none(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        assert store.download_bytes("c", "nope") is None
        assert store.download_json("c", "nope") is None

    def test_overwrite_default_true(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        store.upload_bytes("c", "x", b"v1")
        store.upload_bytes("c", "x", b"v2", overwrite=True)
        assert store.download_bytes("c", "x") == b"v2"

    def test_metadata_saved(self, tmp_path):
        store = LocalBlobStore(tmp_path / "blobs")
        store.upload_json("c", "d.json", {"k": 1}, metadata={"src": "test"})
        meta_path = tmp_path / "blobs" / "c" / "d.json.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["src"] == "test"
        assert "uploaded_at" in meta


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
class TestPathHelpers:
    def test_input_blob_path(self):
        assert input_blob_path("sec", "companyfacts/0000320193") == \
            "inputs/sec/companyfacts/0000320193.json"

    def test_input_blob_path_normalises_spaces(self):
        assert input_blob_path("SEC EDGAR", "AAPL") == \
            "inputs/sec_edgar/AAPL.json"

    def test_output_blob_path(self):
        assert output_blob_path("run-42", "ev-001", "packet.docx") == \
            "outputs/run-42/ev-001/packet.docx"

    def test_output_blob_path_sanitises(self):
        path = output_blob_path("run/42", "ev 001", "my file.docx")
        assert " " not in path
        assert path == "outputs/run_42/ev_001/my_file.docx"

    def test_briefing_blob_path(self):
        assert briefing_blob_path("run-42") == \
            "outputs/run-42/briefing/weekly.html"


# ---------------------------------------------------------------------------
# make_blob_store factory
# ---------------------------------------------------------------------------
class TestMakeBlobStore:
    def test_returns_null_when_no_config(self):
        s = make_blob_store()
        assert isinstance(s, NullBlobStore)

    def test_returns_local_when_root_provided(self, tmp_path):
        s = make_blob_store(local_root=tmp_path / "blobs")
        assert isinstance(s, LocalBlobStore)

    def test_returns_azure_with_connection_string(self):
        mock_instance = MagicMock(spec=AzureBlobStore)
        with patch("calorch.blob_store.AzureBlobStore", return_value=mock_instance) as MockCls:
            s = make_blob_store(connection_string="DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net")
            MockCls.assert_called_once()
            assert s is mock_instance

    def test_returns_azure_with_account_url(self):
        mock_instance = MagicMock(spec=AzureBlobStore)
        with patch("calorch.blob_store.AzureBlobStore", return_value=mock_instance) as MockCls:
            s = make_blob_store(account_url="https://test.blob.core.windows.net")
            MockCls.assert_called_once()
            assert s is mock_instance

def test_make_blob_store_uses_configured_containers(tmp_path):
    """STD-1: container names flow from settings into the store, not hardcoded."""
    from calorch.blob_store import make_blob_store

    store = make_blob_store(
        local_root=tmp_path, input_container="custom-in", output_container="custom-out"
    )
    assert store.input_container == "custom-in"
    assert store.output_container == "custom-out"
    store.upload_bytes(store.output_container, "k.txt", b"x")
    assert (tmp_path / "custom-out" / "k.txt").exists()
