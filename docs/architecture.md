# calorch — Solution Architecture (Azure Container Apps)

> End-to-end architecture of the Calendar-Driven Intelligent Workflow
> Orchestrator, deployed on Azure Container Apps (Consumption tier).
> Covers data sources, classification, parallel fan-out, delivery,
> persistence, governance, and observability.

---

## High-level flow

```mermaid
flowchart TB
    subgraph TRIGGERS["⏰ Triggers"]
        CRON["ACA cron scale rule<br/>0 6 * * MON"]
        API["POST /run<br/>(HTTP)"]
    end

    subgraph ACA["Azure Container Apps (calorch)"]
        LB["Ingress / FQDN<br/>FastAPI :8000"]
        GRAPH["StateGraph<br/>(LangGraph)"]

        subgraph FRONT["Front stage"]
            SCAN["scan_calendar<br/>Graph + SEC EDGAR"]
            KW["prefilter_keywords<br/>Pass 1"]
            LLM["llm_classify<br/>Pass 2 · GPT-4o SO"]
        end

        subgraph FANOUT["Prepare fan-out (Send API, parallel)"]
            P1["prepare_event #1"]
            P2["prepare_event #2"]
            PN["prepare_event #N"]
        end

        GATE["approval_gate<br/>interrupt() before send"]
        DELIVER["deliver_event fan-out<br/>draft/send · patch · repo"]
        AGG["aggregate_briefing<br/>weekly.html"]
    end

    subgraph EXTERNAL["External services"]
        M365["Microsoft 365<br/>Graph · Outlook · OneDrive"]
        SEC["SEC EDGAR<br/>submissions · XBRL · tickers"]
        OPENAI["Azure OpenAI<br/>GPT-4o · Structured Output"]
    end

    subgraph AZURE["Azure platform"]
        COSMOS[("Cosmos DB<br/>events container")]
        PG[("PostgreSQL<br/>LangGraph checkpoints")]
        ACR[("ACR Basic<br/>calorch:tag")]
        LAW[("Log Analytics<br/>App Insights")]
        KEYVAULT[("Key Vault<br/>secrets")]
    end

    subgraph OBS["Observability"]
        SM["LangSmith<br/>traces · evals"]
    end

    CRON --> LB
    API --> LB
    LB --> GRAPH
    GRAPH --> SCAN --> KW --> LLM
    LLM --> P1
    LLM --> P2
    LLM --> PN
    P1 & P2 & PN --> GATE --> DELIVER --> AGG

    SCAN <-->|read| M365
    SCAN <-->|read| SEC
    LLM <-->|invoke| OPENAI
    P1 & P2 & PN <-->|read| M365
    P1 & P2 & PN <-->|XBRL fallback| SEC
    DELIVER <-->|draft/send + patch| M365
    DELIVER -->|upsert| COSMOS
    P1 & P2 & PN -.->|upload| M365
    GATE -.->|checkpoint| PG

    GRAPH -.->|traces| SM
    ACA -->|logs| LAW
    KEYVAULT -.->|secrets| ACA
    ACR -.->|pull| ACA
```

---

## Detailed component diagram

