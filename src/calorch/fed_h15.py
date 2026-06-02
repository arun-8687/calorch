"""Federal Reserve H.15 selected interest rates (no API key required).

H.15 is the Fed's official daily release of selected interest rates. The
XML feed is public and documented; no registration, no rate limits.

Source: https://www.federalreserve.gov/releases/h15/
Endpoint: https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15&series=...

This client parses the official CSV download for treasury constant
maturity yields and the effective federal funds rate. The H.15 release
covers the same series calorch needs (treasury curve + EFFR) and serves
as a key-less fallback when FRED is unavailable.
"""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


H15_CSV_URL = "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15&series=bf17364827e38702b42a58cf8eaa3f78&lastobs=&from=&to=&filetype=csv&label=include&layout=seriescolumn"


@dataclass(frozen=True)
class RatePoint:
    series_id: str
    date: date
    value: float
    series_name: str = ""


class FedH15Client:
    """Key-less FOMC H.15 selected interest rates."""

    SERIES = {
        "DFF":      "Federal funds (effective)",
        "DGS1MO":   "1-month",
        "DGS3MO":   "3-month",
        "DGS6MO":   "6-month",
        "DGS1":     "1-year",
        "DGS2":     "2-year",
        "DGS3":     "3-year",
        "DGS5":     "5-year",
        "DGS7":     "7-year",
        "DGS10":    "10-year",
        "DGS20":    "20-year",
        "DGS30":    "30-year",
    }

    # Map our series ids to the H.15 Unique Identifiers in the CSV header.
    H15_ID_MAP = {
        "DFF":     "RIFLPBCIANM",
        "DGS1MO":  "RIFLGFCM01_N.B",
        "DGS3MO":  "RIFLGFCM03_N.B",
        "DGS6MO":  "RIFLGFCM06_N.B",
        "DGS1":    "RIFLGFCY01_N.B",
        "DGS2":    "RIFLGFCY02_N.B",
        "DGS3":    "RIFLGFCY03_N.B",
        "DGS5":    "RIFLGFCY05_N.B",
        "DGS7":    "RIFLGFCY07_N.B",
        "DGS10":   "RIFLGFCY10_N.B",
        "DGS20":   "RIFLGFCY20_N.B",
        "DGS30":   "RIFLGFCY30_N.B",
    }

    def __init__(self, *, cache_dir: Path | None = None, timeout: float = 30.0) -> None:
        cache = cache_dir or (Path.cwd() / ".cache" / "fed")
        cache.mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache
        self._timeout = timeout

    def _cache_path(self) -> Path:
        return self._cache_dir / "h15.csv"

    def _fetch(self) -> list[RatePoint]:
        cache = self._cache_path()
        if cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=24):
            text = cache.read_text(encoding="utf-8")
        else:
            r = httpx.get(H15_CSV_URL, timeout=self._timeout, follow_redirects=True)
            r.raise_for_status()
            text = r.text
            cache.write_text(text, encoding="utf-8")

        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> list[RatePoint]:
        # H.15 CSV has 5 metadata lines, then a "Time Period" header row
        # with Unique Identifiers (e.g. RIFLGFCM01_N.B). We match the
        # Unique Identifiers to our canonical series ids via H15_ID_MAP.
        lines = text.splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            if "Time Period" in line and "RIFL" in line.upper():
                header_idx = i
                break
        if header_idx is None:
            return []
        header = next(csv.reader(io.StringIO(lines[header_idx])))
        # Map our series id -> column index
        col_idx: dict[str, int] = {}
        for i, col in enumerate(header):
            col = col.strip()
            for sid, uid in FedH15Client.H15_ID_MAP.items():
                if col == uid:
                    col_idx.setdefault(sid, i)
        out: list[RatePoint] = []
        for row in csv.reader(io.StringIO("\n".join(lines[header_idx + 1:]))):
            if not row or len(row) < 2:
                continue
            d_str = row[0].strip()
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                continue
            for sid, idx in col_idx.items():
                if idx >= len(row):
                    continue
                v = row[idx].strip()
                if not v or v in {".", "ND", "NA"}:
                    continue
                try:
                    fv = float(v)
                except ValueError:
                    continue
                out.append(RatePoint(
                    series_id=sid,
                    date=d,
                    value=fv,
                    series_name=FedH15Client.SERIES[sid],
                ))
        return out

    def latest(self, series_id: str) -> RatePoint | None:
        series_id = series_id.upper()
        for pt in self._fetch():
            if pt.series_id == series_id:
                return pt
        return None

    def snapshot(self, series_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        series_ids = series_ids or ["DFF", "DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5", "DGS7", "DGS10", "DGS20", "DGS30"]
        all_pts = self._fetch()
        by_sid: dict[str, list[RatePoint]] = {}
        for pt in all_pts:
            by_sid.setdefault(pt.series_id, []).append(pt)
        out: dict[str, dict[str, Any]] = {}
        for sid in series_ids:
            pts = by_sid.get(sid.upper(), [])
            if not pts:
                out[sid] = {"value": None, "date": None, "change_1w": None, "series_id": sid, "series_name": self.SERIES.get(sid.upper(), sid)}
                continue
            pts.sort(key=lambda p: p.date, reverse=True)
            latest_pt = pts[0]
            change = None
            if len(pts) >= 5:
                prior = pts[4].value
                if prior != 0:
                    change = (latest_pt.value - prior)
            out[sid] = {
                "value": latest_pt.value,
                "date": latest_pt.date.isoformat(),
                "change_1w_bps": round(change * 100, 1) if change is not None else None,
                "series_id": sid,
                "series_name": self.SERIES.get(sid.upper(), sid),
            }
        return out


