# calorch

> **Calendar-Driven Intelligent Workflow Orchestrator** — a LangGraph
> + Azure Container Apps reference implementation of the architecture
> described as **Option C** in `Comparative_Analysis_Enterprise.docx`.

The orchestrator ingests events from **two** sources — Microsoft Graph
(Outlook calendar) **and** SEC EDGAR (live filings) — classifies each
event into one of eight workflow types, runs every type's enrichment in
parallel, generates a DOCX brief, builds a per-type HTML email,
archives it to OneDrive, drafts (or sends) the email, and persists
everything to Cosmos DB. A weekly briefing aggregates the run.

```
START
  → scan_calendar            (Graph API + SEC EDGAR adapter)
  → prefilter_keywords       (Pass 1 — deterministic, zero-cost)
  → llm_classify             (Pass 2 — GPT-4o Structured Output)
  → [fan-out] prepare_event  (enrich → DOCX → HTML preview → OneDrive)
  → approval_gate            (interrupt() before a requested send)
  → [fan-out] deliver_event  (draft/send → Calendar patch → Repo, idempotent)
  → aggregate_briefing       (cross-event weekly summary)
  → END
```

---

## Data sources

calorch reads from two calendar providers and merges them into a single
event stream. The same eight-workflow pipeline runs over both.

### 1. Microsoft Graph (Outlook)

Default source. Pulls `/me/calendar/calendarView` over a date window.
The GraphClient uses MSAL client-credentials flow with the Entra ID app
registration. In demo mode a mock client returns the bundled
`data/seed_events.json`.

### 2. SEC EDGAR (live filings)

Real-time, public. Pulls recent filings (8-K, 10-K, 10-Q, 4, DEF 14A,
SC 13G/D, 13F-HR, etc.) for a configurable ticker watchlist over a date
window. Each filing is converted to a `CalendarEvent` carrying:

| Field | Source | Notes |
|---|---|---|
| `id` | `{ticker}:{accessionNo}` | Stable, deduplicates on re-runs |
| `subject` | Form type + ticker | e.g. `8-K (AAPL)` |
| `start` / `end` | `filingDate` | Filing day, 9:30–10:00 ET (market open) |
| `web_link` | EDGAR archive URL | Preferred over OneDrive for SEC events |
| `sec_form` | `form` field | e.g. `8-K`, `10-K` |
| `sec_items` | `items` field | Comma-separated, e.g. `2.02,9.01` |
| `sec_ticker` | from TickerMap | Drives XBRL lookups in Pass 1 |
| `sec_cik` | from TickerMap | Used for XBRL companyfacts |

The SEC client (`src/calorch/sec.py`) wraps three EDGAR endpoints:

| Endpoint | Purpose |
|---|---|
| `data.sec.gov/submissions/CIK{cik}.json` | Recent filings per issuer |
| `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` | XBRL fundamentals |
| `www.sec.gov/files/company_tickers.json` | Ticker ↔ CIK map (cached) |

SEC fair-use: 9 req/sec, identified by `SEC_USER_AGENT`. The
`_RateLimiter` is thread-safe (token bucket with `threading.Lock`).
The client caches responses to `.cache/sec/` so repeated runs don't
hammer EDGAR.

### 2a. Free data sources (no paid license required)

calorch ships free, official, ToS-compliant data sources. Each is
opt-in via env var and degrades gracefully when disabled.

| Source | File | What it provides | Cost |
|---|---|---|---|
| **SEC EDGAR** | `sec.py` | Filings + XBRL fundamentals (consolidated) | free |
| **SEC iXBRL** | `sec_ixbrl.py` | Segment revenue (iPhone/Mac/Services) + geographic revenue (Americas/EMEA/APAC) parsed from inline XBRL | free |
| **SEC EFTS** | `sec_efts.py` | Full-text search across all filings (guidance/outlook/expect excerpts) | free |
| **FRED API** | `fred.py` | VIX, 10Y, oil, gold, BTC, S&P 500, CPI, unemployment | free with key (no-key works for low volume) |
| **FOMC H.15** | `fed_h15.py` | Treasury yield curve (1M to 30Y) + effective federal funds rate | free, no key |

All four sit behind `Protocol` interfaces in `src/calorch/providers.py`.
Adding a paid vendor (Tiingo for price, Refinitiv/FactSet/Bloomberg for
consensus) is a config change, not a code change.

**Generated output now includes:**

- Macro box: VIX, treasury curve, EFFR, oil, gold, BTC, S&P 500
- Segment table: latest product breakdown (iPhone / Mac / iPad / Wearables / Services for AAPL)
- Geographic table: latest geographic breakdown (Americas / Europe / Greater China / Japan / RoAPAC for AAPL)
- Guidance excerpts: real EFTS hits from 10-K/10-Q/8-K search for "outlook", "guidance", "expect"

