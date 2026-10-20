# LLM Evaluation Service

Runs a test dataset against versions of a prompt, stores per-case results, computes
metrics and reports regressions between runs. FastAPI + PostgreSQL. The job queue
lives in PostgreSQL (no Redis or Celery).

Everything works out of the box with a deterministic **fake provider**: no API keys and
no paid requests. The OpenAI provider is off unless explicitly enabled.

## Quick start

```bash
cp .env.example .env
docker compose up -d --build --wait api worker
./scripts/demo.sh                     # uploads data/demo.jsonl, runs 2 prompts, compares them
open http://localhost:8000/docs       # Swagger UI
```

The stack has four services: `db` (PostgreSQL 16), `migrate` (one-shot `alembic upgrade head`),
`api` (uvicorn) and `worker`. You can scale workers with `docker compose up -d --scale worker=3`.

## Concepts

| Entity | Notes |
|---|---|
| Dataset version | Uploaded as JSONL. Immutable: a database trigger rejects `UPDATE`/`DELETE`. Re-uploading identical content returns the existing version (`created: false`). |
| Prompt version | Template with a `{{question}}` placeholder. Immutable, same as datasets. |
| Run | One dataset version × one prompt version × provider + config. Gets one queue job per case. |
| Case result | One row per `(run_id, case_id)`, enforced by a unique constraint. |
| Pricing | Optional USD per 1M tokens for a `(provider, model)` with an `effective_from` date. |

### Dataset format (JSONL, one case per line)

```json
{"id": "facts-01", "question": "Describe the Zentor-9 battery.", "expected_answer": "The Zentor-9 battery stores 40 kWh ...", "required_facts": ["40 kWh", "25 minutes"]}
{"id": "json-01", "question": "Extract the person as JSON: ...", "expected_answer": "{\"name\": \"Mira Lenz\", \"age\": 34}", "json_schema": {"type": "object", "required": ["name", "age"]}}
```

`id` (`[A-Za-z0-9_.:-]`, max 128), `question`, `expected_answer` are required. `required_facts`
and `json_schema` are optional. Unknown fields, duplicate ids, invalid JSON Schemas and
blank facts are rejected with a 422 that lists line numbers. Size limits:
`MAX_UPLOAD_BYTES` (413) and `MAX_DATASET_CASES` (422).

`data/demo.jsonl` is a synthetic dataset of 40 cases (12 short answers, 14 fact-based,
14 JSON-schema) about fictional entities. Regenerate it with `python scripts/make_demo_dataset.py`.

## API examples

```bash
API=http://localhost:8000

curl -F file=@data/demo.jsonl $API/datasets/demo/versions
# {"dataset":"demo","dataset_version_id":1,"version":1,"case_count":40,...,"created":true}

curl -H 'content-type: application/json' $API/prompts/qa/versions \
  -d '{"template": "Answer the question.\n\nQuestion: {{question}}\nAnswer:"}'

curl -H 'content-type: application/json' $API/runs -d '{
  "dataset_version_id": 1,
  "prompt_version_id": 1,
  "provider": "fake",
  "config": {"model": "fake-model", "max_attempts": 3, "max_output_tokens": 256,
             "timeout_s": 30, "fake": {"accuracy": 0.8, "seed": 0}},
  "max_cases": 40
}'
# 202 {"id":1,"status":"queued","synthetic":true,"progress":{"queued":40,...},...}

curl $API/runs/1                                  # status + progress
curl "$API/runs/1/report?include_cases=false"     # metrics only
curl "$API/runs/compare?base=1&head=2"            # metric deltas + improved/regressed cases

curl -H 'content-type: application/json' $API/pricing -d '{
  "provider": "openai", "model": "gpt-4o-mini",
  "input_usd_per_1m": 0.15, "output_usd_per_1m": 0.6, "effective_from": "2026-01-01"}'
```

The pricing numbers above are placeholders for the example, not current prices.

