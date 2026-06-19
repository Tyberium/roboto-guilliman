# roboto-guilliman - Strategic Roadmap & Implementation Phases

> **Governing principles:** Everything ships through CI. Near-zero cost (GCP free tiers + Twilio at ~$1/mo). Every feature is a portfolio demonstration.

---

## 1. Current State Audit

### What exists and works

| Module | Status | Notes |
|--------|--------|-------|
| FastAPI on Cloud Run | ✅ Deployed | `europe-west1`, public `/health`, `/v1/ask` live |
| Firestore vector store | ✅ Live | 768-dim COSINE index; **156 core rule chunks** ingested |
| Chat history cache | ✅ Live | SHA-256 keyed, skips LLM on cache hit |
| Gemini 2.5 Flash-Lite | ✅ Wired | Vertex AI; richer prompts, `top_k=8`, figure captions in context |
| `text-embedding-004` | ✅ Wired | Separate task types for doc/query |
| Recursive chunker | ✅ Tested | Paragraph-aware fallback; `core_rules` uses rule-number parser (156 chunks; see `docs/core_rules_chunking.md`) |
| Preview chunks CLI | ✅ Working | `poetry run preview-chunks` for `#New40k` core rules |
| Figure captions | ✅ Done (core) | `caption-core-rules-pages` → `page_captions.json`; merged at ingest |
| Source registry | ✅ Working | Parser profiles, `excluded/` quarantine, ingest guards |
| Legacy edition guard | ✅ Working | `/v1/ask` refuses 9th/10th ed queries before retrieval |
| Download CLI | ✅ Working | `poetry run download-rules` via GW public downloads API (~72 PDFs) |
| Ingest CLI | ✅ Working | `poetry run ingest-rules`; core rules + `figure_description` wired |
| Pulumi IaC | ✅ CI-managed | Python, `main` stack, Artifact Registry + Cloud Run |
| GitHub Actions CI | ✅ Passing | ruff → pytest → Docker build → `pulumi up` → smoke test `/health` |
| Local rules corpus | ✅ Downloaded | `data/rules/{parser_profile}/` + manifest (~825 MB, gitignored) |

### Rules download pipeline (implemented)

Warhammer Community exposes a **public search API** - no HTML scraping required:

```
POST /api/search/downloads/
{ "index": "downloads_v2", "searchTerm": "", "gameSystem": "warhammer-40000", "language": "english" }
→ hits[].id.file → https://assets.warhammer-community.com/{file}
```

`download_rules.py` queries the API, then downloads PDFs **sequentially** with a 5s delay,
SHA256 manifest skip-on-unchanged, and exponential backoff on 429/503. Conventions are
documented in `.cursor/rules/gw_rules_downloads.mdc`.

Categories synced: core rules and key downloads, event companions, faction packs (#New40K
and legacy), miscellaneous.

### Gaps (the entire roadmap targets these)

| Gap | Impact | Phase |
|-----|--------|-------|
| No chunking strategy implemented | Core rules parser done; faction packs still flat text | 4a |
| No chunk preview CLI | Other profiles still lack preview parsers | 4a |
| Core rules only in Firestore | Faction packs / missions not ingested yet | 5 |
| No batch ingest | 72 PDFs local; `--only-changed` manifest ingest not built | 5 |
| No billing kill switch | Budget alert emails only; spend is not capped in software | 0 |
| No auth on `/v1/ask` | Anyone can drain your Vertex AI quota | 1 |
| No WhatsApp channel | Primary use-case undelivered | 2 |
| No Battleplan frontend widget | Secondary integration undelivered | 3 |
| Flat PDF extraction (PyMuPDF text) | Misses table structure, weapon profiles | 4 |
| Dense-only retrieval | Keyword-heavy queries (ability names, stratagems) under-perform | 4 |
| No errata/version metadata | Outdated rules could be retrieved (multiple pack versions downloaded) | 4 |
| No evaluation harness | No way to measure retrieval quality regression | 5 |
| No scheduled download refresh | GW errata requires manual `download-rules` re-run | 5 |
| No mypy in CI | Type errors slip through | 1 |
| No rate limiting | DoS / quota exhaustion risk | 2 |

---

## 2. Chunking and data model (locked)

Decisions agreed before bulk ingest. PDF layout mirrors parser profiles in
`roboto_guilliman/ingestion/source_registry.py`.

### Firestore: one collection, one vector index

Keep the existing `(default)` database and single collection `warhammer_rules_11th`
with one COSINE vector index on `embedding`. Do **not** split into per-faction or
per-PDF-type collections.

**Why:** Players ask cross-cutting questions ("Can Orks use this core rule stratagem?").
One index means one `find_nearest` call. Firestore supports metadata pre-filters on
`parser_profile`, `faction`, `status`, etc. Multiple collections would need multiple
indexes, multi-query fusion, and more free-tier overhead for no retrieval gain.

