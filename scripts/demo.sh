#!/usr/bin/env bash
# End-to-end demo against a running `docker compose` stack (fake provider, no keys).
# Uploads the demo dataset, runs two prompt versions, compares them with the CLI gate.
set -euo pipefail

API="${API_URL:-http://localhost:8000}"
cd "$(dirname "$0")/.."

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

for _ in $(seq 1 60); do
  curl -fsS "$API/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$API/health" >/dev/null

DV=$(curl -fsS -F file=@data/demo.jsonl "$API/datasets/demo/versions" | json 'd["dataset_version_id"]')
P1=$(curl -fsS -H 'content-type: application/json' "$API/prompts/qa/versions" \
  -d '{"template": "Answer the question.\n\nQuestion: {{question}}\nAnswer:"}' | json 'd["prompt_version_id"]')
P2=$(curl -fsS -H 'content-type: application/json' "$API/prompts/qa/versions" \
  -d '{"template": "Reply briefly.\n\n{{question}}"}' | json 'd["prompt_version_id"]')
echo "dataset_version=$DV prompt_versions=$P1,$P2"

BASE=$(curl -fsS -H 'content-type: application/json' "$API/runs" \
  -d "{\"dataset_version_id\": $DV, \"prompt_version_id\": $P1}" | json 'd["id"]')
HEAD=$(curl -fsS -H 'content-type: application/json' "$API/runs" \
  -d "{\"dataset_version_id\": $DV, \"prompt_version_id\": $P2, \"config\": {\"fake\": {\"accuracy\": 0.6}}}" \
  | json 'd["id"]')
echo "runs: base=$BASE head=$HEAD"

for run in "$BASE" "$HEAD"; do
  for _ in $(seq 1 120); do
    status=$(curl -fsS "$API/runs/$run" | json 'd["status"]')
    case "$status" in completed|partial|failed) break ;; esac
    sleep 1
  done
  echo "run $run: $status"
  [ "$status" = completed ] || { echo "unexpected status $status" >&2; exit 1; }
done

curl -fsS "$API/runs/$HEAD/report?include_cases=false" | json 'json.dumps(d["metrics"], indent=2)'

echo "--- gate that should pass (no thresholds)"
docker compose exec -T api llm-eval compare "$BASE" "$HEAD" -o /tmp/cmp.json
echo "--- gate that should fail (no regressions allowed)"
set +e
docker compose exec -T api llm-eval compare "$BASE" "$HEAD" --max-regressions 0 -o /tmp/cmp.json
code=$?
set -e
echo "exit code: $code"
[ "$code" -eq 1 ] || { echo "expected exit code 1" >&2; exit 1; }
echo "demo OK"