```mermaid
flowchart TB
    classDef trigger fill:#FFF4E1,stroke:#D97706,color:#7C2D12
    classDef container fill:#E0F2FE,stroke:#0369A1,color:#0C4A6E
    classDef stage fill:#DBEAFE,stroke:#1D4ED8,color:#1E3A8A
    classDef parallel fill:#FEF3C7,stroke:#D97706,color:#78350F
    classDef external fill:#F3E8FF,stroke:#7C3AED,color:#581C87
    classDef azure fill:#E0E7FF,stroke:#4338CA,color:#312E81
    classDef store fill:#DCFCE7,stroke:#15803D,color:#14532D
    classDef obs fill:#FCE7F3,stroke:#BE185D,color:#831843

    %% ====== TRIGGERS ======
    subgraph T1["TRIGGERS"]
        direction TB
        CRON["ACA cron scale rule<br/>(KEDA · weekly)"]:::trigger
        HTTPC["POST /run<br/>(on-demand)"]:::trigger
        INT["interrupt()<br/>approval_gate<br/>(human approval)"]:::trigger
    end

    %% ====== AZURE CONTAINER APPS ======
    subgraph T2["AZURE CONTAINER APPS  ·  rg-calorch-prod  ·  eastus"]
        direction TB
        subgraph T2A["Container App: calorch  (1 vCPU · 2 Gi · min=0 max=3)"]
            direction TB
            INGRESS["Ingress / FQDN<br/>HTTPS · managed cert"]:::container
            SERVE["FastAPI · uvicorn<br/>calorch.serve:app"]:::container
            CHECK["PostgresSaver<br/>(durable thread checkpoint)"]:::container

            subgraph T2B["LangGraph StateGraph"]
                direction LR
                N1["scan_calendar<br/>(Graph + SEC adapter)"]:::stage
                N2["prefilter_keywords<br/>Pass 1 · zero-cost"]:::stage
                N3["llm_classify<br/>Pass 2 · typed enum"]:::stage
                N1 --> N2 --> N3

                subgraph T2C["PREPARE FAN-OUT  (Send API)"]
                    direction TB
                    W1["prepare #1<br/>enrich→docx→html→drive"]:::parallel
                    W2["prepare #2"]:::parallel
                    WN["prepare #N<br/>(1,000+ events/wk)"]:::parallel
                end
                N3 -.->|"Send(payload)"| W1
                N3 -.->|"Send(payload)"| W2
                N3 -.->|"Send(payload)"| WN

                GATE["approval_gate<br/>interrupt() before requested send"]:::stage
                DELIVER["deliver_event fan-out<br/>draft/send→calendar→repo"]:::parallel
                N4["aggregate_briefing<br/>cross-event summary"]:::stage
                W1 & W2 & WN --> GATE --> DELIVER --> N4
            end
            INGRESS --> SERVE --> CHECK
            SERVE --> N1
        end
    end

    %% ====== EXTERNAL SERVICES ======
    subgraph T3["EXTERNAL SERVICES"]
        direction TB
        subgraph T3A["Microsoft 365 tenant"]
            M1["Graph API<br/>/me/calendar/calendarView"]:::external
            M2["Graph API<br/>sendMail / createDraft"]:::external
            M3["OneDrive for Business<br/>DOCX archive"]:::external
        end
        subgraph T3B["SEC EDGAR  (data.sec.gov)"]
            S1["submissions/CIK{cik}.json<br/>filings list"]:::external
            S2["xbrl/companyfacts/<br/>CIK{cik}.json"]:::external
            S3["company_tickers.json<br/>(CIK ↔ ticker)"]:::external
        end
        OAI["Azure OpenAI<br/>GPT-4o · Structured Output<br/>deployment: gpt-4o"]:::external
    end

    %% ====== AZURE PLATFORM ======
    subgraph T4["AZURE PLATFORM"]
        direction TB
        COSMOS[("Cosmos DB<br/>serverless · SQL API<br/>db=calorch · container=events<br/>partition key: /event_id")]:::store
        PG[("PostgreSQL<br/>LangGraph checkpoints")]:::store
        KV[("Key Vault<br/>graph-secret · cosmos-key<br/>openai-key · langsmith-key")]:::store
        CR[("ACR Basic<br/>calorch.azurecr.io<br/>calorch:tag")]:::store
        LA[("Log Analytics<br/>+ App Insights<br/>(Container Apps env)")]:::store
    end

    %% ====== OBSERVABILITY ======
    subgraph T5["OBSERVABILITY"]
        direction TB
        LS["LangSmith<br/>traces · evals · datasets"]:::obs
    end

    %% ====== EDGES ======
    CRON -->|scales 0→1| INGRESS
    HTTPC --> INGRESS
    INGRESS -->|FastAPI| SERVE

    N1 -->|HTTPS| M1
    N1 -->|HTTPS| S1
    N1 -->|HTTPS| S3
    N3 -->|HTTPS + JSON-schema| OAI

    W1 & W2 & WN -->|HTTPS| S2
    DELIVER -->|HTTPS| M2
    W1 & W2 & WN -->|HTTPS| M3
    DELIVER -->|SQL API| COSMOS
    GATE -->|checkpoint| PG

    KV -.->|managed-identity| SERVE
    CR -.->|AcrPull| T2A
    T2A -->|stdout| LA

    N1 & N2 & N3 & W1 & W2 & WN & N4 -.->|traces| LS
    INT -.->|pauses| GATE
```