## Run lifecycle

`queued` → `running` (first job claimed) → one of:

- `completed`: every case got a model answer (answers may still fail evaluation);
- `partial`: some cases ended with an error after retries;
- `failed`: every case ended with an error.

A case that fails evaluation is not an error. Errors are provider failures, timeouts,
lost leases or configuration problems. They are stored per case with `error_kind`
(`transient`, `permanent`, `unexpected`, `config`, `lease_expired`) and the attempt count.

## Queue, retries and leases

- A worker claims one job at a time with `SELECT ... FOR UPDATE SKIP LOCKED` and takes a
  lease (`lease_owner`, `lease_expires_at = now() + LEASE_SECONDS`). Only the database
  clock is used.
- **Transient errors** (timeouts, connection errors, HTTP 408/409/429/5xx) are retried up to
  `max_attempts` with exponential backoff (`RETRY_BACKOFF_BASE_S * 2^(n-1)`, capped by
  `RETRY_BACKOFF_MAX_S`). **Permanent errors** (other 4xx, malformed responses) and unexpected
  exceptions are not retried: blind retries could burn paid requests.
- **Worker crash**: when a lease expires, the job can be claimed again and the attempt count
  goes up. If the lease expires on the last allowed attempt, the job is failed with
  `lease_expired`.
- **Stale results**: a worker can finish a job only while it still owns a live lease for the
  same attempt. Otherwise its result is discarded.
- **No duplicates**: `UNIQUE(run_id, case_id)` on results, and inserts use `ON CONFLICT DO NOTHING`.
- `LEASE_SECONDS` must be greater than `MAX_TIMEOUT_S` (checked at startup), so a healthy but
  slow call never loses its lease.
- `SIGTERM` stops the worker after the current job. Compose gives it 90 s
  (`stop_grace_period`).

### Delivery semantics: at-least-once

A provider call and the database commit are not atomic. If a worker dies (or stalls past its
lease) **after the provider answered but before committing**, another worker will call the
provider again for that case. Duplicate requests, and with a real provider duplicate cost, are
possible. Duplicate *results* are not. The test
`test_lease_lost_mid_call_gives_at_least_once_but_single_result` shows exactly this case.

## Providers

| Provider | Enabled | Notes |
|---|---|---|
| `fake` | always | Deterministic: the output depends on `(seed, rendered prompt, case_id)`. Returns the reference answer with probability `accuracy`, otherwise a degraded one (truncated text, malformed JSON or schema-violating JSON). Can simulate latency, timeouts, transient and permanent failures. |
| `openai` | `OPENAI_ENABLED=true` and `OPENAI_API_KEY` set | Chat Completions over HTTP (`max_completion_tokens`, `temperature`). |

Safety limits: `MAX_CASES_PER_RUN`, `MAX_ATTEMPTS_CEILING`, `MAX_OUTPUT_TOKENS_CEILING` and
`MAX_TIMEOUT_S` cap every run config. Requests above them get a 422.

Secrets: the API key comes from the environment only and is held as a `SecretStr`. The run
config schema rejects unknown fields, so a key cannot be smuggled into the stored config.
Provider errors keep only the HTTP status and the error message, never request headers.
Reference answers are passed only to the fake provider and are never sent to OpenAI.

**Fake vs real results.** Every run and report states `provider`, `model`, the full `config`,
and `synthetic: true|false`. Fake reports carry a note that the numbers do not describe any
real model. Comparing a fake run with a real-provider run is rejected.

## Metrics

Per case:

- `exact_match`: NFKC + casefold + punctuation/whitespace normalization. If the reference is
  a JSON object or array, parsed values are compared instead.
- `facts_found` / `facts_missing`: whole-word, normalized substring search for each required fact.
- `valid_json`, `schema_valid`: JSON parse and JSON Schema (Draft 2020-12) validation.

A case **passes** if:

- all of its required facts are present (when it has any);
- its output matches its JSON Schema (when it has one);
- for cases with neither, the output is an exact match.

Exact match is still reported for every case.

Per run: `pass_rate` (passed / all cases in the run, so errors count as failures),
`exact_match_rate`, `facts_recall`, `json_valid_rate`, `schema_valid_rate`, `error_count`,
`latency_ms_p50` / `p95` (nearest rank, successful calls only), `input_tokens`, `output_tokens`.

`cost` is `null` unless pricing exists for the run's provider and model with
`effective_from <= run date`. Fake runs never have a cost. Token usage is recorded only for
the attempt that produced a stored answer. Failed cases, retried attempts and calls repeated
after a lost lease may also have been billed, but their usage is unknown. In that case
`cost.usd` is `null`, `cost.complete` is `false`, `cost.incomplete_reasons` says why, and
`cost.known_usd` is a lower bound computed from recorded usage only. `input_tokens` and
`output_tokens` cover recorded answers only. A case that failed on configuration (provider
disabled) is counted as an unaccounted call even though no request was sent: the count
errs on the side of caution.

## Comparing runs and the CLI gate

Both runs must use the same dataset version, both must be finished, and both must be synthetic
or both real. The comparison reports metric deltas plus `improved_cases`, `regressed_cases`
and `new_error_cases`.

```bash
llm-eval compare BASE_RUN HEAD_RUN \
  --min-pass-rate 0.8 --max-pass-rate-drop 0.02 --max-regressions 0 --max-error-rate 0.05 \
  -o comparison.json
llm-eval report RUN_ID --summary
```

`compare` exit codes:

- `0`: the gate passed;
- `1`: a threshold was violated (each violation is printed to stderr);
- `2`: the runs cannot be compared (unknown run, different dataset versions, unfinished run,
  or fake vs real).

The CLI talks to PostgreSQL directly (`DATABASE_URL` or `--database-url`). Inside compose:
`docker compose exec api llm-eval compare 1 2`.

## Development and tests

```bash
uv venv && uv pip install -e '.[dev]'     # or: python -m venv .venv && pip install -e '.[dev]'
ruff check .
pytest tests/unit                                    # no database needed
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/llm_eval_test pytest
```

Integration tests **drop and recreate the `public` schema** of `TEST_DATABASE_URL`, so use a
dedicated database. They cover:

- API → queue → worker → PostgreSQL on the demo dataset;
- immutability triggers, input validation and limits;
- retries with backoff, timeouts, permanent errors, `partial` and `failed` runs;
- lease takeover after a simulated crash, and rejection of a stale worker's result;
- the reaper for leases that expire on the last attempt;
- 6 concurrent workers over 60 cases, with each case processed exactly once;
- a real worker process killed with `SIGKILL` mid-job and recovered by another worker;
- pricing and cost, the fake-vs-real comparison guard, and the CLI exit codes.

CI (`.github/workflows/ci.yml`) runs ruff plus the full test suite against a PostgreSQL
service, then brings up the compose stack and runs `scripts/demo.sh`. No API keys are used.

## Limitations

- **Not verified against the real OpenAI API.** The OpenAI provider is tested only with a
  mocked HTTP transport (request shape, status-code mapping, timeouts, no key leakage).
- At-least-once delivery: duplicate provider calls are possible after a crash (see above).
- Provider timeouts are enforced by the provider itself (HTTP timeout for OpenAI, simulated
  for the fake). There is no hard kill of a hung call beyond that.
- The upload size limit is checked after the multipart body is received. Put a request-size
  limit on the reverse proxy in production.
- No authentication or authorization on the API.
- No run cancellation, and no per-provider rate limiting beyond retries on 429.
- Fake provider tokens are word counts, and its latency is near zero unless configured. Its
  metrics only exercise the pipeline.
- Latency percentiles use nearest rank. On small runs p95 is effectively the maximum.