**Document schema (target):**

| Field | Purpose |
|-------|---------|
| `text` | Chunk body (includes rule number in text for embedding) |
| `embedding` | 768-dim `text-embedding-004` vector |
| `parser_profile` | Which chunker produced this row |
| `chunk_type` | `core_rule`, `stratagem`, `datasheet`, `mission`, `table`, `faq` |
| `rule_number` | e.g. `01.03` (core rules citation) |
| `parent_section` | Unit name, mission name, detachment |
| `faction` | When applicable |
| `source` | Stable slug, e.g. `core_rules_11th`, `orks` |
| `source_category` | Raw GW API category (audit) |
| `page` | PDF page number |
| `has_figure` | Page/chunk contains diagram-heavy content |
| `figure_description` | Vision caption stored at ingest (searchable text) |
| `page_image_uri` | Optional cached page PNG for query-time multimodal |
| `status` / `superseded_by` / `effective_date` | Errata override (Phase 4c) |

Retrieval: vector (+ later BM25) on text only. Images are a **generation-time**
enrichment when `has_figure` is set, not embedded.

### Local PDF folders (= parser profiles)

```
data/rules/
  manifest.json
  core_rules/           # #New40k numbered rules only (Firestore ingest)
  excluded/             # Sep 2024 layout core rules, quick start (local only)
  updates_and_faq/      # Q&A, rules commentary
  reference/            # Balance Dataslate, Munitorum tables
  faction_packs/        # stratagems, datasheets, abilities
  event_companions/     # missions, deployment, scoring
  miscellaneous/        # fallback recursive split
```

GW API categories (`new40k`, `faction-packs`, …) are messy; folder placement uses
title + category heuristics in `source_registry.py`.

### Chunking by parser profile

| Profile | Boundary | Overlap |
|---------|----------|---------|
| `core_rules` | One chunk per rule number | None across rules | `#New40k` PDF only (156 chunks). Sep 2024 + Quick Start → `excluded/` (never ingested). See `docs/core_rules_chunking.md`. |
| `updates_and_faq` | One Q+A or errata block | None |
| `reference` | One table row / detachment block | None |
| `faction_packs` | Stratagem, ability, or unit datasheet | None across units |
| `event_companions` | Mission / deployment section | None |
| `miscellaneous` | Recursive split (current chunker) | 200 chars |

### Figures and diagrams

1. **Ingest:** detect image-heavy pages via PyMuPDF block layout; run Gemini vision
   once per flagged page; store `figure_description` on linked chunks.
2. **Query:** text retrieval as today; if top hits have `has_figure` and caption is
   thin, attach cached page PNG to the LLM call (multimodal answer path).
3. **Do not** embed image pixels in Firestore vectors.

### Edition naming (#New40k = 11th edition)

GW's downloads API exposes three core-rules-adjacent PDFs:

| GW title | Category | Edition | roboto folder |
|----------|----------|---------|---------------|
| `#New40k - Core Rules` | `new40k` | **11th** (Jun 2026 launch) | `core_rules/` |
| `Core Rules` | `core-rules-and-key-downloads` | **10th** (Sep 2024 layout) | `excluded/` |
| `Core Rules Updates and Rules Commentary` | `core-rules-and-key-downloads` | Current errata | `updates_and_faq/` |

Community and press call the Jun 2026 launch **11th edition**; GW marketing uses **#New40k**.
Both refer to the same rules generation - not the Sep 2024 book.

**LLM risk:** Training data is dominated by 9th/10th mechanics. Until Firestore is populated,
`/v1/ask` may still feel "10th-ish" on non-legacy questions if the model free-wheels. Ingest
`#New40k` core rules first; keep `temperature` low; cite `[Rule NN.NN]` from retrieved chunks only.

### Legacy edition requests (API)

`/v1/ask` runs `is_legacy_edition_query()` before cache or retrieval. Prior-edition
questions (9th, 10th, "old rules", etc.) get an in-character refusal opening with
*"What sort of heresy is this?"* - no Firestore read, no Gemini call. Enforced in code
and documented in `.cursor/rules/eleventh_edition_only.mdc`.

### Pre-ingest workflow (next implementation steps)

1. `preview-chunks` CLI - print N sample chunks per PDF, no Firestore write. **Done for `core_rules`.**
2. Implement `core_rules` rule-number parser; validate on `data/rules/core_rules/`. **Done - see `docs/core_rules_chunking.md`.**
3. Add figure detection + vision caption pass. **Done for core rules** (`caption-core-rules-pages`, `page_captions.json`).
4. Ingest `#New40k` core rules with figure metadata. **Done** (156 docs in `warhammer_rules_11th`).
5. Batch ingest with `--only-changed` against manifest SHA256 (remaining PDF profiles).

### Billing guardrails

