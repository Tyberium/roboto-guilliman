# Ro-boto-guilliman

AI-powered Warhammer **11th edition** rules arbiter for [battleplan.uk](https://battleplan.uk).

Ro-boto-guilliman answers rules questions using retrieval-augmented generation (RAG) over
ingested core rules PDFs. It cites page/section context, refuses to hallucinate when the
index does not cover a interaction and caches repeat questions in Firestore.

**Deploys only via GitHub Actions.** Infrastructure is Pulumi. Tuned for GCP free tier.

## Stack

| Layer | Tech |
|-------|------|
| Ingestion | Python, PyMuPDF, `text-embedding-004` |
| Vector store | Firestore native vector search (768-dim, COSINE) |
| LLM | Gemini 2.5 Flash-Lite via Vertex AI |
| API | FastAPI on Cloud Run (`min-instances=0`, 256Mi) |
| Infra | Pulumi (Python) in `infra/pulumi/` |
| CI/CD | GitHub Actions (test, build, `pulumi up`) |
| Auth (planned) | Firebase ID tokens from battleplan.uk (no paid LB/IAP) |
| Cache | Firestore `chat_history` collection |

See [docs/free_tier_and_security.md](docs/free_tier_and_security.md) for cost and security notes.

## Project layout

```
ro-boto-guilliman/
  ro_boto_guilliman/
    api/           # FastAPI Cloud Run service
    ingestion/     # PDF parse + Firestore ingest
  infra/pulumi/    # Pulumi stack (Cloud Run, IAM, vector index, Artifact Registry)
  .github/workflows/ci.yml
  tests/
```

## Quick start (local)

```bash
cd github/repositories/ro-boto-guilliman
cp .env.example .env
poetry install
poetry run pytest
```

Authenticate to GCP (Application Default Credentials):

```bash
gcloud auth application-default login
gcloud config set project battleplan-dev-2024
```

### Ingest a rules PDF

The Firestore vector index is created by Pulumi on first deploy. For local ingest only,
ensure the index exists (run `pulumi up` once, or deploy via CI).

Place your licensed core rules PDF locally (never commit it):

```bash
poetry run ingest-rules path/to/core_rules.pdf --source-name core_rules_11th
```

Dry-run parsing only:

```bash
poetry run ingest-rules path/to/core_rules.pdf --dry-run
```

### Run the API locally

```bash
poetry run serve
```

```bash
curl -s -X POST http://localhost:8080/v1/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "What happens when a unit fails a Battle-shock test?"}' | jq
```

## Deployment (GitHub Actions only)

Push to `main` runs:

1. **Test** - ruff + pytest on every PR and push
2. **Build and Deploy** (main only) - bootstrap Pulumi stack if needed, push image to Artifact Registry, `pulumi up`, smoke test `/health`

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
pulumi stack select dev
pulumi preview
```

Production deploys should go through CI, not local `pulumi up`.

## Design notes

- **All Python** for app code; **Pulumi Python** for infra (matches ingestion/API stack).
- **No Terraform, no shell deploy scripts** - CI owns the release path.
- **Free tier first** - no global HTTPS LB or IAP; Firebase token auth at the app layer instead.
- **Embeddings stored as `Vector(...)`** - required for Firestore vector indexes.
- **Query cache** in `chat_history` avoids repeat LLM calls.

## Next steps

- [ ] Firebase ID token middleware on `/v1/ask`
- [ ] Tune chunking against real 11th ed PDF structure
- [ ] Ingest workflow (manual or CI with rules PDF in GCS)
- [ ] Embed chat UI in battleplan.uk

## License

Rules text is Games Workshop IP - ingest only materials you are licensed to use.
Application code: see repository license.
