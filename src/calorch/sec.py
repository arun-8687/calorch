"""SEC EDGAR client.

Pulls:
  * Recent filings for a watchlist of CIKs (submissions API).
  * XBRL company facts (financial fundamentals).
  * Ticker ↔ CIK mapping (cached).

SEC terms of use:
  * 10 req/sec max.
  * Identify yourself via the ``User-Agent`` header (real email required).
  * This client defaults to a placeholder agent; override via
    ``SEC_USER_AGENT`` in your environment.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from cachetools import TTLCache

from calorch.http_client import get_client


# SEC classifies forms into the orchestrator's 8 event types.
# The `classify_form` function takes the form code (e.g. "8-K") and optionally
# the items string (e.g. "5.07") to disambiguate. Items take precedence
# for 8-K because Item 2.02 (Results of Operations) is always an earnings
# event, while other 8-K items (5.03, 5.07, etc.) map differently.

_8K_ITEMS_MAP: dict[str, str] = {
    # Earnings / financial results
    "2.02": "earnings_call",
    "2.03": "earnings_call",
    "2.04": "earnings_call",
    "2.05": "earnings_call",
    "2.06": "earnings_call",
    # Regulation FD / disclosure
    "7.01": "channel_check",
    # Material agreements / events
    "1.01": "management_meeting",
    "1.02": "management_meeting",
    # Director/officer changes
    "5.02": "analyst_meeting",
    # Shareholder matters
    "5.03": "management_meeting",
    "5.04": "management_meeting",
    "5.05": "management_meeting",
    "5.07": "conference",
    "5.08": "conference",
    # Financials
    "4.01": "earnings_call",
    "4.02": "earnings_call",
}

# Non-8-K form classifications.
_FILING_TYPE_MAP: dict[tuple[str, ...], str] = {
    ("10-K", "10-Q"): "earnings_call",
    ("DEF 14A", "PRE 14A", "DEFA14A"): "management_meeting",
    ("S-1", "424B", "F-1"): "conference",
    ("SC 13G", "SC 13G/A", "SC 13D", "SC 13D/A"): "kol_meeting",
    ("4", "4/A"): "analyst_meeting",
    ("13F-HR", "13F-HR/A", "13F-NT"): "portfolio_meeting",
    ("11-K", "10-K/A", "20-F", "40-F", "6-K"): "internal_review",
}


def classify_form(form: str, items: str = "") -> str:
    """Map a SEC form code (+ optional items string) to an EventType value.

    For 8-K filings, the items string (e.g. "5.07,9.01") is inspected
    first. The first matching item in `_8K_ITEMS_MAP` wins. If no item
    matches, "channel_check" is used as the generic 8-K fallback.
    """
    if form.upper() == "8-K" or form == "8-K/A":
        if items:
            for part in items.split(","):
                part = part.strip()
                if part in _8K_ITEMS_MAP:
                    return _8K_ITEMS_MAP[part]
        return "channel_check"
    for forms, label in _FILING_TYPE_MAP.items():
        if form in forms:
            return label
    return "unknown"


# ---------------------------------------------------------------------------
# User-agent
# ---------------------------------------------------------------------------
DEFAULT_USER_AGENT = "Calorch Research calorch@example.com"


# ---------------------------------------------------------------------------
# Cached ticker → CIK map
# ---------------------------------------------------------------------------
class TickerMap:
    """Loaded once from ``https://www.sec.gov/files/company_tickers.json``."""

    _URL = "https://www.sec.gov/files/company_tickers.json"

    def __init__(self, user_agent: str, cache_path: Path | None = None) -> None:
        self._ua = user_agent
        self._path = cache_path
        self._by_ticker: dict[str, dict[str, Any]] = {}
        self._by_cik: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        if self._by_ticker:
            return
        if self._path and self._path.exists() and (datetime.now() - datetime.fromtimestamp(self._path.stat().st_mtime)) < timedelta(days=7):
            data = json.loads(self._path.read_text(encoding="utf-8"))
        else:
            r = get_client().get(
                self._URL,
                headers={"User-Agent": self._ua, "Accept-Encoding": "gzip"},
                service="sec_edgar",
            )
            data = r.json()
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(data), encoding="utf-8")
        for _, entry in data.items():
            tk = entry.get("ticker", "").upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            self._by_ticker[tk] = {"cik": cik, "title": entry.get("title", ""), "exchange": entry.get("exchange", "")}
            self._by_cik[cik] = {"ticker": tk, "title": entry.get("title", ""), "exchange": entry.get("exchange", "")}

    def cik_for(self, ticker: str) -> str | None:
        self.load()
        entry = self._by_ticker.get(ticker.upper())
        return entry["cik"] if entry else None

    def company_for(self, ticker: str) -> dict[str, Any] | None:
        self.load()
        return self._by_ticker.get(ticker.upper())

    def all_tickers(self) -> list[str]:
        self.load()
        return list(self._by_ticker.keys())


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
class _RateLimiter:
    """SEC fair-use: 10 req/sec. Token bucket, 0.1s spacing. Thread-safe."""

    def __init__(self, rate_per_sec: float = 9.0) -> None:
        self._interval = 1.0 / rate_per_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class SecEdgarClient:
    """Read-only EDGAR client for filings + XBRL company facts."""

    _SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
    _COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, *, cache_dir: Path | None = None) -> None:
        self._ua = user_agent
        self._rl = _RateLimiter()
        cache = cache_dir or (Path.cwd() / ".cache" / "sec")
        cache.mkdir(parents=True, exist_ok=True)
        self._tickers = TickerMap(user_agent, cache_path=cache / "company_tickers.json")
        self._facts_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=256, ttl=3600)
        self._sub_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=128, ttl=1800)

    # ---- HTTP ----
    def _get(self, url: str) -> dict[str, Any]:
        self._rl.wait()
        try:
            r = get_client().get(
                url,
                headers={"User-Agent": self._ua, "Accept-Encoding": "gzip"},
                service="sec_edgar",
            )
            return r.json()
        except httpx.HTTPError:
            raise

    # ---- public ----
    def cik_for(self, ticker: str) -> str | None:
        return self._tickers.cik_for(ticker)

    def company_for(self, ticker: str) -> dict[str, Any] | None:
        return self._tickers.company_for(ticker)

    def list_recent_filings(
        self,
        tickers: Iterable[str],
        start: date,
        end: date,
        forms: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return filings as synthetic 'calendar events'.

        Each event has the shape::

            {
              "id":          "<cik>-<accession>",   # used as the calendar event id
              "subject":     "Apple Inc. — 8-K (Item 2.02 — Results of Operations)",
              "bodyPreview": "...",
              "start":       {"dateTime": "<filingDate>T<filingTime>", ...},
              "end":         {"dateTime": "<filingDate>T<filingTime+1h>", ...},
              "organizer":   {"emailAddress": {"name": "SEC EDGAR"}},
              "attendees":   [...],
              "location":    {"displayName": "EDGAR / <primaryDocument>"},
              "isOnlineMeeting": False,
              "webLink":     "https://www.sec.gov/Archives/edgar/data/<cik>/<accession-stripped>/<primaryDocument>",
              "_form":       "8-K",
              "_ticker":     "AAPL",
              "_cik":        "0000320193",
              "_accession":  "0000320193-26-000123",
            }
        """
        forms_set = set(f.upper() for f in forms) if forms else None
        out: list[dict[str, Any]] = []
        for ticker in tickers:
            cik = self.cik_for(ticker)
            if not cik:
                continue
            sub = self._submissions(cik)
            recent = sub.get("filings", {}).get("recent", {})
            form_list = recent.get("form", [])
            date_list = recent.get("filingDate", [])
            acc_list = recent.get("accessionNumber", [])
            primdoc_list = recent.get("primaryDocument", [])
            items_list = recent.get("items", [])
            n = len(form_list)
            for i in range(n):
                form = form_list[i]
                fd = date_list[i] if i < len(date_list) else ""
                if not fd:
                    continue
                try:
                    d = date.fromisoformat(fd)
                except ValueError:
                    continue
                if not (start <= d <= end):
                    continue
                if forms_set and form.upper() not in forms_set:
                    continue
                acc = acc_list[i] if i < len(acc_list) else ""
                acc_nodash = acc.replace("-", "")
                prim = primdoc_list[i] if i < len(primdoc_list) else ""
                items = items_list[i] if i < len(items_list) else ""
                company = sub.get("name", ticker)
                start_dt = datetime(d.year, d.month, d.day, 9, 0, tzinfo=timezone.utc)
                end_dt = start_dt + timedelta(hours=1)
                subject = f"{company} — {form} ({items or 'filing'})"
                body = (
                    f"CIK {cik} · accession {acc} · filed {fd} · primary doc {prim} · "
                    f"items: {items or '—'}"
                )
                out.append({
                    "id": f"{cik}-{acc}",
                    "subject": subject,
                    "bodyPreview": body,
                    "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": "UTC"},
                    "organizer": {"emailAddress": {"name": "SEC EDGAR", "address": "edgar@sec.gov"}},
                    "attendees": [],
                    "location": {"displayName": f"EDGAR · {prim or form}"},
                    "isOnlineMeeting": False,
                    "webLink": (
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{prim}"
                        if prim else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
                    ),
                    "_form": form,
                    "_items": items,
                    "_ticker": ticker.upper(),
                    "_cik": cik,
                    "_accession": acc,
                    "_filingDate": fd,
                    "_company": company,
                })
        return out

    def get_company_facts(self, cik: str) -> dict[str, Any]:
        """Return XBRL companyfacts JSON for a CIK (cached)."""
        if cik in self._facts_cache:
            return self._facts_cache[cik]
        url = self._COMPANYFACTS.format(cik=cik)
        data = self._get(url)
        self._facts_cache[cik] = data
        return data

    def latest_financials(self, ticker: str) -> dict[str, Any]:
        """Pull the most recent Revenue, NetIncome, EPS facts."""
        cik = self.cik_for(ticker)
        if not cik:
            return {}
        facts = self.get_company_facts(cik)
        us_gaap = facts.get("facts", {}).get("us-gaap", {})

        def _latest(concept: str, unit: str = "USD") -> dict[str, Any] | None:
            entries = us_gaap.get(concept, {}).get("units", {}).get(unit, [])
            if not entries:
                return None
            # Pick the most recent annual/quarterly entry with form 10-K/10-Q.
            candidates = [
                e for e in entries
                if e.get("form") in {"10-K", "10-Q"} and e.get("fp") in {"FY", "Q1", "Q2", "Q3", "Q4"}
            ]
            if not candidates:
                return entries[-1]
            # Sort by end date desc, then by filed date desc, and take the first.
            candidates.sort(key=lambda e: (e.get("end") or "", e.get("filed") or ""), reverse=True)
            return candidates[0]

        rev = _latest("Revenues") or _latest("RevenueFromContractWithCustomerExcludingAssessedTax")
        ni = _latest("NetIncomeLoss")
        eps = _latest("EarningsPerShareDiluted", unit="USD/shares") or _latest("EarningsPerShareBasic", unit="USD/shares")
        return {
            "ticker": ticker,
            "cik": cik,
            "company": facts.get("entityName"),
            "as_of": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "revenue": rev.get("val") if rev else None,
            "revenue_period": f"{rev.get('start', '?')} → {rev.get('end', '?')}" if rev else None,
            "revenue_form": rev.get("form") if rev else None,
            "net_income": ni.get("val") if ni else None,
            "eps_diluted": eps.get("val") if eps else None,
            "eps_form": eps.get("form") if eps else None,
        }

    # ---- internals ----
    def _submissions(self, cik: str) -> dict[str, Any]:
        if cik in self._sub_cache:
            return self._sub_cache[cik]
        url = self._SUBMISSIONS.format(cik=cik)
        data = self._get(url)
        self._sub_cache[cik] = data
        return data


# ---------------------------------------------------------------------------
# Adapter so the rest of the orchestrator can treat SEC as a GraphClient
# --------------------------------------------------------------------------