### Form → workflow mapping (Pass 1 fast-path)

The `items` field on an 8-K is the disambiguator — Item 2.02 is always
an earnings release, Item 5.07 is a shareholder vote, etc.

| Form | Items | Workflow |
|---|---|---|
| 10-K, 10-Q | — | `earnings_call` |
| 8-K | 2.02, 2.03, 2.04, 4.01, 4.02 | `earnings_call` |
| 8-K | 7.01 (Reg FD) | `channel_check` |
| 8-K | 5.07, 5.08 | `conference` |
| 8-K | 1.01, 1.02, 5.03, 5.04, 5.05 | `management_meeting` |
| 8-K | 5.02 | `analyst_meeting` |
| 8-K | (no items / unknown) | `channel_check` (catch-all) |
| DEF 14A, PRE 14A | — | `management_meeting` |
| Form 4, 4/A | — | `analyst_meeting` |
| 13F-HR, 13F-HR/A | — | `portfolio_meeting` |
| SC 13G, SC 13D | — | `kol_meeting` |
| S-1, 424B, F-1 | — | `conference` |
| 11-K, 20-F, 40-F, 6-K | — | `internal_review` |

This pre-classification is strong enough (confidence 0.95) that Pass 2
skips the LLM call entirely for SEC-sourced events.

### XBRL enrichment

`latest_financials(ticker)` pulls the most recent revenue, net income,
and diluted EPS from XBRL `companyfacts`, sorted by `end` date. The
DOCX brief and HTML email include these in the snapshot table:

```
AAPL  Revenue $X,XXXM  Net income $XXXM  EPS $X.XX  (form 10-K, FY2024)
```

---

## Project layout

```
calorch/
├── pyproject.toml
├── langgraph.json            # LangGraph Studio entry
├── Dockerfile                # Azure Container Apps
├── .env.example              # mock-mode template
├── .env.sec.example          # SEC + real-run template
├── data/
│   └── seed_events.json      # 8 demo events (one per workflow type)
├── deploy/
│   ├── README.md             # cost comparison + ops
│   ├── containerapp.yaml     # ACA template
│   └── deploy.ps1            # one-shot bootstrap
├── scripts/
│   ├── run_demo.py           # end-to-end smoke test (mocks)
│   ├── run_sec.py            # real SEC EDGAR run
│   ├── inspect.py            # pretty-print summary.json
│   ├── inspect_sec.py        # SEC-specific inspector
│   ├── probe_sec.py          # probe a single ticker
│   ├── probe_sec2.py         # probe with items field
│   ├── probe_xbrl.py         # probe XBRL fundamentals
│   ├── pdf2md.py             # PDF→markdown utility
│   └── show_summary.py       # alt summary renderer
└── src/calorch/
    ├── state.py              # TypedDict state, Pydantic models, enums
    ├── config.py             # Settings from env
    ├── llm.py                # LLM factory: Opencode Go → Azure OpenAI → MockChatModel
    ├── tools.py              # GraphClient, OneDriveClient,
    │                         #   Repository, EnterpriseDataClient
    ├── sec.py                # SEC EDGAR client, TickerMap,
    │                         #   8-K item classification
    ├── renderers.py          # python-docx + HTML email builders,
    │                         #   8 per-type analysis builders
    ├── nodes.py              # all node functions + per-event pipeline
    ├── graph.py              # StateGraph assembly
    ├── serve.py              # FastAPI wrapper for Azure Container Apps
    └── cli.py                # `calorch run / summary / serve`
```

---

## Quick start (mock mode — no Azure, M365, or SEC required)

```powershell
# 1. install
python -m pip install -e .

# 2. run the demo
python scripts/run_demo.py

# 3. inspect what was produced
python scripts/inspect.py
Get-Content .\out\briefings\weekly.html
```

The demo:
* loads 8 seed events (one per workflow type) from `data/seed_events.json`
* classifies each one through Pass 1 (keywords) and Pass 2 (mock LLM)
* fans out 8 parallel `prepare_event` runs, then 8 `deliver_event` runs
* writes 8 DOCX briefs, 8 HTML emails, 8 OneDrive uploads, 8 follow-ups
* patches the (mock) calendar event with a link
* aggregates everything into `out/briefings/weekly.html`
* persists to `out/repository.json`

You should see, in the logs, all 8 events being processed in the same
second — that's the `Send` API fanning out in parallel.

---

## Real run against SEC EDGAR (no M365 required)