| Control | Status | Notes |
|---------|--------|-------|
| **Budget alert** | ✅ Live | Project-scoped **£5/mo** alert on `roboto-guilliman` (50/90/100%); email to owner |
| **Billing kill switch** | ❌ Planned | Alert notifies only; does not stop Vertex/Firestore spend |

**Kill switch (implement before public launch):** When monthly spend crosses a threshold
(e.g. 80-100% of budget), automatically degrade or halt paid inference so a bad deploy or
abuse cannot drain the card.

Recommended implementation (Phase 0 ops):

1. **Pub/Sub billing export** or **Monitoring alert policy** on billing metric → Cloud Function / Workflow.
2. Set **`ASK_DISABLED=true`** in Secret Manager (or Firestore `ops/config` flag).
3. **`/v1/ask`** checks flag first: return **503** with a short in-character message; skip
   embedding + Gemini calls. **`/health`** stays 200 so CI smoke tests still pass.
4. Manual re-enable via secret flip or console after investigating the spike.
5. Optional: remove `allUsers` Cloud Run invoker when disabled (harder stop, slower to restore).

Quality-over-availability: prefer a brief outage over serving hallucinated or quota-exhausted garbage answers.

---

## 3. Architecture Target State

```
                       ┌─────────────────────────────────────────┐
                       │           GitHub Actions CI              │
                       │  lint → type-check → test → eval-gate   │
                       │  → docker build → pulumi up → smoke test │
                       └───────────────┬─────────────────────────┘
                                       │ deploys
                       ┌───────────────▼─────────────────────────┐
                       │         Cloud Run (free tier)            │
                       │         roboto-guilliman API            │
                       │                                          │
                       │  POST /v1/ask          (Firebase auth)  │
                       │  POST /webhook/whatsapp (Twilio HMAC)   │
                       │  GET  /health                           │
                       │  GET  /docs            (OpenAPI, public)│
                       └──────┬──────────────┬────────────────────┘
                              │              │
              ┌───────────────▼──┐     ┌─────▼───────────────────┐
              │   Firestore       │     │  Vertex AI (free quota) │
              │  vector index     │     │  Gemini 2.5 Flash-Lite  │
              │  chat_history     │     │  text-embedding-004     │
              │  rate_limits      │     └─────────────────────────┘
              └───────────────────┘
                       ▲
        ┌──────────────┴──────────────────┐
        │  Ingest pipeline (manual today) │
        │  download-rules → local PDFs    │
        │  → ingest-rules → Firestore     │
        └─────────────────────────────────┘

  Clients
  ───────
  battleplan.uk  ──── Firebase ID token ──▶  POST /v1/ask
  WhatsApp group ──── Twilio Sandbox   ──▶  POST /webhook/whatsapp
```

---

## 4. Technology Decisions (Zero-Cost Stack)

| Concern | Chosen solution | Free tier / cost |
|---------|----------------|-----------------|
| Compute | Cloud Run v2, `min=0`, `max=2` | 2M req/mo free |
| Vector store | Firestore native vector | 50k reads/20k writes per day |
| LLM | Gemini 2.5 Flash-Lite (Vertex AI) | Pay-per-token, cheapest Gemini model; cache skips it entirely |
| Embeddings | `text-embedding-004` | Ingest is one-off; query embed is 1 small call per question |
| IaC | Pulumi Cloud (free tier) | Free for individual / open-source |
| CI/CD | GitHub Actions | 2,000 min/mo free (private) |
| Container registry | Artifact Registry | 0.5 GB free |
| Auth | Firebase Auth (ID tokens) | Free Spark plan |
| WhatsApp channel | Twilio WhatsApp Business | Dedicated Twilio number ~$1/mo. Supports group chat and DMs. HMAC webhook security. No sandbox join-code friction for members. |
| Keyword search (BM25) | `rank_bm25` pure Python library | No infra, runs in Cloud Run |
| Rate limiting | Firestore counters (no Redis needed) | Uses free Firestore quota |
| Evaluation | `ragas` (offline, runs in CI) | Open-source, no hosted service needed |
| Secrets | GitHub Actions secrets + Cloud Run env vars | Free |
| PDF storage (ingest source) | Local `data/rules/{parser_profile}/` (gitignored); GCS optional in Phase 5 | Free |

**Twilio note:** Using a dedicated Twilio number (~$1/mo) from day one. This avoids the sandbox join-code step entirely - members just message the bot number directly or add it to the group. It is the only deliberate spend in the stack.

---

## 5. CI Pipeline - Full Target Definition

Every merge to `main` runs this pipeline in order. Gates are enforced - a failed gate blocks deploy.

