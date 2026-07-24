"""Optional SEC backend built on the `edgartools` library.

Opt-in alternative to `calorch.sec_ixbrl.SecIxbrlClient`, selected via
`SEC_BACKEND=edgartools` (default remains `"native"` — see `calorch.config`).
`EdgarToolsClient.latest_fundamentals` returns the *exact same contract* as
`SecIxbrlClient.latest_fundamentals` (same keys), so it's a drop-in for
`calorch.providers.IxbrlFundamentalsProvider`. It also adds one capability
the native client doesn't have: `fundamentals_history`, a short quarterly
time series for trend context.

Requires the optional `edgar` extra (`pip install calorch[edgar]`); the
package is never imported at module load time so `calorch.sec_edgartools`
itself always imports cleanly. Callers that construct `EdgarToolsClient`
should catch `ImportError` and fall back to the native client (see
`calorch.providers.build_providers` and `calorch.data_ingestion`).

Value-vs-metadata split
------------------------
edgartools statements (`income_statement`, `balance_sheet`,
`cash_flow_statement`) return one internally-consistent value per period
column — e.g. the *discrete* fiscal quarter, not a year-to-date cumulative
total. The lower-level `EntityFacts.get_fact()` API, however, returns
whatever XBRL fact SEC filers reported for that label, which for flow
concepts (revenue, net income, R&D expense, capex, ...) in a mid-fiscal-year
10-Q is frequently the *cumulative* year-to-date figure, not the quarter
alone. So for flow concepts we always read the *value* from the statement
DataFrame and only use `get_fact()` for its `period_end` / `form_type`
metadata (keyed by the same period label the DataFrame uses). Instant
(balance-sheet) concepts like `Assets` or `StockholdersEquity` don't have
this cumulative problem, so we read both value and metadata straight from
`get_fact()`, falling back to it when the concept is absent from the
balance-sheet presentation entirely.
"""
from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("calorch.sec_edgartools")


