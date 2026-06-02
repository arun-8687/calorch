# calorch

> **Calendar-Driven Intelligent Workflow Orchestrator** — a LangGraph
> + Azure Container Apps engine that reads Outlook calendar events,
> classifies them into one of eight research workflow types, enriches
> each with live SEC EDGAR / FRED / Tiingo data, generates a prep-pack
> DOCX brief with LLM-powered narrative, and drafts an HTML email.

```
START
  → scan_calendar       (Graph API — Outlook calendar ONLY)
  → prefilter_keywords  (Pass 1 — deterministic keyword scoring)
  → llm_classify        (Pass 2 — LLM, model-agnostic JSON output)
  → [fan-out] prepare_event
        ├── providers.fundamentals  → SEC iXBRL (revenue, EPS, margins, balance sheet)
        ├── providers.segments      → SEC iXBRL (product + geographic revenue)
        ├── providers.narrative     → SEC EFTS (guidance excerpts)
        ├── providers.macro         → FRED + FOMC H.15 (rates, VIX, oil, gold, BTC)
        ├── providers.price         → Tiingo (EOD price, market cap)
        ├── providers.consensus     → Tiingo (analyst estimates, ratings)
        ├── TemplateEngine          → JSON template per event type
        └── LlmEnricher            → Opencode Go / Azure OpenAI narrative
  → approval_gate        (optional human-in-the-loop interrupt)
  → [fan-out] deliver_event
  → aggregate_briefing
  → END
```

---

## Data sources — live, no stubs

Every provider is live. No stub data, no curated demo data, no mocks in the data path.
When a provider lacks credentials it returns empty data with a clear `note` explaining why.

| Provider | Source | Cost | Data |
|----------|--------|------|------|
| **SEC iXBRL Fundamentals** | `data.sec.gov/api/xbrl/companyfacts/` | Free | Revenue, EPS, gross/operating/net margins, ROE, ROA, assets, liabilities, cash, debt, capex, R&D — all quarterly |
| **SEC iXBRL Segments** | iXBRL instance docs from 10-Q/10-K | Free | Product segment revenue (iPhone/Mac/Services) + geographic revenue (Americas/EMEA/APAC) |
| **SEC EFTS** | `efts.sec.gov/LATEST/search-index` | Free | Full-text filing search — guidance, outlook, risk factor excerpts |
| **FRED** | `api.stlouisfed.org` | Free (key optional) | VIX, S&P 500, oil, gold, BTC, CPI, unemployment, DFF |
| **FOMC H.15** | `federalreserve.gov` (scraped) | Free | Full Treasury yield curve (1M → 30Y) + effective fed funds rate |
| **Tiingo** | `api.tiingo.com` | $50/mo | EOD prices, market cap, analyst consensus, price targets |

Every report ends with a **Data Sources table** showing provider status:

| Provider | Status | Detail |
|----------|--------|--------|
| SEC iXBRL Fundamentals | ACTIVE | Revenue, EPS, margins, balance sheet, cash flow |
| SEC iXBRL | ACTIVE | Company facts + segment revenue |
| SEC EFTS | ACTIVE | Full-text filing search |
| FRED | ACTIVE | Federal Reserve Economic Data |
| FOMC H.15 | ACTIVE | US Treasury / Fed rates |
| Tiingo | MISSING | TIINGO_API_KEY not set |

---

## Templates — no hardcoded content

All 8 report types are defined as JSON templates in `data/templates/`, modeled on
real equity research prep packs. Each template specifies:

| Field | Purpose |
|-------|---------|
| `sections` | Ordered section list — title, content source (`llm` / `data` / `static`), fallback text |
| `llm_method` | Which `LlmEnricher` method to call (`enrich_headline`, `enrich_key_questions`, etc.) |
| `prompt_addendum` | Section-specific prompt text appended to the baseline system prompt |
| `table_type` | `two_col` (Metric\|Value) or `multi_col` with configurable headers |
| `rows_from` | Key linking to data built by the renderer (live provider data) |

`src/calorch/templates.py` loads templates and resolves variables with live data.

---

## LLM — model-agnostic, thinking-filtered

