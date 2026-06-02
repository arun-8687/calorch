"""Tests for the FRED macro client + FOMC H.15 fallback."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from calorch.fed_h15 import FedH15Client
from calorch.fred import FredClient, FredPoint


# ---------------------------------------------------------------------------
# FOMC H.15 parser unit tests (no network needed)
# ---------------------------------------------------------------------------
def test_fed_h15_parse_handles_missing_dots():
    """H.15 csv uses '.' for missing; parser must skip without error."""
    csv_text = (
        "Time Period,RIFLPBCIANM,RIFLGFCY02_N.B\n"
        "2026-05-30,5.33,4.62\n"
        "2026-05-29,5.33,4.59\n"
    )
    pts = FedH15Client._parse(csv_text)
    assert len(pts) == 4  # 2 dates x 2 series
    series = {p.series_id for p in pts}
    assert series == {"DFF", "DGS2"}


def test_fed_h15_parse_skips_junk_header():
    """H.15 csv has metadata header lines; parser must find the right row."""
    csv_text = (
        "Title,Federal Reserve H.15\n"
        "Series,blah\n"
        "Units,Percent\n"
        "Time Period,RIFLPBCIANM,RIFLGFCY02_N.B\n"
        "2026-05-30,5.33,4.62\n"
    )
    pts = FedH15Client._parse(csv_text)
    assert len(pts) == 2


# ---------------------------------------------------------------------------
# Real FRED client (caching behaviour only — no live network in tests)
# ---------------------------------------------------------------------------
def test_fred_client_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The cache should short-circuit a second call within the TTL window."""
    calls = []

    def fake_get(url, params, timeout):
        from unittest.mock import MagicMock
        calls.append(params.get("series_id"))
        m = MagicMock()
        m.raise_for_status = lambda: None
        m.json = lambda: {
            "observations": [
                {"date": "2026-05-30", "value": "5.33",
                 "realtime_start": "2026-05-30", "realtime_end": "2026-05-30"},
                {"date": "2026-05-29", "value": "5.32",
                 "realtime_start": "2026-05-29", "realtime_end": "2026-05-29"},
            ]
        }
        return m

    monkeypatch.setattr("httpx.get", fake_get)
    client = FredClient(api_key=None, cache_dir=tmp_path)
    pts1 = client.get_series("DFF", start="2026-05-01", end="2026-05-30")
    pts2 = client.get_series("DFF", start="2026-05-01", end="2026-05-30")
    assert len(pts1) == 2
    assert len(pts2) == 2
    # Second call must hit the cache, not httpx
    assert calls == ["DFF"]
    assert pts1[0].value == 5.33


def test_fred_client_handles_missing_dot_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """FRED's '.' sentinel for missing observations must be skipped."""
    def fake_get(url, params, timeout):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.raise_for_status = lambda: None
        m.json = lambda: {
            "observations": [
                {"date": "2026-05-30", "value": ".", "realtime_start": "x", "realtime_end": "y"},
                {"date": "2026-05-29", "value": "4.50", "realtime_start": "x", "realtime_end": "y"},
            ]
        }
        return m

    monkeypatch.setattr("httpx.get", fake_get)
    client = FredClient(api_key=None, cache_dir=tmp_path)
    pts = client.get_series("DGS10", start="2026-05-01", end="2026-05-30")
    assert len(pts) == 1
    assert pts[0].value == 4.50


def test_free_macro_provider_prefers_fred_over_h15():
    """FreeMacroProvider fills missing keys from H.15 when FRED lacks them."""
    from calorch.providers import FreeMacroProvider

    class _FredStub:
        def snapshot(self):
            return {"vix": {"value": 14.0, "date": "2026-05-30", "change_1w": 0.0, "series_id": "VIXCLS"}}

    class _H15Stub:
        def snapshot(self):
            return {
                "DGS10": {"value": 4.27, "date": "2026-05-30", "change_1w_bps": 5.0, "series_id": "DGS10"},
                "DFF":   {"value": 5.33, "date": "2026-05-30", "change_1w_bps": 0.0, "series_id": "DFF"},
            }

    bundle = FreeMacroProvider(fred=_FredStub(), fed_h15=_H15Stub())
    out = bundle.snapshot()
    assert out["vix"]["value"] == 14.0
    # H.15 series are remapped to canonical labels (treasury_10y, fed_funds)
    assert out["treasury_10y"]["value"] == 4.27
    assert out["treasury_10y"]["source"] == "fed-h15"
    assert out["fed_funds"]["value"] == 5.33
    # FRED-sourced entries get the "fred" label
    assert out["vix"]["source"] == "fred"


def test_free_macro_provider_survives_fred_failure():
    from calorch.providers import FreeMacroProvider

    class _FredBroken:
        def snapshot(self):
            raise RuntimeError("network down")

    class _H15Stub:
        def snapshot(self):
            return {"DGS10": {"value": 4.27, "date": "2026-05-30", "series_id": "DGS10"}}

    bundle = FreeMacroProvider(fred=_FredBroken(), fed_h15=_H15Stub())
    out = bundle.snapshot()
    assert out["treasury_10y"]["value"] == 4.27