Pulls live filings from `data.sec.gov` for a watchlist of tickers,
classifies them, generates briefs. Useful for testing the full pipeline
end-to-end without Graph credentials.

```powershell
# Last 14 days, default watchlist (AAPL, MSFT, NVDA, GOOGL, AMZN, ...)
python scripts/run_sec.py

# Last 7 days, custom watchlist
python scripts/run_sec.py --days 7 --watchlist AAPL,MSFT,NVDA

# Explicit date window, restrict to 8-K + 10-Q
python scripts/run_sec.py --start 2026-05-01 --end 2026-05-31 --forms 8-K,10-Q

# Send for real (no draft mode)
python scripts/run_sec.py --send
```

The SEC run uses `LocalOneDriveClient` (writes DOCX to
`out/onedrive/`) and `JsonRepository` (writes to
`out/repository.json`) — no Azure credentials needed. XBRL
fundamentals are merged into the enterprise-data snapshot table.

Sample output for 14-day window:
```
filings     : 1034
by form     :
  · 4         612
  · 8-K       198
  · 10-Q      87
  · SC 13G    64
  · DEF 14A   41
  · 13F-HR    22
  · ...
classifications:
  · AAPL  8-K         →  earnings_call        conf=0.95  items=2.02
  · MSFT  4           →  analyst_meeting      conf=0.95
  · NVDA  8-K         →  channel_check        conf=0.95  items=7.01
  · ...
documents: 1034  emails: 1034  follow-ups: 1034
```

---

## Production CLI (M365 + Azure)

```powershell
# Run over a window, drafts only (default — does not actually send)
python -m calorch.cli run --start 2026-03-02 --end 2026-03-09

# Send for real (requires real Graph credentials)
python -m calorch.cli run --start 2026-03-02 --end 2026-03-09 --send

# Generate previews and pause before sending (local inspection only)
python -m calorch.cli run --start 2026-03-02 --end 2026-03-09 --send --interrupt

# Show repository contents
python -m calorch.cli summary
```

For resumable human approval, use the HTTP service. A send request returns
`pending_approval` after previews are prepared; resume the same LangGraph
thread with:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "$url/runs/$threadId/approval" `
  -Headers @{ "X-Calorch-API-Key" = $env:CALORCH_API_KEY } `
  -ContentType "application/json" `
  -Body '{"approved":true}'
```

---

## Production wiring

Copy `.env.example` → `.env` and fill in:

