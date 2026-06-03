"""SEC EDGAR Full-Text Search (EFTS) client.

EFTS is the official free search engine over every filing on EDGAR,
indexing the full body text. Used here to pull "guidance / outlook /
expect" snippets from 8-Ks and 10-K/10-Q narratives for the prep pack.

Endpoint: https://efts.sec.gov/LATEST/search-index?q=...&dateRange=custom&startdt=...&enddt=...
        https://efts.sec.gov/LATEST/search-index?q=...&forms=10-K
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from calorch.http_client import get_client


EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"


@dataclass(frozen=True)
class EftsHit:
    id: str                  # e.g. "https://www.sec.gov/Archives/edgar/data/320193/000032019326000123/0000320193-26-000123-index.htm"
    ciks: list[str]
    form: str
    file_date: str
    accession: str
    display_names: list[str]
    score: float
    snippet: str             # highlighted excerpt from EFTS
    source: str = "efts"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ciks": self.ciks,
            "form": self.form,
            "file_date": self.file_date,
            "accession": self.accession,
            "display_names": self.display_names,
            "score": self.score,
            "snippet": self.snippet,
            "source": self.source,
        }


class SecEftsClient:
    """EFTS full-text search across all EDGAR filings."""

    def __init__(self, user_agent: str, *, cache_dir: Path | None = None) -> None:
        self._ua = user_agent
        cache = cache_dir or (Path.cwd() / ".cache" / "sec_efts")
        cache.mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache
        self._last_req = 0.0
        self._min_interval = 1.0 / 9.0  # share SEC fair-use 9 req/sec

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_req
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_req = time.monotonic()

    def _cache_path(self, params: dict[str, Any]) -> Path:
        key = "_".join(f"{k}={v}" for k, v in sorted(params.items()))
        return self._cache_dir / (key.replace("/", "_") + ".json")

    def search(
        self,
        q: str,
        *,
        cik: str | None = None,
        forms: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10,
    ) -> list[EftsHit]:
        """Return EFTS hits sorted by score desc.

        ``q`` is the raw search string (e.g. "outlook fiscal 2026", or
        ``"guidance"``). ``forms`` filters to e.g. ``["10-K", "10-Q"]``.
        ``start``/``end`` are date strings in MM/DD/YYYY format (EFTS
        requirement).
        """
        params: dict[str, Any] = {
            "q": q,
            "dateRange": "custom" if (start or end) else "all",
            "limit": str(limit),
        }
        if cik:
            # EFTS requires 10-digit zero-padded CIK; the older code used
            # ``str(int(cik))`` which strips leading zeros and silently
            # returns 0 hits.
            params["ciks"] = str(cik).zfill(10)
        # EFTS does not accept multiple ``forms`` joined by ``-`` (it
        # returns 0 hits). Run one request per form and merge results.
        form_list = list(forms) if forms else [None]
        results: list[EftsHit] = []
        seen_ids: set[str] = set()
        for form in form_list:
            per_params = dict(params)
            if form:
                per_params["forms"] = form
            if start:
                per_params["startdt"] = start
            if end:
                per_params["enddt"] = end

            cache = self._cache_path(per_params)
            payload: dict[str, Any] | None = None
            if cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=12):
                try:
                    payload = json.loads(cache.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    payload = None
            if payload is None:
                self._rate_limit()
                url = f"{EFTS_BASE}?{urlencode(per_params)}"
                try:
                    client = get_client()
                    r = client.get(url, headers={"User-Agent": self._ua}, service="sec_efts")
                except httpx.HTTPError:
                    continue
                try:
                    payload = r.json()
                except (json.JSONDecodeError, ValueError):
                    continue
                cache.write_text(json.dumps(payload), encoding="utf-8")

            for h in payload.get("hits", {}).get("hits", []):
                hid = h.get("_id", "")
                if hid in seen_ids:
                    continue
                seen_ids.add(hid)
                src = h.get("_source", {})
                ad = src.get("adsh", "")
                # Build snippet: prefer highlight, fall back to items summary
                hl = h.get("highlight", {}).get("text", [""])
                snippet = hl[0] if hl else ""
                if not snippet:
                    items = src.get("items", "")
                    if items:
                        snippet = f"{src.get('form', '')} filed {src.get('file_date', '')} — items: {items}"
                    else:
                        snippet = f"{src.get('form', '')} filed {src.get('file_date', '')} — {src.get('file_description', '')}"
                results.append(EftsHit(
                    id=hid,
                    ciks=[str(c) for c in src.get("ciks", [])],
                    form=src.get("form", ""),
                    file_date=src.get("file_date", ""),
                    accession=ad,
                    display_names=src.get("display_names", []),
                    score=h.get("_score") or 0.0,
                    snippet=snippet,
                ))
        # sort by score desc, trim to limit
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    def search_for_guidance(
        self,
        cik: str,
        ticker: str,
        *,
        forms: list[str] | None = None,
        lookback_days: int = 365,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search for the latest 'guidance / outlook' excerpts for a CIK."""
        forms = forms or ["10-K", "10-Q", "8-K"]
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=lookback_days)
        # EFTS wants MM/DD/YYYY
        start = start_dt.strftime("%m/%d/%Y")
        end = end_dt.strftime("%m/%d/%Y")

        all_hits: list[dict[str, Any]] = []
        for q in ('"outlook"', '"guidance"', '"fiscal 2026"', '"expect"', '"anticipate"'):
            try:
                hits = self.search(
                    q,
                    cik=cik,
                    forms=forms,
                    start=start,
                    end=end,
                    limit=limit,
                )
            except httpx.HTTPError:
                continue
            for h in hits:
                d = h.to_dict()
                d["ticker"] = ticker
                d["query"] = q.strip('"')
                all_hits.append(d)
        # de-dup by accession
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for h in all_hits:
            if h["accession"] in seen:
                continue
            seen.add(h["accession"])
            unique.append(h)
        unique.sort(key=lambda h: h.get("file_date", ""), reverse=True)
        return unique[:limit]