| Feature | Detail |
|---------|--------|
| **Classification** | Plain `invoke()` with JSON prompt — no `response_format` / structured-output requirement. Works with DeepSeek, kimi, GLM, Azure OpenAI |
| **Enrichment** | `LlmEnricher` generates narrative bullets for all 8 event types. Headline, guidance, margin walk, risk factors, key questions, channel check questionnaire |
| **Grounding** | Every prompt includes: *"ONLY use data explicitly provided in context. Do NOT use training data."* |
| **Thinking-block filter** | 150+ phrase blacklist + 70% threshold — if the model outputs chain-of-thought reasoning instead of bullets, the response is discarded and template fallback is used |
| **Fallback** | Every section has data-driven fallback content in the template — no blank sections |

Supported models via Opencode Go endpoint: `deepseek-v4-pro`, `deepseek-v4-flash`, `kimi-k2.5`, `kimi-k2.6`, `glm-5.1`, `glm-5`.

---

## Project layout

```
calorch/
├── pyproject.toml
├── langgraph.json               # LangGraph Studio entry
├── Dockerfile                   # Azure Container Apps
├── .env.example                 # template — copy to .env
├── data/
│   ├── seed_events.json         # 16 demo events (2 per workflow type)
│   └── templates/               # 8 JSON report templates
│       ├── earnings_call.json
│       ├── management_meeting.json
│       ├── conference.json
│       ├── kol_meeting.json
│       ├── channel_check.json
│       ├── portfolio_meeting.json
│       ├── internal_review.json
│       └── analyst_meeting.json
├── deploy/
│   ├── README.md
│   ├── containerapp.yaml
│   └── deploy.ps1
├── scripts/
│   ├── run_demo.py              # end-to-end smoke test
│   ├── run_sec.py               # real SEC EDGAR run
│   └── render_architecture.py   # markdown → HTML with Mermaid
├── docs/
│   ├── architecture.md
│   └── evaluations/             # ADR, data-source, implementation reviews
├── tests/
│   ├── test_graph.py            # 3 end-to-end tests
│   ├── test_renderers.py        # DOCX + HTML rendering
│   ├── test_llm_enrich.py       # LLM enrichment layer
│   ├── test_llm.py              # LLM factory (Opencode Go / Azure / Mock)
│   ├── test_providers.py        # Provider dispatch + live provider units
│   ├── test_sec_providers.py    # iXBRL parser + EFTS client
│   ├── test_fred.py             # FRED + FOMC H.15
│   ├── test_classifier.py       # Classification heuristics
│   ├── test_tools.py            # GraphClient, OneDrive, Repository
│   └── test_serve.py            # FastAPI /health, /run
└── src/calorch/
    ├── state.py                 # TypedDict state, Pydantic models, enums
    ├── config.py                # Settings from environment
    ├── graph.py                 # StateGraph assembly
    ├── nodes.py                 # All node functions + per-event pipeline
    ├── renderers.py             # DOCX + HTML builders, build_analysis dispatch
    ├── _earnings_helpers.py     # Financial table builders + formatters
    ├── templates.py             # Template engine — JSON → EventAnalysis
    ├── llm.py                   # LLM factory: Opencode Go → Azure → MockChatModel
    ├── llm_enrich.py            # LLM enrichment with thinking-block filter
    ├── providers.py             # ProviderBundle — Protocol-based live data layer
    ├── tools.py                 # GraphClient, OneDriveClient, Repository, make_providers
    ├── sec.py                   # SEC EDGAR client, TickerMap, form classification
    ├── sec_ixbrl.py             # iXBRL parser + companyfacts fundamentals
    ├── sec_efts.py              # SEC full-text search client
    ├── fred.py                  # FRED API client
    ├── fed_h15.py               # FOMC H.15 yield curve scraper
    ├── tiingo.py                # Tiingo API client (prices + consensus)
    ├── serve.py                 # FastAPI /health, /run, /runs/{id}/approval
    └── cli.py                   # `calorch run / summary / serve`
```

---

## Quick start