@contextmanager
def _quiet_facts():
    """Silence edgartools' per-lookup ``UserWarning``.

    ``EntityFacts.get_fact()`` warns whenever a concept exists but has no
    fact for the requested period -- an outcome this module treats as
    ordinary (the caller falls through to the next synonym, or leaves the
    field ``None``). Left unsuppressed it emits a multi-line warning per
    miss per ticker, which is pure noise in Azure Functions logs.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        yield


# key -> ordered candidate XBRL concept names (without the "us-gaap:" prefix).
# First concept present in the statement wins, mirroring the synonym
# fallbacks `SecIxbrlClient` uses for iXBRL segment tags.
_INCOME_FLOW_MAP: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "rd_expense": ("ResearchAndDevelopmentExpense",),
    "cost_of_revenue": ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"),
}
_CASHFLOW_FLOW_MAP: dict[str, tuple[str, ...]] = {
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "ocf": ("NetCashProvidedByUsedInOperatingActivities",),
    "buybacks": ("PaymentsForRepurchaseOfCommonStock",),
    "dividends_paid": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
}
_INSTANT_MAP: dict[str, tuple[str, ...]] = {
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "equity": ("StockholdersEquity",),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "long_term_debt": ("LongTermDebt", "LongTermDebtNoncurrent"),
    "shares_out": ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
    "inventory": ("InventoryNet",),
    "receivables": ("AccountsReceivableNetCurrent",),
    "accounts_payable": ("AccountsPayableCurrent",),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
}
_ALL_KEYS: tuple[str, ...] = tuple(_INCOME_FLOW_MAP) + tuple(_CASHFLOW_FLOW_MAP) + tuple(_INSTANT_MAP)

# Quarterly history — income-statement flow metrics...
_HISTORY_MAP: dict[str, tuple[str, ...]] = {
    k: _INCOME_FLOW_MAP[k] for k in ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted")
}
# ...joined with cash-flow-statement and balance-sheet metrics by identical
# period label (see `fundamentals_history`).
_HISTORY_CASHFLOW_MAP: dict[str, tuple[str, ...]] = {
    k: _CASHFLOW_FLOW_MAP[k] for k in ("capex", "ocf", "buybacks", "dividends_paid")
}
_HISTORY_BALANCE_MAP: dict[str, tuple[str, ...]] = {
    k: _INSTANT_MAP[k] for k in ("current_assets", "current_liabilities")
}

# Non-period columns every edgartools statement DataFrame carries.
_METADATA_COLS = {"label", "depth", "is_abstract", "is_total", "section", "confidence"}


class EdgarToolsClient:
    """Fetches SEC fundamentals via the `edgartools` library.

    For tests (or any caller that wants to avoid the real dependency and
    network), pass `company_factory` — a callable taking a ticker (str) or
    CIK (int) and returning a `Company`-like object exposing `.cik`, `.name`,
    `.income_statement()`, `.balance_sheet()`, `.cash_flow_statement()` and
    `.get_facts()`. When omitted, `__init__` lazily imports `edgar`, calls
    `edgar.set_identity(user_agent)`, and uses the real `edgar.Company`.
    """

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path | None = None,
        *,
        company_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._ua = user_agent
        self._cache_dir = cache_dir
        if company_factory is not None:
            self._company_factory = company_factory
            return

        import edgar  # lazy import — optional dependency (pip install calorch[edgar])

        edgar.set_identity(user_agent)
        if cache_dir is not None and hasattr(edgar, "set_local_storage_path"):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                edgar.set_local_storage_path(str(cache_dir))
            except OSError as e:
                log.warning("edgartools cache_dir setup failed, continuing without it: %s", e)
        self._company_factory = edgar.Company

    # ------------------------------------------------------------------
    # CIK lookup
    # ------------------------------------------------------------------
    def cik_for(self, ticker: str) -> str | None:
        """Return a zero-padded 10-digit CIK for `ticker`, or None."""
        try:
            company = self._company_factory(ticker)
            cik = getattr(company, "cik", None)
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("edgartools cik_for failed for %s: %s", ticker, e)
            return None
        if cik is None:
            return None
        return str(cik).zfill(10)

    # ------------------------------------------------------------------
    # Fundamentals — matches SecIxbrlClient.latest_fundamentals contract
    # ------------------------------------------------------------------
    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
        result: dict[str, Any] = {"source": "sec-edgartools", "ticker": ticker, "cik": cik}
        for key in _ALL_KEYS:
            result[key] = None
            result[f"{key}_period"] = None
            result[f"{key}_form"] = None

        try:
            company = self._company_factory(int(cik))
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("edgartools company lookup failed for %s (cik=%s): %s", ticker, cik, e)
            result["note"] = f"company lookup failed: {e}"
            return result

        income_df = self._safe_statement(company, "income_statement")
        cashflow_df = self._safe_statement(company, "cash_flow_statement")
        balance_df = self._safe_statement(company, "balance_sheet")
        facts = self._safe_facts(company)

        _fill_flow(result, income_df, facts, _INCOME_FLOW_MAP)
        _fill_flow(result, cashflow_df, facts, _CASHFLOW_FLOW_MAP)
        _fill_instant(result, balance_df, facts, _INSTANT_MAP)
        _apply_derived(result)

        result["company_name"] = getattr(company, "name", None) or ticker
        result["as_of"] = result.get("revenue_period") or ""
        return result

    # ------------------------------------------------------------------
    # Quarterly history — new capability, not in the native client
    # ------------------------------------------------------------------
    def fundamentals_history(self, cik: str, ticker: str, *, quarters: int = 5) -> dict[str, Any]:
        base = {"source": "sec-edgartools", "ticker": ticker, "cik": cik}
        try:
            company = self._company_factory(int(cik))
            stmt = company.income_statement(periods=quarters, annual=False)
            df = stmt.to_dataframe()
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("edgartools fundamentals_history failed for %s: %s", ticker, e)
            return {**base, "quarterly": []}

        try:
            cols = list(df.columns)
        except Exception as e:  # noqa: BLE001
            log.warning("edgartools fundamentals_history: malformed dataframe for %s: %s", ticker, e)
            return {**base, "quarterly": []}
        period_labels = [c for c in cols if c not in _METADATA_COLS]  # newest first

        # Cash-flow / balance-sheet statements are fetched defensively, same
        # as `_safe_statement` — a failure here degrades those columns to
        # None per-row rather than failing the whole history call, since
        # `_value_at` already treats a None dataframe as "concept absent".
        cashflow_df = self._safe_statement(company, "cash_flow_statement", periods=quarters)
        balance_df = self._safe_statement(company, "balance_sheet", periods=quarters)

        quarterly: list[dict[str, Any]] = []
        for label in period_labels:
            row: dict[str, Any] = {"label": label}
            for key, concepts in _HISTORY_MAP.items():
                row[key] = None
                for concept in concepts:
                    val = _value_at(df, concept, label)
                    if val is not None:
                        row[key] = val
                        break
            rev = row.get("revenue")
            if rev:
                if row.get("gross_profit") is not None:
                    row["gross_margin"] = round(row["gross_profit"] / rev * 100, 1)
                if row.get("operating_income") is not None:
                    row["operating_margin"] = round(row["operating_income"] / rev * 100, 1)
                if row.get("net_income") is not None:
                    row["net_margin"] = round(row["net_income"] / rev * 100, 1)

            # Joined by identical period label — cash-flow-statement metrics.
            for key, concepts in _HISTORY_CASHFLOW_MAP.items():
                row[key] = None
                for concept in concepts:
                    val = _value_at(cashflow_df, concept, label)
                    if val is not None:
                        row[key] = val
                        break

            row["fcf"] = None
            row["fcf_margin"] = None
            if row.get("ocf") is not None and row.get("capex") is not None:
                row["fcf"] = row["ocf"] - row["capex"]
                if rev:
                    row["fcf_margin"] = round(row["fcf"] / rev * 100, 1)

            # Joined by identical period label — balance-sheet metrics.
            for key, concepts in _HISTORY_BALANCE_MAP.items():
                row[key] = None
                for concept in concepts:
                    val = _value_at(balance_df, concept, label)
                    if val is not None:
                        row[key] = val
                        break

            row["current_ratio"] = None
            ca = row.get("current_assets")
            cl = row.get("current_liabilities")
            if ca is not None and cl:
                row["current_ratio"] = round(ca / cl, 2)

            quarterly.append(row)

        return {**base, "quarterly": quarterly}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _safe_statement(self, company: Any, method_name: str, *, periods: int = 1) -> Any:
        try:
            method = getattr(company, method_name)
            stmt = method(periods=periods, annual=False)
            if stmt is None:
                return None
            return stmt.to_dataframe()
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("edgartools %s() failed: %s", method_name, e)
            return None

    def _safe_facts(self, company: Any) -> Any:
        try:
            return company.get_facts()
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("edgartools get_facts() failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Module-level helpers (pure, no `self` needed — easy to unit test directly)
# ---------------------------------------------------------------------------
def _period_label(df: Any) -> str | None:
    """Return the single newest period column label of a `periods=1` frame."""
    if df is None:
        return None
    try:
        cols = list(df.columns)
    except Exception:
        return None
    return cols[-1] if cols else None


def _value_at(df: Any, concept: str, period_label: str) -> Any:
    try:
        if concept not in df.index:
            return None
        val = df.loc[concept, period_label]
    except Exception:
        return None
    # A duplicated concept in the index (the same tag presented at more than
    # one depth/dimension) makes `.loc` return a Series rather than a scalar.
    # Take the first non-null entry -- the face-presentation total, which
    # edgartools orders ahead of any dimensional breakdown.
    if hasattr(val, "iloc") and not isinstance(val, (str, bytes)):
        try:
            non_null = [v for v in list(val) if v is not None and v == v]
            val = non_null[0] if non_null else None
        except (TypeError, ValueError):
            return None
    if val is None:
        return None
    try:
        if val != val:  # NaN check without importing pandas
            return None
    except TypeError:
        pass
    return val


def _period_meta(facts: Any, concept: str, period_label: str) -> tuple[str | None, str]:
    """Metadata-only lookup: never trust `get_fact()`'s *value* for flow concepts."""
    if facts is None:
        return period_label, ""
    try:
        with _quiet_facts():
            fact = facts.get_fact(f"us-gaap:{concept}", period=period_label)
    except Exception:
        fact = None
    if fact is None:
        return period_label, ""
    period_end = getattr(fact, "period_end", None)
    form = getattr(fact, "form_type", None) or ""
    period_str = period_end.isoformat() if hasattr(period_end, "isoformat") else (str(period_end) if period_end else period_label)
    return period_str, form