| Variable | Purpose |
|----------|---------|
| `OPENCODE_GO_API_KEY` / `OPENCODE_GO_MODEL` | **Temp / international provider** — OpenAI-compatible endpoint (`glm-5.1`, `kimi-k2.5`, `deepseek-v4-pro`, …). Overrides Azure when set. |
| `AZURE_OPENAI_API_KEY` / `_ENDPOINT` / `_DEPLOYMENT` | GPT-4o Structured Output for Pass 2 (fallback when Opencode Go is absent) |
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` | Entra ID app registration for Microsoft Graph |
| `GRAPH_USER_ID` | UPN or object id of the analyst whose calendar to scan |
| `ONEDRIVE_DRIVE_ID` | Drive where DOCX briefs are archived |
| `REPO_BACKEND=cosmos` + `COSMOS_*` | Switch from local JSON to Cosmos DB |
| `CHECKPOINT_POSTGRES_URI` | Durable LangGraph approval checkpoints across restarts |
| `CALORCH_API_KEY` | Required `X-Calorch-API-Key` value for HTTP workflow endpoints |
| `SEC_USER_AGENT` | **Required** — `"Your Name you@example.com"` |
| `SEC_WATCHLIST` | Comma-separated tickers (default: AAPL,MSFT,NVDA,GOOGL,AMZN,META,AVGO,JPM,TSLA,WMT) |
| `SEC_FORMS` | Optional filter (e.g. `8-K,10-K,10-Q`); all forms by default |
| `FACTSET_API_KEY` / `BLOOMBERG_BLPAPI_HOST` / `LSEG_CLIENT_ID` / `SPGLOBAL_API_KEY` | Enterprise data providers (license assumed held) |
| `TIINGO_API_KEY` | Optional: real EOD price + sector ETF data ($50/mo) |
| `FRED_API_KEY` | Optional: FRED macro API key (free, 30s signup) — without it, FOMC H.15 still provides treasury rates |
| `USE_FRED` / `USE_FED_H15` / `USE_IXBRL_SEGMENTS` / `USE_SEC_EFTS` | Toggle each free source on/off (all default `true`) |
| `USE_MOCKS=false` | Disable mocks — required for real runs |
| `LANGSMITH_API_KEY` | Trace every classification in LangSmith |

Then either run locally:

```powershell
python -m calorch.cli run --start 2026-06-01 --end 2026-06-08
```

…or deploy to Azure Container Apps — see `deploy/README.md`.

---

## How this maps to the ADR

| ADR row (section #) | Implementation |
|---|---|
| 1 — Outlook Calendar | `scan_calendar` → `GraphClient.list_events` (Graph SDK / MSAL) |
| 1b — SEC EDGAR (added) | `scan_calendar` → `SecAsCalendarClient` (live filings) |
| 2 — NLP Classification | `prefilter_keywords` (Pass 1) + `llm_classify` (Pass 2, typed enum) |
| 2b — Form code classifier (added) | `sec.classify_form` uses `items` field for 8-K disambiguation |
| 3 — Event Routing | `fan_out_prepare_events` + `fan_out_delivery` using LangGraph `Send` |
| 4-11 — Eight Workflows | `renderers.build_analysis` switch — earnings_call, management_meeting, conference, kol_meeting, channel_check, portfolio_meeting, internal_review, analyst_meeting |
| 12 — Email Delivery | `approval_gate` → `deliver_event`; records a Graph draft before `/send` |
| 13 — Repository | `JsonRepository` locally; `CosmosRepository` via `REPO_BACKEND=cosmos` |
| 14 — Calendar Attachments | `GraphClient.patch_event` with HTML body + OneDrive link |
| 15 — Weekly Briefing | `aggregate_briefing` node, written to `briefings/weekly.html` |
| 16 — Follow-ups | `FollowUpItem` per pipeline run, persisted into repository |
| 17-22 — Technical | Same set of tools, async via `httpx` in real client |
| 23-25 — Governance | `interrupt()` approval after previews; Postgres checkpoints; Entra ID via GraphClient |
| 26-30 — Value Props | All eight workflows are first-class; LLM analysis is repeatable; **parallel fan-out** is the headline strength |

---

## Cost profile (incremental only, per ADR §Pricing)

| Component | Cost |
|---|---|
| Azure Container Apps (Consumption, weekly job) | ~$0.30–1/mo |
| Azure Container Registry (Basic) | $5/mo |
| Azure OpenAI GPT-4o | ~$15–40/mo |
| Opencode Go (alternative) | ~$10/mo (international, OpenAI-compatible) |
| Cosmos DB Serverless | ~$0.25/GB + RUs |
| Azure Key Vault | ~$1/mo |
| Application Insights / Log Analytics | ~$2–5/mo |
| LangSmith (optional) | $0–39/seat |
| **Total** | **~$24–52/mo shared** |

M365 E3, FactSet, Bloomberg, LSEG, S&P Capital IQ, and SEC EDGAR are
assumed to be existing firm-wide licenses (SEC EDGAR is free).

See `deploy/README.md` for the full cost breakdown on Container Apps.

---

## Run logs (expected, mock mode)

```
INFO  calorch.nodes | scanning calendar 2026-03-02 → 2026-03-09
INFO  calorch.nodes | found 8 events
INFO  calorch.nodes | pipeline start event=ev-001 type=earnings_call       conf=0.99
INFO  calorch.nodes | pipeline start event=ev-002 type=management_meeting conf=0.55
INFO  calorch.nodes | pipeline start event=ev-003 type=conference         conf=0.85
INFO  calorch.nodes | pipeline start event=ev-004 type=kol_meeting        conf=0.85
INFO  calorch.nodes | pipeline start event=ev-005 type=channel_check      conf=0.99
INFO  calorch.nodes | pipeline start event=ev-006 type=portfolio_meeting  conf=0.85
INFO  calorch.nodes | pipeline start event=ev-007 type=internal_review    conf=0.99
INFO  calorch.nodes | pipeline start event=ev-008 type=analyst_meeting    conf=0.85

[ok] wrote C:\workspace\calorch\out\summary.json
[ok] artefacts in C:\workspace\calorch\out
```

---

## Documentation

| File | Purpose |
|---|---|
| `README.md` | This file — quick start, CLI, production wiring, ADR mapping |
| `docs/architecture.md` | Full architecture doc with 5 Mermaid diagrams (source) |
| `docs/architecture.html` | Rendered HTML — open in any browser, Mermaid auto-renders |
| `IMPLEMENTATION_REVIEW.md` | Code-review findings, bug/issue tracker, fix history |
| `deploy/README.md` | Azure Container Apps deploy guide + cost comparison |

The HTML architecture doc is self-contained (only loads Mermaid from a CDN).
Print stylesheet included.

---

## License

Internal — Confidential.