```powershell
# 1. Install
python -m pip install -e .

# 2. Run demo (no keys needed — uses seed events + MockChatModel)
python scripts/run_demo.py

# 3. Real run with Opencode Go LLM
$env:OPENCODE_GO_API_KEY = "sk-..."
$env:OPENCODE_GO_MODEL = "deepseek-v4-pro"
python -m calorch.cli run --start 2026-06-01 --end 2026-06-08

# 4. With live prices (optional — Tiingo $50/mo)
$env:TIINGO_API_KEY = "your-key"
python -m calorch.cli run --start 2026-06-01 --end 2026-06-08
```

---

## Production wiring

Copy `.env.example` → `.env`:

| Variable | Purpose |
|----------|---------|
| `OPENCODE_GO_API_KEY` / `OPENCODE_GO_MODEL` | Opencode Go LLM (`deepseek-v4-pro`, `kimi-k2.6`, `glm-5.1`, …) |
| `AZURE_OPENAI_API_KEY` / `_ENDPOINT` / `_DEPLOYMENT` | Azure OpenAI (fallback when Opencode Go absent) |
| `TIINGO_API_KEY` | Real EOD prices + analyst consensus ($50/mo) |
| `FRED_API_KEY` | FRED macro API key (free) — no-key calls work for low volume |
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` | Entra ID app for Outlook calendar |
| `GRAPH_USER_ID` | UPN of analyst whose calendar to scan |
| `SEC_USER_AGENT` | **Required** — `"Your Name you@example.com"` |
| `SEC_WATCHLIST` | Comma-separated tickers (default: AAPL,MSFT,NVDA,GOOGL,AMZN,…) |
| `USE_FRED` / `USE_FED_H15` / `USE_IXBRL_SEGMENTS` / `USE_SEC_EFTS` | Toggle free sources (all default `true`) |
| `CALORCH_API_KEY` | Required `X-Calorch-API-Key` for HTTP endpoints |
| `CHECKPOINT_POSTGRES_URI` | Durable LangGraph checkpoints across restarts |
| `FACTSET_API_KEY` / `BLOOMBERG_BLPAPI_HOST` / `LSEG_CLIENT_ID` | Enterprise terminal data (future) |

---

## Event types

| # | Type | Enrichment | Template |
|---|------|-----------|----------|
| 1 | `earnings_call` | Executive snapshot, guidance, margin walk, risk factors, key questions, financial tables, segment + geo breakdowns, analyst sentiment, ESG, price performance | `earnings_call.json` |
| 2 | `management_meeting` | Executive summary, key questions for management, risk factors, macro context | `management_meeting.json` |
| 3 | `conference` | Company overview, recent developments, key questions for 1x1s, risk factors, ESG & governance | `conference.json` |
| 4 | `kol_meeting` | Pre-call research, discussion guide, hypotheses tracker, note-taking template | `kol_meeting.json` |
| 5 | `channel_check` | Revenue overview, model assumptions, standardized questionnaire (15-20 Q), channel finding tracker | `channel_check.json` |
| 6 | `portfolio_meeting` | Market context, sector performance, holdings snapshot, key movers, upcoming catalysts | `portfolio_meeting.json` |
| 7 | `internal_review` | Coverage universe, research activity, performance review, outstanding items | `internal_review.json` |
| 8 | `analyst_meeting` | Analyst profile, debate points, key questions, risk factors, quoted view | `analyst_meeting.json` |

---

## Cost profile

| Component | Cost |
|---|---|
| Azure Container Apps (Consumption, weekly job) | ~$0.30/mo |
| Azure Container Registry (Basic) | $5/mo |
| Opencode Go LLM (~100 calls/week) | ~$10/mo |
| SEC EDGAR / FRED / FOMC H.15 | Free |
| Tiingo (optional) | $50/mo |
| Cosmos DB Serverless | ~$0.25/mo |
| Application Insights | ~$2-5/mo |
| **Total** | **~$18-70/mo** |

---

## Tests

```powershell
# Full suite
python -m pytest tests/ -q

# Per module
python -m pytest tests/test_providers.py -q
python -m pytest tests/test_sec_providers.py -q
python -m pytest tests/test_graph.py -q
```

57 tests. No network needed — tests use MockChatModel + inline HTTP mocks.

---

## License

Internal — Confidential.