---

## Data flow — single SEC filing, end to end

```mermaid
sequenceDiagram
    autonumber
    participant K as KEDA cron
    participant A as ACA · calorch
    participant G as Graph API
    participant S as SEC EDGAR
    participant O as Azure OpenAI
    participant L as OneDrive
    participant M as Graph Mail
    participant C as Cosmos DB
    participant P as PostgreSQL checkpoint
    participant H as Human reviewer
    participant T as LangSmith

    K->>A: scale 0→1 (Monday 06:00 UTC)
    A->>S: GET /company_tickers.json
    S-->>A: ticker↔CIK map
    loop for each ticker in watchlist
        A->>S: GET /submissions/CIK{cik}.json
        S-->>A: filings[] (form, items, date, accession)
    end
    A->>S: GET /xbrl/companyfacts/CIK{cik}.json (per ticker)
    S-->>A: revenue, net_income, eps
    A->>A: Pass 1: classify_form(form, items)
    Note over A: 8-K items=2.02 → earnings_call<br/>8-K items=5.07 → conference
    alt confidence < 0.95
        A->>O: structured classify
        O-->>A: ClassificationResult
    end
    A->>T: log trace
    par prepare per filing (parallel Send)
        A->>S: GET XBRL (latest)
        S-->>A: snapshot
        A->>A: build_analysis(type, ev, ed)
        A->>A: render DOCX (python-docx)
        A->>L: PUT /drive/items/{id}:/calorch/{ev_id}.docx
        L-->>A: webUrl
        A->>A: render HTML email preview
    end
    alt send_emails = true and require_approval = true
        A->>P: checkpoint prepared previews
        A-->>H: pending_approval
        H->>A: POST /runs/{id}/approval
    end
    par deliver per filing (parallel Send)
        A->>M: POST /me/messages (record stable draft id)
        alt send_emails = true
            A->>C: UPSERT status=prepared, draft id
            A->>M: POST /messages/{id}/send
        end
        A->>C: UPSERT final delivery status
    end
    A->>A: aggregate_briefing
    A-->>K: exit (scale 1→0)
```

---

## Deployment topology

```mermaid
flowchart LR
    classDef rg fill:#EEF2FF,stroke:#4338CA,color:#312E81
    classDef role fill:#FEF3C7,stroke:#D97706,color:#78350F

    subgraph RG["Resource Group: rg-calorch-prod"]
        direction TB
        ID["Managed Identity<br/>(system-assigned)"]:::role
        subgraph VNET["Container Apps Environment"]
            ACA["calorch<br/>(revision: latest)"]
            ACAENV["env, ingress,<br/>Log Analytics link"]
        end
        subgraph SIDECAR["Side services"]
            ACR["ACR Basic<br/>calorch.azurecr.io"]
            COSMOS["Cosmos DB<br/>serverless"]
            PG["PostgreSQL<br/>LangGraph checkpoints"]
            KV["Key Vault"]
            OAI["Azure OpenAI<br/>(separate RG)"]
        end
    end

    subgraph M365["M365 tenant"]
        GRAPH["Graph API<br/>(Entra app reg)"]
        OD["OneDrive"]
    end

    subgraph SEC["SEC EDGAR"]
        EDGAR["data.sec.gov"]
    end

    ID -.->|AcrPull| ACR
    ID -.->|Cosmos DB Data Contributor| COSMOS
    ID -.->|Cognitive Services User| OAI
    ID -.->|Secret User| KV
    ACA -->|HTTPS| GRAPH
    ACA -->|HTTPS| OD
    ACA -->|HTTPS| EDGAR
    ACA -->|HTTPS| OAI
    ACA -->|SQL API| COSMOS
    ACA -->|checkpoint| PG
    KV -.->|secrets| ACA
```

---

