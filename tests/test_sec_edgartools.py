"""Tests for the opt-in edgartools SEC backend.

No network, no real `edgar` import: `EdgarToolsClient` is driven entirely
through its injectable `company_factory` kwarg with lightweight fakes that
mimic just the surface `edgartools` exposes (`.cik`, `.name`,
`.income_statement()` / `.balance_sheet()` / `.cash_flow_statement()`
returning objects with `.to_dataframe()`, and `.get_facts()` returning an
object with `.get_fact(concept, period=...)`).
"""
from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from calorch.config import Settings
from calorch.data_ingestion import IngestionPipeline
from calorch.sec_edgartools import EdgarToolsClient


# ---------------------------------------------------------------------------
# Fakes — no pandas, no edgartools, no network.
# ---------------------------------------------------------------------------
class _Loc:
    def __init__(self, values: dict[str, dict[str, Any]]) -> None:
        self._values = values

    def __getitem__(self, key: tuple[str, str]) -> Any:
        concept, col = key
        return self._values[concept][col]


class FakeFrame:
    """Stand-in for a pandas DataFrame: `.columns`, `.index`, `.loc[concept, col]`."""

    def __init__(self, values: dict[str, dict[str, Any]], columns: list[str]) -> None:
        self._values = values
        self.columns = columns
        self.index = list(values.keys())

    @property
    def loc(self) -> _Loc:
        return _Loc(self._values)


class FakeStatement:
    def __init__(self, frame: FakeFrame | None) -> None:
        self._frame = frame

    def to_dataframe(self) -> FakeFrame | None:
        return self._frame


class FakeFact:
    def __init__(self, value: Any, period_end: date | None, form_type: str) -> None:
        self.value = value
        self.period_end = period_end
        self.form_type = form_type


class FakeFacts:
    """`facts.get_fact(concept, period=None)` -> FakeFact | None."""

    def __init__(self, entries: dict[tuple[str, str | None], FakeFact]) -> None:
        self._entries = entries

    def get_fact(self, concept: str, period: str | None = None) -> FakeFact | None:
        return self._entries.get((concept, period))


class FakeCompany:
    def __init__(
        self,
        *,
        cik: int = 320193,
        name: str = "Apple Inc.",
        income: FakeStatement | None = None,
        balance: FakeStatement | None = None,
        cashflow: FakeStatement | None = None,
        facts: FakeFacts | None = None,
        history_income: FakeStatement | None = None,
    ) -> None:
        self.cik = cik
        self.name = name
        self._income = income
        self._balance = balance
        self._cashflow = cashflow
        self._facts = facts
        self._history_income = history_income

    def income_statement(self, periods: int = 1, annual: bool = False) -> FakeStatement | None:
        if periods == 1:
            return self._income
        return self._history_income

    def balance_sheet(self, periods: int = 1, annual: bool = False) -> FakeStatement | None:
        return self._balance

    def cash_flow_statement(self, periods: int = 1, annual: bool = False) -> FakeStatement | None:
        return self._cashflow

    def get_facts(self) -> FakeFacts:
        if self._facts is None:
            raise RuntimeError("facts unavailable")
        return self._facts


# Contract keys `SecIxbrlClient.latest_fundamentals` guarantees (see sec_ixbrl.py _MAP).
CONTRACT_METRIC_KEYS = [
    "revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
    "total_assets", "total_liabilities", "equity", "cash", "long_term_debt",
    "capex", "rd_expense", "shares_out", "inventory", "receivables",
]