def _instant_from_facts(facts: Any, concept: str) -> tuple[Any, str | None, str]:
    """Value + metadata for an instant concept, straight from raw facts."""
    if facts is None:
        return None, None, ""
    try:
        with _quiet_facts():
            fact = facts.get_fact(f"us-gaap:{concept}")
    except Exception:
        fact = None
    if fact is None:
        return None, None, ""
    val = getattr(fact, "value", None)
    period_end = getattr(fact, "period_end", None)
    form = getattr(fact, "form_type", None) or ""
    period_str = period_end.isoformat() if hasattr(period_end, "isoformat") else (str(period_end) if period_end else None)
    return val, period_str, form


def _fill_flow(result: dict[str, Any], df: Any, facts: Any, mapping: dict[str, tuple[str, ...]]) -> None:
    """Flow concepts: value always from the statement DataFrame."""
    period_label = _period_label(df)
    if period_label is None:
        return
    for key, concepts in mapping.items():
        for concept in concepts:
            val = _value_at(df, concept, period_label)
            if val is None:
                continue
            result[key] = val
            period_str, form = _period_meta(facts, concept, period_label)
            result[f"{key}_period"] = period_str
            result[f"{key}_form"] = form
            break


def _fill_instant(result: dict[str, Any], df: Any, facts: Any, mapping: dict[str, tuple[str, ...]]) -> None:
    """Instant concepts: prefer the balance-sheet DataFrame, fall back to raw facts."""
    period_label = _period_label(df)
    for key, concepts in mapping.items():
        for concept in concepts:
            val: Any = None
            period_str: str | None = None
            form = ""
            if period_label is not None:
                val = _value_at(df, concept, period_label)
                if val is not None:
                    period_str, form = _period_meta(facts, concept, period_label)
            if val is None:
                val, period_str, form = _instant_from_facts(facts, concept)
            if val is None:
                continue
            result[key] = val
            result[f"{key}_period"] = period_str
            result[f"{key}_form"] = form
            break


def _apply_derived(result: dict[str, Any]) -> None:
    """Same derived-metric formulas as `SecIxbrlClient.latest_fundamentals`."""
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

    ocf = result.get("ocf")
    capex = result.get("capex")
    if ocf is not None and capex is not None:
        result["fcf"] = ocf - capex
        if rev and rev > 0:
            result["fcf_margin"] = round(result["fcf"] / rev * 100, 1)

    current_assets = result.get("current_assets")
    current_liabilities = result.get("current_liabilities")
    if current_assets is not None and current_liabilities:
        result["current_ratio"] = round(current_assets / current_liabilities, 2)