## Request lifecycle (human review gate)

```mermaid
stateDiagram-v2
    [*] --> Idle: scale = 0
    Idle --> Booting: cron OR /run
    Booting --> PullingImage: AcrPull (managed identity)
    PullingImage --> Starting: cold-start ~5-10s
    Starting --> Scanning: scan_calendar
    Scanning --> Classifying: Pass 1 (keywords/SEC)
    Classifying --> Classifying: Pass 2 (GPT-4o SO)
    Classifying --> FanOut: Send × N prepare branches

    state FanOut <<choice>>
    FanOut --> Enriching: prepare_event
    Enriching --> RenderingDOCX: build_analysis
    RenderingDOCX --> UploadingOneDrive
    UploadingOneDrive --> PreviewingEmail

    state PreviewingEmail <<choice>>
    PreviewingEmail --> DraftingEmail: draft mode (default)
    PreviewingEmail --> HumanReview: send + require_approval
    HumanReview --> Checkpointed: interrupt()<br/>(pause)
    Checkpointed --> Resumed: POST /runs/{id}/approval
    Resumed --> DraftingEmail
    DraftingEmail --> SendingEmail: send requested
    DraftingEmail --> PersistingRepo: draft mode
    SendingEmail --> PersistingRepo: Cosmos upsert
    PersistingRepo --> Aggregating
    Aggregating --> BriefingWritten
    BriefingWritten --> Idle: scale = 0
```

---

## Security & governance

| Concern | Control |
|---|---|
| **Credential storage** | ACA secrets today; use Key Vault references before production rollout |
| **Container pull** | Managed identity + AcrPull role, no admin password |
| **Cosmos access** | Cosmos account key in ACA secret today; managed identity is the target hardening step |
| **OpenAI access** | Azure OpenAI key in ACA secret today; managed identity is the target hardening step |
| **Graph access** | App registration (client credentials), secret in ACA secret store |
| **HTTP API access** | `X-Calorch-API-Key` required for `/run` and `/runs/*`; health probe remains public |
| **Email draft vs send** | `send_emails=False` default — human approves per event |
| **Interrupt gate** | `approval_gate` calls `interrupt()` after previews; `POST /runs/{id}/approval` resumes |
| **SEC fair-use** | `_RateLimiter` (9 req/sec, thread-safe) + `.cache/sec/` disk cache |
| **LLM tracing** | LangSmith environment configuration; add PII redaction before enabling on sensitive calendars |
| **Network egress** | ACA outbound to Graph/EDGAR/OpenAI over HTTPS only; no VNet required at this tier |
| **Image scanning** | Enable Microsoft Defender for container registries in the Azure subscription |

---

## Cost breakdown (monthly, typical weekly job)

| Resource | Quantity | Unit | Monthly |
|---|---|---|---|
| ACR (Basic) | 1 | $5.00 | $5.00 |
| Container Apps active time | ~20 min/wk | $0.000012/vCPU-sec | $0.30 |
| Cosmos DB Serverless | 100 RUs, 50 MB | $0.25/GB | $0.05 |
| Log Analytics | 1 GB ingested | $2.30/GB | $2.30 |
| Key Vault | 10 secrets, 10k ops | $0.03/10k | $0.01 |
| Azure OpenAI (Pass 2 only) | ~5k calls/wk | ~$0.01/call | $20.00 |
| LangSmith Developer | 1 seat | $39 | $39.00 |
| **Total** | | | **~$66.66/mo** |

For a 4-week run (1,000 filings, all 8 types, 1 LLM call per filing):
- Cold-start amortised: ~10s × 4 = 40s
- Active time: ~5 min × 4 = 20 min
- LLM calls: SEC path skips Pass 2 (confidence 0.95), so only Outlook events
  invoke GPT-4o. With a 200-event Outlook calendar that's ~$8/mo.

---

## Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| EDGAR rate-limited | 429 response | `_RateLimiter` backs off automatically |
| EDGAR down | connect error | Filing skipped, error logged, rest of pipeline continues |
| OneDrive 401 | token expired | MSAL refresh-token flow, transparent to caller |
| LLM timeout | OpenAI 30s default | Pass 1 hint used (confidence 0.4), error recorded |
| Cosmos write conflict | 409 from server | SDK retries with new session token |
| ACA cold-start | first weekly run | 5-10s added; LangGraph checkpointer restores from last checkpoint |
| Graph send quota | 429 from Graph | Microsoft Graph throttling headers respected (Retry-After) |
| Image pull failure | 401/403 from ACR | AcrPull role re-checked; image tag rolled back via ACA revision |

---

## Comparison with original ADR (Option C, Functions)

| Aspect | ADR Option C (Functions) | This impl (ACA) | Azure Durable Functions |
|---|---|---|---|
| Runtime | Azure Functions (Consumption) | Container Apps (Consumption) | Azure Functions (Consumption) |
| Cold start | per-function | per-revision (1x/week) | per-function |
| Per-node timeout | 5 min default, 10 min max | unbounded | 5 min default, 10 min max |
| Checkpointer | Durable Functions orchestration state | LangGraph `PostgresSaver` (`MemorySaver` only for local dev) | Built-in task hub (Azure Storage) |
| State sharing | orchestrator output binding | `configurable["context"]` injection | Orchestrator local variables (deterministic only) |
| Parallel fan-out | `task.all([...])` | `Send` API (one line) | `task_all([call_sub_orchestrator(...)])` |
| Scale to zero | yes | yes | yes |
| Cost (weekly 1k events) | Functions: ~$5; Premium EP1: ~$160 | ~$7 | ~$5–10 |
| Code complexity | N×function.json + bindings | 1 Dockerfile, 1 ACA YAML | N×activity + 1 orchestrator + bindings |
| Multi-day pauses | native (durable timers) | supported with `CHECKPOINT_POSTGRES_URI` | native (`WaitForExternalEvent` + durable timers) |
| Best for calorch? | acceptable | **yes** | poor (determinism + 10-min cap) |

The ADR's Functions architecture is preserved in spirit (per-node
boundary, durable orchestration, scale-to-zero) but realised on a
runtime that doesn't impose a 10-minute execution limit on the
per-event pipeline — important when a single event triggers
Graph + EDGAR + OpenAI + OneDrive + Outlook + Cosmos round trips.

For the full evaluation of why we chose LangGraph on ACA over Durable
Functions, see `docs/evaluations/azure-durable-functions.md`.

For the enterprise-grade data-source strategy (Refinitiv / FactSet / Tiingo
/ FRED / SEC), see `docs/evaluations/enterprise-data-sources.md`. For a
per-field gap analysis (what SEC has, what needs supplementing), see
`docs/evaluations/sec-edgar-coverage.md`.

## Data source layer (built today)

The orchestrator is wired against a `ProviderBundle` of free, official
sources plus stubs for fields that require a paid terminal:

| Provider | Real impl | Stub | Triggered by env var |
|---|---|---|---|
| Macro (VIX, 10Y, oil, …) | FRED + FOMC H.15 | `StubFredClient` + `StubFedH15Client` | `FRED_API_KEY`, `USE_FRED`, `USE_FED_H15` |
| Segments (product) | `SecIxbrlClient` (real parser) | `StubIxbrlClient` (curated AAPL/MSFT/…) | `USE_IXBRL_SEGMENTS` |
| Segments (geographic) | `SecIxbrlClient` (real parser) | `StubIxbrlClient` (curated AAPL) | `USE_IXBRL_SEGMENTS` |
| Narrative (guidance) | `SecEftsClient` (real search) | `StubEftsClient` (curated AAPL/MSFT/NVDA) | `USE_SEC_EFTS` |
| Price (52w, market cap) | (none — no free source) | `StubPriceProvider` | `TIINGO_API_KEY` (when issued) |
| Consensus (EPS est, target) | (none — no free source) | `StubConsensusProvider` | `REFINITIV_CLIENT_ID` (when issued) |

The dispatcher in `src/calorch/providers.py:build_providers()` reads
`Settings` and returns the right implementation. The renderer never knows
which one is wired.

---

**See also:** `deploy/README.md` · `IMPLEMENTATION_REVIEW.md` · `langgraph.json`