def _full_company() -> FakeCompany:
    """A company with every contract metric populated, internally consistent."""
    period = "Q2 2026"
    income = FakeStatement(FakeFrame(
        {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {period: 100_000.0},
            "GrossProfit": {period: 40_000.0},
            "OperatingIncomeLoss": {period: 20_000.0},
            "NetIncomeLoss": {period: 15_000.0},
            "EarningsPerShareDiluted": {period: 2.0},
            "ResearchAndDevelopmentExpense": {period: 8_000.0},
        },
        columns=["label", "depth", period],
    ))
    cashflow = FakeStatement(FakeFrame(
        {"PaymentsToAcquirePropertyPlantAndEquipment": {period: 5_000.0}},
        columns=["label", period],
    ))
    balance = FakeStatement(FakeFrame(
        {
            "Assets": {period: 200_000.0},
            "Liabilities": {period: 80_000.0},
            "StockholdersEquity": {period: 120_000.0},
            "CashAndCashEquivalentsAtCarryingValue": {period: 50_000.0},
            "LongTermDebt": {period: 30_000.0},
            "InventoryNet": {period: 10_000.0},
            "AccountsReceivableNetCurrent": {period: 12_000.0},
            # shares_out deliberately absent from the presentation -> exercises
            # the raw-facts fallback path in _fill_instant.
        },
        columns=["label", period],
    ))
    facts_entries: dict[tuple[str, str | None], FakeFact] = {}
    for concept in (
        "RevenueFromContractWithCustomerExcludingAssessedTax", "GrossProfit", "OperatingIncomeLoss",
        "NetIncomeLoss", "EarningsPerShareDiluted", "ResearchAndDevelopmentExpense",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    ):
        facts_entries[(f"us-gaap:{concept}", period)] = FakeFact(None, date(2026, 3, 28), "10-Q")
    for concept in (
        "Assets", "Liabilities", "StockholdersEquity", "CashAndCashEquivalentsAtCarryingValue",
        "LongTermDebt", "InventoryNet", "AccountsReceivableNetCurrent",
    ):
        val = {
            "Assets": 200_000.0, "Liabilities": 80_000.0, "StockholdersEquity": 120_000.0,
            "CashAndCashEquivalentsAtCarryingValue": 50_000.0, "LongTermDebt": 30_000.0,
            "InventoryNet": 10_000.0, "AccountsReceivableNetCurrent": 12_000.0,
        }[concept]
        facts_entries[(f"us-gaap:{concept}", period)] = FakeFact(val, date(2026, 3, 28), "10-Q")
    # shares_out only reachable via the no-period raw-facts fallback.
    facts_entries[("us-gaap:CommonStockSharesOutstanding", None)] = FakeFact(
        1_000.0, date(2026, 3, 28), "10-Q"
    )
    facts = FakeFacts(facts_entries)
    return FakeCompany(income=income, cashflow=cashflow, balance=balance, facts=facts)


# ---------------------------------------------------------------------------
# Contract parity + derived metrics
# ---------------------------------------------------------------------------
def test_latest_fundamentals_contract_key_parity() -> None:
    company = _full_company()
    client = EdgarToolsClient("test agent test@example.com", company_factory=lambda _ident: company)

    result = client.latest_fundamentals("0000320193", "AAPL")

    assert result["source"] == "sec-edgartools"
    assert result["ticker"] == "AAPL"
    assert result["cik"] == "0000320193"
    assert result["company_name"] == "Apple Inc."
    for key in CONTRACT_METRIC_KEYS:
        assert key in result, f"missing contract key {key}"
        assert f"{key}_period" in result
        assert f"{key}_form" in result
        assert result[key] is not None, f"{key} unexpectedly None"
    assert result["as_of"] == result["revenue_period"]


def test_latest_fundamentals_values_and_period_metadata() -> None:
    company = _full_company()
    client = EdgarToolsClient("test agent test@example.com", company_factory=lambda _ident: company)

    result = client.latest_fundamentals("0000320193", "AAPL")

    assert result["revenue"] == 100_000.0
    assert result["net_income"] == 15_000.0
    assert result["eps_diluted"] == 2.0
    assert result["revenue_period"] == "2026-03-28"
    assert result["revenue_form"] == "10-Q"
    # shares_out came from the raw-facts fallback (absent from the balance sheet frame)
    assert result["shares_out"] == 1_000.0
    assert result["shares_out_form"] == "10-Q"


def test_latest_fundamentals_margins_derivation() -> None:
    company = _full_company()
    client = EdgarToolsClient("test agent test@example.com", company_factory=lambda _ident: company)

    result = client.latest_fundamentals("0000320193", "AAPL")

    assert result["gross_margin"] == round(40_000.0 / 100_000.0 * 100, 1)
    assert result["operating_margin"] == round(20_000.0 / 100_000.0 * 100, 1)
    assert result["net_margin"] == round(15_000.0 / 100_000.0 * 100, 1)
    assert result["roe"] == round(15_000.0 / 120_000.0 * 100, 1)
    assert result["roa"] == round(15_000.0 / 200_000.0 * 100, 1)
    assert result["net_debt"] == 30_000.0 - 50_000.0
    assert result["debt_equity"] == round(30_000.0 / 120_000.0, 2)


# ---------------------------------------------------------------------------
# Degraded shapes
# ---------------------------------------------------------------------------
def test_latest_fundamentals_degrades_on_factory_exception() -> None:
    def _boom(_ident: Any) -> Any:
        raise RuntimeError("no such company")

    client = EdgarToolsClient("test agent test@example.com", company_factory=_boom)
    result = client.latest_fundamentals("0000000000", "NOPE")

    assert result["source"] == "sec-edgartools"
    assert result["ticker"] == "NOPE"
    assert result["cik"] == "0000000000"
    assert "note" in result
    # per-metric keys still present (initialized before the failing lookup) and None
    for key in CONTRACT_METRIC_KEYS:
        assert result[key] is None


