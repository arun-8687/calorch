"""SEC iXBRL instance document parser for segment & geographic revenue.

SEC's structured XBRL ``companyfacts`` endpoint only carries consolidated
totals. Per-segment revenue (e.g. Apple iPhone vs. Mac vs. Services) and
geographic revenue (Americas/EMEA/APAC) live in the inline XBRL (iXBRL)
instance document attached to each 10-K/10-Q, in the
``<xbrli:segment>`` blocks under the filing's
``ProductOrServiceAxis`` / ``StatementBusinessSegmentsAxis`` /
``StatementGeographicalAxis`` dimensions.

This client:
  1. Finds the most recent 10-K/10-Q for a CIK via the submissions feed.
  2. Pulls the primary iXBRL document from
     ``https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/...``.
  3. Walks the XML with lxml and extracts segment-tagged facts.

Cost: 2-3 MB XML per filing, ~1s parse time. We cache per accession.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from lxml import etree

from calorch.http_client import get_client


XBRL_NS = "http://www.xbrl.org/2003/instance"
XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://www.xbrl.org/2006/xbrldi"
LINK_NS = "http://www.xbrl.org/2003/linkbase"
XLINK_NS = "http://www.w3.org/1999/xlink"
USGAAP_NS = "http://fasb.org/us-gaap/2024"  # fiscal-year specific; we'll match prefix

# Revenue tags we'll surface across segments. Companies use different
# us-gaap tags so we accept several synonyms.
REVENUE_TAGS = {
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenuesNetOfInterestExpense",
    "RegulatedAndUnregulatedOperatingRevenue",
}

# Common segment-axis dimension names (QNames appear in iXBRL).
SEGMENT_AXES = {
    "ProductOrServiceAxis",
    "StatementBusinessSegmentsAxis",
    "OperatingSegmentsAxis",
    "ProductsAndServicesAxis",
}
GEO_AXES = {
    "StatementGeographicalAxis",
    "GeographicalAxis",
    "CountryAxis",
}

# Heuristic: members ending with SegmentMember / CountryMember / RegionMember
# are geographic even when the axis is StatementBusinessSegmentsAxis.
_GEO_MEMBER_SUFFIXES = ("SegmentMember", "CountryMember", "RegionMember")


@dataclass(frozen=True)
class SegmentFact:
    concept: str           # e.g. "us-gaap:Revenues"
    segment_member: str    # e.g. "aapl:IPhoneMember"
    segment_label: str     # best-effort human label
    period_start: str      # ISO date or ""
    period_end: str
    value: float
    unit: str = "USD"
    decimals: str = ""
    axis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "segment_member": self.segment_member,
            "segment_label": self.segment_label,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "value": self.value,
            "unit": self.unit,
            "axis": self.axis,
        }


def _period_days(start: str, end: str) -> int:
    """Return approximate days in a period."""
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        return (e - s).days
    except ValueError:
        return 0


def _local_name(tag: str) -> str:
    """Strip namespace from a Clark-notation tag like ``{ns}localname``."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _strip_ns(name: str) -> str:
    """Strip a prefix like ``us-gaap:Revenues`` or a Clark-notation tag.

    Accepts both ``prefix:local`` (QName) and ``{ns}local`` (Clark form,
    used by lxml's ``Element.tag``).
    """
    if name.startswith("{"):
        return _local_name(name)
    return name.split(":", 1)[-1] if ":" in name else name


def _to_float(s: str | None) -> float | None:
    if s is None or s == "" or s == ".":
        return None
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


