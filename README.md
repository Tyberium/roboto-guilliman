# roboto-guilliman

AI-powered Warhammer **11th edition** rules arbiter for [battleplan.uk](https://battleplan.uk).

roboto-guilliman answers rules questions using retrieval-augmented generation (RAG) over
ingested rules PDFs. It cites page/section context, refuses to hallucinate when the
index does not cover an interaction, and caches repeat questions in Firestore.

**Live API (EU):** `https://roboto-guilliman-wifsng2koa-ew.a.run.app`

**Deploys only via GitHub Actions.** Infrastructure is Pulumi. Tuned for GCP free tier.

## Stack

| Layer | Tech |
|-------|------|
| Rules corpus | `download-rules` CLI → Warhammer Community downloads API |
| Ingestion | Python, PyMuPDF, `text-embedding-004` |
| Vector store | Firestore native vector search (768-dim, COSINE) |
| LLM | Gemini 2.5 Flash-Lite via Vertex AI |
| API | FastAPI on Cloud Run (`min-instances=0`, 256Mi, `europe-west1`) |
| Infra | Pulumi (Python) in `infra/pulumi/` |
| CI/CD | GitHub Actions (test, build, `pulumi up`, smoke test) |
| Auth (planned) | Firebase ID tokens from battleplan.uk (no paid LB/IAP) |
| Cache | Firestore `chat_history` collection |

See [docs/free_tier_and_security.md](docs/free_tier_and_security.md) for cost and security notes.
See [docs/roadmap.md](docs/roadmap.md) for phases and gaps.

## Project layout

```
roboto-guilliman/
  roboto_guilliman/
    api/              # FastAPI Cloud Run service
    ingestion/
      download_rules.py   # sync PDFs from GW downloads API
      ingest_rules.py     # chunk + embed + Firestore write
  data/rules_pdfs/  # local PDF cache (gitignored)
  infra/pulumi/     # Pulumi stack (Cloud Run, IAM, vector index, Artifact Registry)
  .github/workflows/ci.yml
  tests/
```

## Quick start (local)

```bash
cd github/repositories/roboto-guilliman
cp .env.example .env
poetry install
poetry run pytest
```

Authenticate to GCP (Application Default Credentials):

```bash
gcloud auth application-default login
gcloud config set project roboto-guilliman
```

### Download rules PDFs

Warhammer Community exposes a **public downloads search API** (no HTML scraping). The CLI
fetches all English Warhammer 40,000 PDFs listed on their downloads page - core rules,
faction packs, event companions, and miscellaneous - and saves them locally with a
SHA256 manifest for skip-on-unchanged refreshes.

```bash
poetry run download-rules              # ~72 PDFs, 5s delay between each (~7 min)
poetry run download-rules --dry-run    # list what would be downloaded
poetry run download-rules --force      # re-download even if manifest matches
```

Output: `data/rules_pdfs/` + `data/rules_pdfs/manifest.json` (never committed).

Politeness defaults: sequential requests, 5s delay, identifying User-Agent, backoff on
429/503. See `.cursor/rules/gw_rules_downloads.mdc` for conventions when extending this.

### Ingest a rules PDF

The Firestore vector index is created by Pulumi on first deploy. For local ingest only,
ensure the index exists (run `pulumi up` once, or deploy via CI).

```bash
poetry run ingest-rules data/rules_pdfs/core_rules__*.pdf --source-name core_rules_11th
```

Dry-run parsing only:

```bash
poetry run ingest-rules path/to/rules.pdf --dry-run
```

Batch ingest of the full downloaded corpus is not automated yet (see roadmap Phase 5).

### Run the API locally

```bash
poetry run serve
```

```bash
curl -s -X POST http://localhost:8080/v1/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "What happens when a unit fails a Battle-shock test?"}' | jq
```

Smoke test against production:

```bash
curl -s https://roboto-guilliman-wifsng2koa-ew.a.run.app/health
```

## Deployment (GitHub Actions only)

Push to `main` runs:

1. **Test** - ruff + pytest on every PR and push
2. **Build and Deploy** (main only) - Pulumi bootstrap, push image to Artifact Registry
   (`europe-west1`), `pulumi up`, smoke test `/health`

CI status: [GitHub Actions](https://github.com/Tyberium/roboto-guilliman/actions)

### Required GitHub secrets

| Secret | Purpose |
|--------|---------|
| `PULUMI_ACCESS_TOKEN` | [Pulumi Cloud](https://app.pulumi.com/) state (free tier) |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Keyless GCP auth from Actions |
| `GCP_SERVICE_ACCOUNT` | Deployer service account email |

Alternative: use `GCP_SA_KEY` (JSON) with `credentials_json` in the auth step if WIF is not set up yet.

### Manual Pulumi (local preview only)

```bash
cd infra/pulumi
poetry install
pulumi stack select main
pulumi preview
```

Production deploys should go through CI, not local `pulumi up`.

## Design notes

- **Single environment** - one GCP project and one Pulumi `main` stack; no dev/stage split (see [docs/free_tier_and_security.md](docs/free_tier_and_security.md)).
- **EU region** - Cloud Run, Firestore, and Artifact Registry in `europe-west1`.
- **All Python** for app code; **Pulumi Python** for infra (matches ingestion/API stack).
- **Free tier first** - no global HTTPS LB or IAP; Firebase token auth at the app layer instead.
- **Embeddings stored as `Vector(...)`** - required for Firestore vector indexes.
- **Query cache** in `chat_history` avoids repeat LLM calls.
- **Rules corpus** - downloaded locally via GW's public API; ingested to private Firestore only.

## Next steps

- [ ] Batch ingest downloaded PDFs into `warhammer_rules_11th`
- [ ] Firebase ID token middleware on `/v1/ask`
- [ ] Tune chunking against real 11th ed PDF structure (tables, datasheets)
- [ ] Scheduled `download-rules` refresh when GW publishes errata
- [ ] Embed chat UI in battleplan.uk

## License

Rules text is Games Workshop IP - download and ingest only for private use; never commit
PDFs or expose rules text publicly. Application code: see repository license.