def test_latest_fundamentals_missing_statements_yields_all_none() -> None:
    company = FakeCompany(income=None, balance=None, cashflow=None, facts=None)
    client = EdgarToolsClient("test agent test@example.com", company_factory=lambda _ident: company)

    result = client.latest_fundamentals("0000320193", "AAPL")

    assert result["source"] == "sec-edgartools"
    for key in CONTRACT_METRIC_KEYS:
        assert result[key] is None
    assert "gross_margin" not in result
    assert result["as_of"] == ""


def test_fundamentals_history_degrades_to_empty_on_exception() -> None:
    def _boom(_ident: Any) -> Any:
        raise RuntimeError("boom")

    client = EdgarToolsClient("test agent test@example.com", company_factory=_boom)
    result = client.fundamentals_history("0000320193", "AAPL")

    assert result == {"source": "sec-edgartools", "ticker": "AAPL", "cik": "0000320193", "quarterly": []}


# ---------------------------------------------------------------------------
# fundamentals_history — ordering, labels, margins
# ---------------------------------------------------------------------------
def test_fundamentals_history_ordering_and_labels() -> None:
    labels = ["Q2 2026", "Q1 2026", "Q4 2025"]
    revenue = {"Q2 2026": 100.0, "Q1 2026": 90.0, "Q4 2025": 80.0}
    gross_profit = {"Q2 2026": 40.0, "Q1 2026": 30.0, "Q4 2025": 20.0}
    net_income = {"Q2 2026": 10.0, "Q1 2026": 9.0, "Q4 2025": 8.0}
    frame = FakeFrame(
        {
            "RevenueFromContractWithCustomerExcludingAssessedTax": revenue,
            "GrossProfit": gross_profit,
            "NetIncomeLoss": net_income,
        },
        columns=["label", "depth", *labels],
    )
    company = FakeCompany(history_income=FakeStatement(frame))
    client = EdgarToolsClient("test agent test@example.com", company_factory=lambda _ident: company)

    result = client.fundamentals_history("0000320193", "AAPL", quarters=3)

    assert result["source"] == "sec-edgartools"
    assert [row["label"] for row in result["quarterly"]] == labels  # newest first, as given
    first = result["quarterly"][0]
    assert first["revenue"] == 100.0
    assert first["gross_profit"] == 40.0
    assert first["net_income"] == 10.0
    assert first["gross_margin"] == round(40.0 / 100.0 * 100, 1)
    assert first["operating_income"] is None  # not populated in this fixture
    assert "operating_margin" not in first
    assert first["net_margin"] == round(10.0 / 100.0 * 100, 1)


# ---------------------------------------------------------------------------
# cik_for
# ---------------------------------------------------------------------------
def test_cik_for_zero_pads() -> None:
    company = FakeCompany(cik=320193)
    client = EdgarToolsClient("test agent test@example.com", company_factory=lambda _ticker: company)
    assert client.cik_for("AAPL") == "0000320193"


def test_cik_for_returns_none_on_exception() -> None:
    def _boom(_ticker: str) -> Any:
        raise RuntimeError("unknown ticker")

    client = EdgarToolsClient("test agent test@example.com", company_factory=_boom)
    assert client.cik_for("NOPE") is None


# ---------------------------------------------------------------------------
# Backend selection — providers.build_providers
# ---------------------------------------------------------------------------
@pytest.fixture
def base_settings() -> Settings:
    return Settings(
        azure_openai_api_key=None,
        azure_openai_endpoint=None,
        azure_openai_deployment="gpt-4o",
        azure_openai_api_version="2024-08-01-preview",
        graph_tenant_id=None,
        graph_client_id=None,
        graph_client_secret=None,
        graph_user_id="me",
        onedrive_drive_id=None,
        repo_backend="json",
        repo_path=Path("./out/repository.json"),
        repo_table_name="calorchdelivery",
        search_endpoint=None,
        search_index="calorch-knowledge",
        search_api_key=None,
        search_semantic_config=None,
        rag_top_k=4,
        knowledge_writeback=True,
        approver_emails=[],
        approval_base_url=None,
        opencode_go_api_key=None,
        opencode_go_model="glm-5.1",
        sec_user_agent="Test/test@example.com",
        sec_cache_dir=Path(".cache/sec"),
        sec_watchlist=["AAPL"],
        sec_forms=None,
        use_ixbrl_segments=True,
        use_sec_efts=True,
        sec_backend="native",
        alphasense_api_key=None,
        alphasense_client_id=None,
        alphasense_client_secret=None,
        alphasense_username=None,
        alphasense_password=None,
        alphasense_base_url="https://api.alpha-sense.com",
        use_alphasense=True,
        narrative_backend="alphasense",
        sentiment_backend="alphasense",
        use_mocks=True,
        output_dir=Path("./out"),
        langsmith_api_key=None,
        langsmith_project="calorch",
        langsmith_tracing=False,
        azure_storage_connection_string=None,
        azure_storage_account_url=None,
        blob_input_container="calorch-inputs",
        blob_output_container="calorch-outputs",
        blob_local_root=None,
        use_blob_providers=False,
    )