```
┌─────────────────────────────────────────────────────────────────┐
│ Job: quality                                                    │
│  ruff check + format check                                      │
│  mypy --strict roboto_guilliman                                │
│  pytest -q --cov=roboto_guilliman --cov-fail-under=80         │
└──────────────────────────┬──────────────────────────────────────┘
                           │ passes
┌──────────────────────────▼──────────────────────────────────────┐
│ Job: eval-gate  (Phase 5, golden dataset)                       │
│  poetry run eval --fail-below precision=0.75 faithfulness=0.80 │
└──────────────────────────┬──────────────────────────────────────┘
                           │ passes (main branch only)
┌──────────────────────────▼──────────────────────────────────────┐
│ Job: build-and-deploy                                           │
│  GCP Workload Identity Federation (keyless)                     │
│  docker build + push (sha tag + latest)                        │
│  pulumi up --yes                                                │
│  smoke test: /health + POST /v1/ask (unauthenticated probe)    │
└─────────────────────────────────────────────────────────────────┘
```

PRs run `quality` only (no deploy, no eval).

---

## 6. Implementation Phases

---

### Phase 1 - Security Hardening & CI Quality Gate

**Goal:** Lock down the API so only legitimate callers can consume Vertex AI quota. Raise CI quality bar to portfolio standard.

**Acceptance criteria:**
- `POST /v1/ask` returns `401` without a valid Firebase ID token
- `GET /health` remains unauthenticated (Cloud Run health probe needs it)
- `mypy --strict` passes in CI with zero errors
- Pytest coverage gate at 80% enforced in CI
- All secrets managed via GitHub Actions secrets (no plain-text in code)

**Implementation tasks:**

0. **New GCP project setup** (one-time, outside CI - prerequisite for everything)
   - Create project `roboto-guilliman` in GCP console
   - Enable APIs: Cloud Run, Artifact Registry, Firestore, Vertex AI, Secret Manager
   - Create Firestore `(default)` database (Native mode, `europe-west1`) - API enable alone is not enough
   - Create Workload Identity Federation pool for GitHub Actions (keyless auth)
   - Create deployer service account with:
     - `roles/run.admin`, `roles/artifactregistry.admin`, `roles/datastore.user`, `roles/datastore.indexAdmin`, `roles/aiplatform.user`, `roles/secretmanager.admin`, `roles/iam.serviceAccountUser`, `roles/iam.serviceAccountAdmin`, `roles/resourcemanager.projectIamAdmin`, `roles/compute.viewer`
   - Update `.github/workflows/ci.yml` env vars: `GCP_PROJECT_ID: roboto-guilliman`
   - Update `Pulumi.main.yaml`: `gcp:project: roboto-guilliman`
   - Update `config.py` default: `gcp_project_id: str = "roboto-guilliman"`
   - Update `.env.example`: `GCP_PROJECT_ID=roboto-guilliman`
   - Update GitHub Actions secrets: `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`
   - Set a GCP billing budget alert at $5/mo (Twilio ~$1 + buffer for Vertex AI). **Done (£5/mo project alert).**
   - Wire **billing kill switch** - see §2 Billing guardrails (503 on `/v1/ask` when spend threshold hit).

1. **Firebase auth middleware** (`roboto_guilliman/api/auth.py`)
   - FastAPI dependency that extracts `Authorization: Bearer <id_token>`
   - Verifies with `firebase-admin` SDK (`verify_id_token`)
   - Returns decoded `uid` and `email` for downstream logging
   - Returns `401` on missing/invalid/expired token
   - Add `firebase-admin` to `pyproject.toml`

2. **Wire auth dependency** onto `/v1/ask` only
   - `AskRequest` gains optional `uid: str | None` from token (for future per-user history)
   - `/health` and `/docs` stay open

3. **mypy configuration** (`pyproject.toml` `[tool.mypy]`)
   - `strict = true`, `python_version = "3.13"`
   - Add `mypy` and `types-*` stubs to dev dependencies

4. **CI quality job update** (`.github/workflows/ci.yml`)
   - Split `test` job into `quality` (lint + mypy + pytest + coverage)
   - Coverage: `pytest --cov=roboto_guilliman --cov-fail-under=80`

5. **CORS configuration**
   - Allow `https://battleplan.uk` and `http://localhost:*` only
   - Use `fastapi.middleware.cors.CORSMiddleware`

**New files:**
- `roboto_guilliman/api/auth.py`
- `tests/test_auth.py` (mock Firebase token verification)

---

### Phase 2 - WhatsApp Channel (Twilio)

**Goal:** The bot answers rules questions sent to a WhatsApp group or DM via Twilio. This is the highest-value integration.

**Acceptance criteria:**
- Twilio can deliver a WhatsApp message to the Cloud Run webhook
- The bot responds within 10 seconds (Twilio timeout) with the rules answer
- Twilio webhook HMAC signature is validated - spoofed requests are rejected
- Rate limiting: max 10 requests per phone number per minute (Firestore counter)
- Works from a WhatsApp group (bot mentioned or message sent to bot number)
- Deployed entirely via CI (Twilio config stored in GitHub secrets / Cloud Run env vars)

**Architecture:**