class SecIxbrlClient:
    """Fetches and parses iXBRL segment facts for a CIK."""

    def __init__(self, user_agent: str, *, cache_dir: Path | None = None) -> None:
        self._ua = user_agent
        cache = cache_dir or (Path.cwd() / ".cache" / "sec_ixbrl")
        cache.mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache
        # share SEC's fair-use 9 req/sec limit
        self._last_req = 0.0
        self._min_interval = 1.0 / 9.0

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_req
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_req = time.monotonic()

    def _get(self, url: str) -> bytes:
        """Fetch URL with rate limiting, using shared HTTP client with retry/pooling."""
        self._rate_limit()
        client = get_client()
        response = client.get(
            url,
            headers={"User-Agent": self._ua, "Accept-Encoding": "gzip"},
            service="sec_ixbrl",
        )
        return response.content

    def find_latest_filing(
        self,
        cik: str,
        *,
        form: str = "10-K",
    ) -> dict[str, Any] | None:
        """Return accession + primary doc for the latest 10-K/10-Q of a CIK."""
        cik_padded = str(cik).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        try:
            data = json.loads(self._get(url))
        except (httpx.HTTPError, json.JSONDecodeError):
            return None
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        prim = recent.get("primaryDocument", [])
        for i, f in enumerate(forms):
            if f.upper() == form.upper():
                return {
                    "cik": cik_padded,
                    "form": f,
                    "filingDate": dates[i] if i < len(dates) else "",
                    "accession": accs[i] if i < len(accs) else "",
                    "primaryDocument": prim[i] if i < len(prim) else "",
                }
        return None

    def _filing_url(self, cik: str, accession: str, primary_doc: str) -> str:
        acc = accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{primary_doc}"

    def _cache_path(self, cik: str, accession: str) -> Path:
        return self._cache_dir / f"{cik}_{accession.replace('-', '')}.xml"

    def fetch_ixbrl(self, cik: str, accession: str, primary_doc: str) -> bytes:
        cache = self._cache_path(cik, accession)
        if cache.exists():
            return cache.read_bytes()
        url = self._filing_url(cik, accession, primary_doc)
        body = self._get(url)
        cache.write_bytes(body)
        return body

    def extract_segment_facts(
        self,
        ixbrl_bytes: bytes,
        *,
        revenue_only: bool = True,
    ) -> list[SegmentFact]:
        """Walk iXBRL XML and pull segment-tagged facts.

        In inline XBRL the typed values are tagged directly (no
        ``xbrli:nonFraction`` wrapper). Each tagged element carries a
        ``contextRef`` that points to a ``<xbrli:context>`` containing
        ``<xbrli:segment>``. We match segments whose dimension matches
        the known axes (product/geo/operating-segments).
        """
        try:
            root = etree.fromstring(ixbrl_bytes)
        except etree.XMLSyntaxError:
            return []
        # 1. Index all contexts by id, capturing the segment/explicitMember.
        ctx_segments: dict[str, dict[str, str]] = {}
        for ctx in root.iter(f"{{{XBRLI_NS}}}context"):
            ctx_id = ctx.get("id", "")
            seg = ctx.find(f"{{{XBRLI_NS}}}entity/{{{XBRLI_NS}}}segment")
            if seg is None:
                continue
            dims: dict[str, str] = {}
            for em in seg.iter():
                if not em.tag.endswith("}explicitMember"):
                    continue
                dim = em.get("dimension", "")
                text = (em.text or "").strip()
                dims[dim] = text
            ctx_segments[ctx_id] = dims

        # 2. Also capture period from the context.
        ctx_periods: dict[str, dict[str, str]] = {}
        for ctx in root.iter(f"{{{XBRLI_NS}}}context"):
            ctx_id = ctx.get("id", "")
            period = ctx.find(f"{{{XBRLI_NS}}}period")
            if period is None:
                continue
            start = period.find(f"{{{XBRLI_NS}}}startDate")
            end = period.find(f"{{{XBRLI_NS}}}endDate")
            instant = period.find(f"{{{XBRLI_NS}}}instant")
            ctx_periods[ctx_id] = {
                "start": start.text.strip() if start is not None and start.text else "",
                "end": end.text.strip() if end is not None and end.text else (instant.text.strip() if instant is not None and instant.text else ""),
            }

        # 3. Walk all elements that look like iXBRL facts (carry contextRef
        # AND have numeric text content). We can't use the XBRLI namespace
        # because typed values are tagged in their own taxonomies (us-gaap,
        # dei, custom).
        results: list[SegmentFact] = []
        for el in root.iter():
            ctx_ref = el.get("contextRef", "")
            if not ctx_ref or ctx_ref not in ctx_segments:
                continue
            dims = ctx_segments[ctx_ref]
            if not dims:
                continue
            # skip metadata/structural elements without decimals/unitRef
            if not el.get("unitRef"):
                continue
            # In iXBRL the element's own tag carries the qualified name
            # (e.g. {http://fasb.org/us-gaap/2024}Revenues). The ``name``
            # attribute is only set for ``xbrli:nonFraction``-wrapped
            # children of the instance document, not for inline facts.
            concept_full = el.get("name") or el.tag
            concept = _strip_ns(concept_full)
            if revenue_only and concept not in REVENUE_TAGS:
                continue
            raw_value = _to_float((el.text or "").strip())
            if raw_value is None:
                continue
            # Scale based on decimals attribute (-6 → millions, -3 → thousands, etc.)
            decimals_str = el.get("decimals", "")
            scale = 1.0
            if decimals_str and decimals_str.lstrip("-").isdigit():
                d = int(decimals_str)
                if d != 0:
                    scale = 10 ** (-d)
            value = raw_value * scale
            unit = el.get("unitRef", "USD")
            decimals = decimals_str
            period = ctx_periods.get(ctx_ref, {"start": "", "end": ""})

            # Find the first matching segment axis
            axis = ""
            member = ""
            for dim, mem in dims.items():
                axis_local = _strip_ns(dim)
                if axis_local in SEGMENT_AXES or axis_local in GEO_AXES:
                    axis = axis_local
                    member = _strip_ns(mem)
                    break
            if not axis:
                # unknown axis — still capture
                first_dim = next(iter(dims))
                axis = _strip_ns(first_dim)
                member = _strip_ns(next(iter(dims.values())))

            results.append(SegmentFact(
                concept=f"us-gaap:{concept}",
                segment_member=member,
                segment_label=member,  # caller can map ticker→prefix
                period_start=period["start"],
                period_end=period["end"],
                value=value,
                unit=unit,
                decimals=decimals,
                axis=axis,
            ))
        return results

    def latest_revenue_segments(
        self,
        cik: str,
        ticker: str,
        *,
        top_n: int = 8,
    ) -> list[dict[str, Any]]:
        """Convenience: return product-axis segment revenue for the latest 10-K/10-Q.

        Output is sorted by value desc, deduplicated by segment_member,
        keeping the latest period only and preferring quarterly over
        semiannual/annual durations.
        """
        # Try 10-Q first (more recent for earnings prep), then 10-K
        for form in ("10-Q", "10-K"):
            filing = self.find_latest_filing(cik, form=form)
            if not filing:
                continue
            try:
                body = self.fetch_ixbrl(cik, filing["accession"], filing["primaryDocument"])
            except (httpx.HTTPError, FileNotFoundError):
                continue
            facts = self.extract_segment_facts(body)
            if not facts:
                continue
            # Keep only product-axis facts (exclude geo members even when axis overlaps)
            product_facts = [
                f for f in facts
                if f.axis in SEGMENT_AXES and not f.segment_member.endswith(_GEO_MEMBER_SUFFIXES)
            ]
            if not product_facts:
                continue
            latest_end = max(f.period_end for f in product_facts)
            period_facts = [f for f in product_facts if f.period_end == latest_end]
            # de-dup by member, prefer SHORTER period duration (quarterly)
            member_facts: dict[str, SegmentFact] = {}
            for f in period_facts:
                days = _period_days(f.period_start, f.period_end)
                existing = member_facts.get(f.segment_member)
                if existing is None or days < _period_days(existing.period_start, existing.period_end):
                    member_facts[f.segment_member] = f
            unique = sorted(member_facts.values(), key=lambda f: abs(f.value), reverse=True)
            out = []
            for f in unique[:top_n]:
                d = f.to_dict()
                d["ticker"] = ticker
                d["form"] = filing["form"]
                d["filing_date"] = filing["filingDate"]
                d["accession"] = filing["accession"]
                d["primary_document"] = filing["primaryDocument"]
                out.append(d)
            return out
        return []

    def latest_revenue_geo(
        self,
        cik: str,
        ticker: str,
        *,
        top_n: int = 6,
    ) -> list[dict[str, Any]]:
        """Same as above but only geographic-axis facts."""
        # Try 10-Q first (more recent for earnings prep), then 10-K
        for form in ("10-Q", "10-K"):
            filing = self.find_latest_filing(cik, form=form)
            if not filing:
                continue
            try:
                body = self.fetch_ixbrl(cik, filing["accession"], filing["primaryDocument"])
            except (httpx.HTTPError, FileNotFoundError):
                continue
            facts = self.extract_segment_facts(body)
            if not facts:
                continue
            # Geographic facts: explicit geo axis OR geo member suffix
            geo = [
                f for f in facts
                if f.axis in GEO_AXES or f.segment_member.endswith(_GEO_MEMBER_SUFFIXES)
            ]
            if not geo:
                continue
            latest_end = max(f.period_end for f in geo)
            period_facts = [f for f in geo if f.period_end == latest_end]
            # de-dup by member, prefer SHORTER period duration
            member_facts: dict[str, SegmentFact] = {}
            for f in period_facts:
                days = _period_days(f.period_start, f.period_end)
                existing = member_facts.get(f.segment_member)
                if existing is None or days < _period_days(existing.period_start, existing.period_end):
                    member_facts[f.segment_member] = f
            unique = sorted(member_facts.values(), key=lambda f: abs(f.value), reverse=True)
            out = []
            for f in unique[:top_n]:
                d = f.to_dict()
                d["ticker"] = ticker
                d["form"] = filing["form"]
                d["filing_date"] = filing["filingDate"]
                out.append(d)
            return out
        return []


    # ------------------------------------------------------------------
    # Company Facts — consolidated fundamentals (free, no iXBRL parse)
    # ------------------------------------------------------------------
    _CF_CACHE: dict[str, dict[str, Any]] = {}

    def _fetch_companyfacts(self, cik: str) -> dict[str, Any]:
        if cik in self._CF_CACHE:
            return self._CF_CACHE[cik]
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        self._rate_limit()
        try:
            resp = httpx.get(url, headers={"User-Agent": self._ua}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._CF_CACHE[cik] = data
            return data
        except Exception:
            return {}

    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
        """Return key financial metrics from SEC companyfacts for latest quarter."""
        cf = self._fetch_companyfacts(cik)
        facts = cf.get("facts", {}).get("us-gaap", {})
        if not facts:
            return {"source": "sec-ixbrl", "ticker": ticker, "note": "no us-gaap facts"}

        result: dict[str, Any] = {"source": "sec-ixbrl", "ticker": ticker, "cik": cik}

        _MAP: dict[str, tuple[str, str]] = {
            "revenue":          ("Revenues", "USD"),
            "gross_profit":     ("GrossProfit", "USD"),
            "operating_income": ("OperatingIncomeLoss", "USD"),
            "net_income":       ("NetIncomeLoss", "USD"),
            "eps_diluted":      ("EarningsPerShareDiluted", "USD/shares"),
            "total_assets":     ("Assets", "USD"),
            "total_liabilities":("Liabilities", "USD"),
            "equity":           ("StockholdersEquity", "USD"),
            "cash":             ("CashAndCashEquivalentsAtCarryingValue", "USD"),
            "long_term_debt":   ("LongTermDebt", "USD"),
            "capex":            ("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
            "rd_expense":       ("ResearchAndDevelopmentExpense", "USD"),
            "shares_out":       ("CommonStockSharesOutstanding", "shares"),
            "inventory":        ("InventoryNet", "USD"),
            "receivables":      ("AccountsReceivableNetCurrent", "USD"),
        }

        for key, (concept, unit) in _MAP.items():
            result[key] = None
            result[f"{key}_period"] = None
            result[f"{key}_form"] = None
            entries = facts.get(concept, {}).get("units", {}).get(unit, [])
            if not entries:
                continue
            ranked = sorted(
                entries,
                key=lambda e: (
                    e.get("end", ""),
                    0 if e.get("frame") is None else 1,
                    0 if e.get("form") == "10-Q" else 1,
                ),
                reverse=(True, False, False),
            )
            if not ranked:
                continue
            best = ranked[0]
            result[key] = best.get("val")
            result[f"{key}_period"] = (best.get("end", "") or "")[:10]
            result[f"{key}_form"] = best.get("form", "")

        # Derived metrics
        rev = result.get("revenue")
        if rev and rev > 0:
            if result.get("gross_profit"):
                result["gross_margin"] = round(result["gross_profit"] / rev * 100, 1)
            if result.get("operating_income"):
                result["operating_margin"] = round(result["operating_income"] / rev * 100, 1)
            if result.get("net_income"):
                result["net_margin"] = round(result["net_income"] / rev * 100, 1)

        eq = result.get("equity")
        assets = result.get("total_assets")
        ni = result.get("net_income")
        if eq and eq != 0 and ni:
            result["roe"] = round(ni / eq * 100, 1)
        if assets and assets != 0 and ni:
            result["roa"] = round(ni / assets * 100, 1)

        cash_v = result.get("cash")
        debt_v = result.get("long_term_debt")
        if cash_v is not None and debt_v is not None:
            result["net_debt"] = debt_v - cash_v
        if debt_v and eq and eq != 0:
            result["debt_equity"] = round(debt_v / eq, 2)

        # Company name from DEI taxonomy
        dei = cf.get("facts", {}).get("dei", {})
        erc = dei.get("EntityRegistrantName", {}).get("units", {}).get("text", [])
        if erc:
            result["company_name"] = erc[-1].get("val", ticker)

        result["as_of"] = result.get("revenue_period", "")
        return result