def test_default_backend_is_native(base_settings: Settings) -> None:
    from calorch.providers import IxbrlFundamentalsProvider, build_providers
    from calorch.sec_ixbrl import SecIxbrlClient

    bundle = build_providers(base_settings)
    assert isinstance(bundle.fundamentals, IxbrlFundamentalsProvider)
    assert isinstance(bundle.fundamentals._ixbrl, SecIxbrlClient)
    assert not any(s["source_name"] == "SEC edgartools" for s in bundle.sources)


def test_backend_edgartools_constructs_edgar_client(base_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import calorch.sec_edgartools as sec_edgartools_mod

    constructed: list[Any] = []

    class StubEdgarToolsClient:
        def __init__(self, user_agent: str, cache_dir: Path | None = None) -> None:
            self.user_agent = user_agent
            self.cache_dir = cache_dir
            constructed.append(self)

        def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
            return {"source": "sec-edgartools", "ticker": ticker, "cik": cik}

    monkeypatch.setattr(sec_edgartools_mod, "EdgarToolsClient", StubEdgarToolsClient)
    settings = dataclasses.replace(base_settings, sec_backend="edgartools")

    from calorch.providers import build_providers

    bundle = build_providers(settings)

    assert len(constructed) == 1
    assert bundle.fundamentals._ixbrl is constructed[0]
    assert any(s["source_name"] == "SEC edgartools" and s["status"] == "active" for s in bundle.sources)
    assert bundle.fundamentals.latest_fundamentals("0000320193", "AAPL")["source"] == "sec-edgartools"


def test_backend_edgartools_falls_back_to_native_on_import_error(
    base_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_edgartools as sec_edgartools_mod

    class ExplodingEdgarToolsClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError("edgartools not installed")

    monkeypatch.setattr(sec_edgartools_mod, "EdgarToolsClient", ExplodingEdgarToolsClient)
    settings = dataclasses.replace(base_settings, sec_backend="edgartools")

    from calorch.providers import IxbrlFundamentalsProvider, build_providers
    from calorch.sec_ixbrl import SecIxbrlClient

    bundle = build_providers(settings)

    assert isinstance(bundle.fundamentals, IxbrlFundamentalsProvider)
    assert isinstance(bundle.fundamentals._ixbrl, SecIxbrlClient)
    assert any(s["source_name"] == "SEC edgartools" and s["status"] == "error" for s in bundle.sources)


# ---------------------------------------------------------------------------
# Backend selection — data_ingestion.IngestionPipeline
# ---------------------------------------------------------------------------
def test_ingestion_pipeline_uses_native_client_by_default(tmp_path: Path) -> None:
    from calorch.sec_ixbrl import SecIxbrlClient

    pipeline = IngestionPipeline(blob_store=object(), date="20260101")
    pipeline._s = dataclasses.replace(pipeline._s, sec_cache_dir=tmp_path)

    client = pipeline._fundamentals_client()
    assert isinstance(client, SecIxbrlClient)


def test_ingestion_pipeline_selects_edgartools_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_edgartools as sec_edgartools_mod

    class StubEdgarToolsClient:
        def __init__(self, user_agent: str, cache_dir: Path | None = None) -> None:
            self.user_agent = user_agent
            self.cache_dir = cache_dir

    monkeypatch.setattr(sec_edgartools_mod, "EdgarToolsClient", StubEdgarToolsClient)

    pipeline = IngestionPipeline(blob_store=object(), date="20260101")
    pipeline._s = dataclasses.replace(pipeline._s, sec_cache_dir=tmp_path, sec_backend="edgartools")

    client = pipeline._fundamentals_client()
    assert isinstance(client, StubEdgarToolsClient)


def test_ingestion_pipeline_falls_back_to_native_on_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calorch.sec_ixbrl import SecIxbrlClient

    def _raise_import_error(*args: Any, **kwargs: Any) -> Any:
        raise ImportError("edgartools not installed")

    monkeypatch.setattr("calorch.sec_edgartools.EdgarToolsClient", _raise_import_error)

    pipeline = IngestionPipeline(blob_store=object(), date="20260101")
    pipeline._s = dataclasses.replace(pipeline._s, sec_cache_dir=tmp_path, sec_backend="edgartools")

    client = pipeline._fundamentals_client()
    assert isinstance(client, SecIxbrlClient)