```
WhatsApp message
      │
Twilio webhook
      │ POST /webhook/whatsapp
      │ X-Twilio-Signature header
      ▼
FastAPI validate_twilio_signature()
      │
parse_incoming_message()  ← if group: require @roboto mention, else silently drop
      │
rate_limit_check()  ← Firestore counter, per phone number
      │
RulesRetriever.retrieve()  ← existing pipeline
      │
GeminiArbiter.answer()     ← existing pipeline
      │
format_for_whatsapp()  ← strip Markdown, add emoji sparingly
      │
Twilio MessagingResponse  ← TwiML or REST API reply
      │
      ▼
WhatsApp reply to sender / group
```

**Implementation tasks:**

1. **WhatsApp router** (`roboto_guilliman/api/whatsapp.py`)
   - `POST /webhook/whatsapp` - accepts Twilio form-encoded body
   - Twilio signature validation using `twilio.request_validator.RequestValidator`
   - Parse `Body`, `From`, `To`, `NumMedia` fields
   - **Group mention filter:** if `To` is a group JID, message `Body` must contain `@roboto` (case-insensitive); if not, return HTTP 204 (no reply - Twilio receives an empty acknowledgement)
   - Strip the `@roboto` mention from the query before passing to the retriever
   - DMs (`To` is the bot number directly) are always processed without a mention check
   - Reject non-text messages gracefully ("I can only answer text questions, try: @roboto what happens when a unit fails a Battle-shock test?")
   - Add `twilio` to `pyproject.toml`

2. **Rate limiter** (`roboto_guilliman/rate_limiter.py`)
   - Firestore document per `From` number, TTL-style counter with `window_start`
   - `RateLimiter.check(phone: str) -> bool`
   - 10 requests per 60-second window (configurable via settings)

3. **WhatsApp formatter** (`roboto_guilliman/api/whatsapp_formatter.py`)
   - Convert Markdown bold (`**term**`) → `*term*` (WhatsApp bold)
   - Convert headers to uppercase plain text
   - Truncate to 1600 chars with "..." if answer is very long
   - Add citation footer: `📖 Source: {source} p.{page}`

4. **Settings additions** (`roboto_guilliman/config.py`)
   - `twilio_account_sid: str`
   - `twilio_auth_token: str`
   - `rate_limit_requests: int = 10`
   - `rate_limit_window_seconds: int = 60`

5. **Pulumi updates** (`infra/pulumi/__main__.py`)
   - Add `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` as Cloud Run secret env vars
   - Bind to GCP Secret Manager secrets (free tier: 6 active secret versions free)

6. **CI secrets** (`.github/workflows/ci.yml`)
   - Add `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` to `build-and-deploy` env
   - `pulumi config set --secret` for Twilio credentials

7. **Tests** (`tests/test_whatsapp.py`)
   - Mock Twilio signature validation
   - Test message parsing, rate limit enforcement, formatter output

**New files:**
- `roboto_guilliman/api/whatsapp.py`
- `roboto_guilliman/rate_limiter.py`
- `roboto_guilliman/api/whatsapp_formatter.py`
- `tests/test_whatsapp.py`
- `tests/test_rate_limiter.py`

**Twilio setup (one-time, outside CI):**
1. Sign up for Twilio account, purchase a number (~$1/mo)
2. Enable WhatsApp on the number via Twilio Console → Messaging → Senders → WhatsApp
3. Set webhook URL: `https://<cloud-run-uri>/webhook/whatsapp`
4. Add the bot number to the Warhammer WhatsApp group - no join code, no opt-in friction
5. Store `ACCOUNT_SID` and `AUTH_TOKEN` in GitHub secrets

---

### Phase 3 - Battleplan.uk Integration

**Goal:** The Battleplan.uk React app can surface the rules arbiter inline for signed-in members.

**Acceptance criteria:**
- Battleplan Cloud Function acts as BFF (Backend for Frontend) proxy to the rules API
- Firebase ID token forwarded transparently - no Battleplan user needs to re-authenticate
- Rules query widget embeddable in Battleplan admin/game screens
- Response includes citations displayed as collapsible cards

**Architecture:**

```
battleplan.uk (React)
      │
      │  Firebase ID token - proves the user is a signed-in battleplan member
      ▼
Firebase Cloud Function: callRulesArbiter (HTTPS callable)
      │  1. Verifies user's Firebase ID token (battleplan project - Cloud Function does this natively)
      │  2. Fetches a short-lived OIDC token for its own service account
      │  3. Calls ro-boto with: Authorization: Bearer <OIDC token>
      ▼
Cloud Run: POST /v1/ask  (roboto-guilliman project)
      │  Verifies Google-signed OIDC token against ro-boto's Cloud Run invoker IAM
      │  (GCP validates this natively - no Firebase SDK needed on ro-boto side)
      ▼
Firestore + Vertex AI  (roboto-guilliman project, fully isolated)
```

**Why service-to-service OIDC instead of forwarding the Firebase token:**
- Projects are now separate - ro-boto has no Firebase project of its own to verify battleplan tokens against
- Cloud Run's built-in IAM invoker check handles OIDC validation with zero extra code
- ro-boto stays completely unaware of battleplan's user model - it just trusts calls from the battleplan service account
- This is the correct pattern for GCP service-to-service auth at any scale

**Implementation tasks:**

1. **Battleplan Cloud Function** (in `battleplan` repo, not this repo)
   - HTTPS callable function `callRulesArbiter(query: string)`
   - Verifies caller's Firebase ID token from `context.auth` (already automatic in callable functions)
   - Fetches OIDC token for its own service account targeting the ro-boto Cloud Run URL
   - Calls `RO_BOTO_URL/v1/ask` with `Authorization: Bearer <oidc_token>`
   - Returns `{ answer, context_chunks, cached }`
   - `RO_BOTO_URL` stored as a Firebase function config / environment variable

2. **IAM grant in ro-boto Pulumi** (`infra/pulumi/__main__.py`)
   - Grant `roles/run.invoker` to the battleplan Cloud Function's service account on the ro-boto Cloud Run service
   - This is the only change needed on the ro-boto side for Battleplan auth
   - Store battleplan's service account email in Pulumi config

3. **Remove Firebase auth from `/v1/ask`** (or make it dual-mode)
   - Phase 1 adds Firebase ID token auth for direct API calls
   - Phase 3 relaxes this: Cloud Run's IAM layer handles OIDC from battleplan; Firebase ID tokens still accepted for any future direct callers
   - In practice: Cloud Run `--no-allow-unauthenticated` + IAM invoker grants replaces the app-layer Firebase check for server-to-server calls

4. **OpenAPI documentation polish** (`roboto_guilliman/api/main.py`)
   - Add `tags`, `summary`, `description`, `response_description` to all endpoints
   - `/docs` is already public - this becomes the integration reference for Battleplan developers
   - Add example request/response bodies using Pydantic `model_config`

5. **CORS update** - already done in Phase 1, ensure `battleplan.uk` is in allowed origins

**New files in this repo:** none. IAM change is in Pulumi. Cloud Function lives in the Battleplan repo.

---

### Phase 4 - Advanced RAG (Retrieval Quality Uplift)

**Goal:** Upgrade retrieval from "functional" to "portfolio-grade". Demonstrate senior engineering: hierarchical parsing, hybrid search, data versioning.

**Acceptance criteria:**
- Weapon profile tables are parsed into structured Markdown (not garbled flat text)
- Hybrid BM25 + vector search returns better results for stratagem/keyword queries
- Errata and FAQs can override base rules without re-ingesting the whole corpus
- Metadata pre-filtering by `faction` reduces cross-contamination
- All existing tests still pass; new tests cover each new component

#### 4a - Hierarchical PDF Parsing

**Current problem:** `fitz.page.get_text("text")` flattens tables into unusable strings. Weapon datasheets lose column alignment entirely.

**Solution:** Two-pass extraction per page using PyMuPDF's structured block output.

**Implementation tasks:**

1. **Structured page extractor** (`roboto_guilliman/ingestion/page_parser.py`)
   - Use `page.get_text("dict")` to get blocks with `type` (`0`=text, `1`=image)
   - Detect table-like blocks: lines with consistent x-coordinates
   - Convert table blocks to GitHub-flavoured Markdown tables
   - Preserve parent-child structure: weapon profile rows linked to unit name header
   - Output: `ParsedPage(text_blocks: list[Block], tables: list[MarkdownTable], section: str | None)`

2. **Enhanced TextChunk** (`roboto_guilliman/chunking.py`)
   - Add `chunk_type: Literal["text", "table", "datasheet"]` field
   - Add `parent_section: str | None` (e.g. unit name for weapon profile rows)
   - Add `faction: str | None` (extracted from section context)

3. **Update ingest pipeline** (`roboto_guilliman/ingestion/ingest_rules.py`)
   - Replace `page.get_text("text")` with `page_parser.extract_page`
   - Store `chunk_type`, `parent_section`, `faction` in Firestore documents
   - Dry-run now logs structured output for inspection

#### 4b - Hybrid Search (BM25 + Vector)

**Current problem:** Dense-only vector search performs poorly on exact name queries (e.g. "Rapid Ingress stratagem" - the word "Rapid" may not be semantically close to any chunk's vector).

**Solution:** Reciprocal Rank Fusion (RRF) of BM25 and vector results. Both run in the existing Cloud Run instance - no new infra.

**Implementation tasks:**

1. **BM25 index** (`roboto_guilliman/bm25_index.py`)
   - `BM25Index`: loads all `text` values from Firestore at startup, builds `rank_bm25.BM25Okapi`
   - Lazy-loads on first query, cached in `AppState`
   - `search(query: str, top_k: int) -> list[str]` returns doc IDs
   - Refresh on startup only (acceptable for a rules corpus that changes rarely)

2. **Hybrid retriever** (`roboto_guilliman/retriever.py`)
   - `HybridRetriever` replaces `RulesRetriever`
   - Runs vector search and BM25 search in parallel (`asyncio.gather`)
   - Merges via RRF: `score = 1/(k + rank_vector) + 1/(k + rank_bm25)`, `k=60`
   - Returns top-k after fusion
   - Falls back gracefully if BM25 index is empty (first cold start)

3. **Settings** - add `bm25_weight: float = 0.4`, `vector_weight: float = 0.6` to config

4. **Tests** (`tests/test_hybrid_retriever.py`)
   - Mock Firestore + embedding service
   - Assert RRF produces correct ranking for known fixtures

#### 4c - Errata Override Engine

**Goal:** When GW publishes a FAQ or Balance Dataslate, the new rule supersedes the old one. The old chunk must never be retrieved.

**Implementation tasks:**

1. **Metadata schema update** - Add to every Firestore document:
   - `status: Literal["active", "inactive"]` (default `"active"`)
   - `superseded_by: str | None` (doc ID of the replacement chunk)
   - `source_version: str` (e.g. `"core_rules_11th"`, `"faq_2025_q2"`)
   - `effective_date: datetime`

2. **Retriever pre-filter** - Add `status == "active"` filter to all Firestore queries

3. **Ingest CLI update** - New `--supersedes` flag:
   ```
   poetry run ingest-rules faq.pdf --source-name faq_2025_q2 \
     --supersedes core_rules_11th:page:42
   ```
   Marks the old document as `inactive` and sets `superseded_by`

4. **Ingest with version** (`ingest_rules.py`) - Store all new metadata fields on write

---

### Phase 5 - Evaluation Harness & CI-Driven Ingest

**Goal:** Measure and gate on retrieval quality. Automate rules ingestion via CI so errata can be published by committing a PDF to GCS.

#### 5a - Evaluation Harness

**Acceptance criteria:**
- Golden dataset of 20+ expert-curated query/answer pairs stored in `tests/golden_dataset.json`
- CI runs evaluation on every push to `main`
- Deploy is blocked if `context_precision < 0.75` or `faithfulness < 0.80`

**Implementation tasks:**

1. **Golden dataset** (`tests/golden_dataset.json`)
   - 20 queries covering: basic rules, stratagems, faction interactions, edge cases
   - Each entry: `{ "query": "...", "expected_answer_keywords": [...], "expected_source": "..." }`

2. **Evaluation runner** (`tests/eval_runner.py`)
   - Uses `ragas` library: `context_precision`, `faithfulness`, `answer_relevance` metrics
   - Calls the live API (or mocked retriever) for each query
   - Outputs JSON report to `eval_report.json`
   - `--fail-below precision=X faithfulness=Y` flag for CI gate

3. **CI eval gate** (`.github/workflows/ci.yml`)
   - New `eval-gate` job, runs after `quality`, before `build-and-deploy`
   - Only on `main` branch pushes (too slow for PRs)
   - Uploads `eval_report.json` as a workflow artifact

#### 5b - Batch Ingest & Refresh

**Goal:** Ingest the downloaded rules corpus into Firestore. Refresh when GW publishes
errata via `download-rules`, then re-ingest changed PDFs only.

**Prerequisites (done):**
- `poetry run download-rules` syncs ~72 English WH40K PDFs via GW public API
- Manifest at `data/rules/manifest.json` tracks SHA256 per file

**Implementation tasks:**

1. **Batch ingest CLI** (`ingest_rules.py` or new `ingest_all.py`)
   - Walk `data/rules/manifest.json`, run `ingest-rules` per PDF
   - `--source-name` derived from manifest title + category slug
   - `--only-changed` skips PDFs whose SHA256 matches last ingested hash in Firestore metadata
   - Rate-limit embedding batches to stay within Vertex free quota

2. **GCS bucket (optional)** (`infra/pulumi/__main__.py`)
   - Add `gcp.storage.Bucket("rules_pdfs", ...)` if CI-driven ingest is needed later
   - IAM: runtime SA gets `roles/storage.objectViewer`

3. **CI ingest job (optional)** (`.github/workflows/ingest.yml`)
   - Triggered by: `workflow_dispatch` only (not on every push)
   - Steps: `download-rules` (with delay) → batch ingest changed PDFs
   - Never commit PDFs; use GCS or ephemeral runner storage

4. **Scheduled refresh (optional)** - monthly `download-rules` + ingest changed files;
   document in README; do not hammer GW servers (keep 5s+ delay)

5. **Ingest status** - log last sync timestamp; optional README badge for ingest workflow

---

### Phase 6 - CV Portfolio Polish

**Goal:** Make the repository an obvious senior engineering showpiece. Every visitor (recruiter or engineer) should understand the depth within 2 minutes.

**Acceptance criteria:**
- README has architecture diagram, badges, and links to live demo
- OpenAPI docs (`/docs`) are fully documented with examples
- Repository demonstrates: IaC, CI/CD, RAG, free-tier cost engineering, security, testing, evaluation

**Implementation tasks:**

1. **README rewrite**
   - Add badges: CI status, coverage, Python version, mypy, Cloud Run region
   - Add architecture diagram (Mermaid or linked image)
   - "Why this is hard" section: explain hybrid search, errata engine, structured PDF parsing
   - "Cost breakdown" section: reference `docs/free_tier_and_security.md`
   - "Try it live" section with Battleplan.uk link and WhatsApp sandbox join code

2. **OpenAPI documentation** - Every endpoint has `summary`, `description`, examples
   - `/v1/ask` example request: Battle-shock test query
   - `/v1/ask` example response: answer with citations
   - `/webhook/whatsapp` documented as internal (Twilio-only)

3. **ARCHITECTURE.md** (`docs/architecture.md`)
   - Data flow from PDF to answer
   - Chunking strategy rationale
   - Hybrid search design decision log
   - Errata versioning design

4. **`docs/adr/`** - Architecture Decision Records
   - `adr_001_firestore_over_pinecone.md` - why Firestore vector (free) over Pinecone
   - `adr_002_hybrid_search_rrf.md` - why RRF over weighted sum
   - `adr_003_twilio_whatsapp_sandbox.md` - Twilio free tier decision

5. **CI badge in README** - Link to GitHub Actions run

---

## 7. Phase Delivery Order & Dependencies

```
Phase 1 (Security)
    │  blocks everything - auth must exist before public exposure
    ▼
Phase 2 (WhatsApp)  ←──── highest user value, deliver early
    │
    ├──▶ Phase 3 (Battleplan) ← depends on Phase 1 auth
    │
    ▼
Phase 4 (Advanced RAG)  ← improves answer quality for both channels
    │
    ▼
Phase 5 (Eval + CI Ingest)  ← quality gate + automation
    │
    ▼
Phase 6 (Portfolio Polish)  ← document everything done above
```

Phases 2 and 3 can be worked in parallel once Phase 1 is done.
Phases 4 and 5 are independent of each other within their phase.

---

## 8. Effort Estimates

| Phase | Complexity | Estimated Sessions |
|-------|-----------|-------------------|
| 1 - Security + CI | Low-Medium | 1 |
| 2 - WhatsApp | Medium | 2 |
| 3 - Battleplan | Low (mostly in battleplan repo) | 1 |
| 4a - Hierarchical parsing | High | 2 |
| 4b - Hybrid BM25 search | Medium | 1 |
| 4c - Errata engine | Medium | 1 |
| 5a - Evaluation harness | Medium | 1 |
| 5b - Batch ingest + refresh | Medium | 1-2 |
| 6 - Portfolio polish | Low | 1 |
| **Total** | | **~11 sessions** |

---

## 9. "Definition of Done" per Phase

A phase is complete when:
- [ ] All implementation tasks listed above are committed
- [ ] CI pipeline passes green end-to-end (quality → build → deploy)
- [ ] New code has 100% type-hint coverage (`mypy --strict` passes)
- [ ] New code has corresponding `pytest` tests (coverage gate still passes)
- [ ] `README.md` or `docs/` reflects the new capability
- [ ] No new paid services introduced

---

## 10. Open Questions / Decisions Needed

1. **WhatsApp group vs DM:** ✅ Decided. Bot is added to the group and responds **only when @mentioned** (`@roboto`). All other group traffic is silently ignored. DMs to the bot number also work (no mention needed in a 1:1 conversation). This is the correct UX - no noise, no accidental triggers.

2. **Battleplan project separation:** ✅ Decided. roboto-guilliman gets its own GCP project (`roboto-guilliman`) from day one. Battleplan billing spikes, IAM changes, and quota limits are fully isolated. Cross-project auth uses service-to-service OIDC (see Phase 3) rather than shared Firebase tokens.

3. **Twilio number:** Using a dedicated WhatsApp Business number via Twilio (~$1/mo) from day one. No sandbox, no join-code friction. The number is provisioned manually once; everything after that (webhook config, secret rotation) is CI-managed.

4. **Rules PDF distribution rights:** GW rules are copyright. PDFs stay in gitignored
   `data/rules/`; ingest chunks go to private Firestore only. Use GW's public downloads
   API politely (see `.cursor/rules/gw_rules_downloads.mdc`). The CV portfolio can reference
   the architecture without exposing rules text.

5. **11th edition / #New40k timing:** ✅ Mapped. `#New40k - Core Rules` (Jun 2026) = 11th ed canonical ingest; Sep 2024 `Core Rules` = 10th ed → `excluded/`. Faction duplicates: prefer `#New40k` / newest manifest date per faction at ingest (Phase 4 errata metadata).
