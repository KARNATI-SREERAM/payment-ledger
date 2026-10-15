#!/usr/bin/env bash
# Produces EVERY number in README.md and LEARNING.md from ONE fresh run, and saves the output to
# results/measured_run.txt. Steps: clean start -> lint + full test suite -> HTTP drills (latency,
# concurrency, rate limit) -> locust load test with a ledger audit before and after -> Postgres
# SIGKILL drill -> memory -> final audit.
# WARNING: deletes existing ledger data (docker compose down -v). Takes about 3-4 minutes.
# Needs the Mac venv: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
# Usage (from the project folder): ./scripts/measure_all.sh

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p results
exec > >(tee results/measured_run.txt) 2>&1  # everything below is printed AND saved

PY=.venv/bin/python
check() {  # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then echo "PASS  $1: $2 = $3"; else echo "FAIL  $1: $2 != $3"; fi
}
audit_field() {  # audit_field <name>: one value from scripts/check_ledger.py's output line
  $PY scripts/check_ledger.py | head -1 | tr ' ' '\n' | awk -F= -v k="$1" '$1==k {print $2}'
}

echo "== 0. Environment"
# `git status --porcelain` also sees new untracked and staged files (git diff --quiet does not).
echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)  git: $(git rev-parse --short HEAD)$([ -z "$(git status --porcelain)" ] || echo ' (plus uncommitted changes)')"
[ -z "$(git status --porcelain)" ] || echo "uncommitted: $(git status --porcelain | awk '{print $2}' | paste -sd' ' -)"
echo "machine: $(sysctl -n machdep.cpu.brand_string), $(( $(sysctl -n hw.memsize) / 1073741824 )) GB RAM, macOS $(sw_vers -productVersion)"
echo "docker: $(docker version --format '{{.Server.Version}}'), $(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}}' | awk '{printf "%s %s %.1f GiB available to containers", $1, $2, $3/1073741824}')"

echo "== 1. Clean start (fresh Postgres volume)"
docker compose down -v >/dev/null 2>&1
docker compose up --build -d >/dev/null 2>&1
for _ in $(seq 1 60); do [ "$(docker compose ps api --format '{{.Health}}')" = "healthy" ] && break; sleep 2; done
echo "containers: $(docker compose ps -a --format '{{.Service}}={{.State}}' | paste -sd' ' -)"
echo "api config: 1 uvicorn worker, DB pool 10+5, rate limit per client:" \
  "$(docker compose exec -T api printenv RATE_LIMIT_PER_SEC) req/s, burst $(docker compose exec -T api printenv RATE_LIMIT_BURST)"

echo "== 2. Lint and full test suite (real Postgres, ledger_test database)"
.venv/bin/ruff check .
REQUIRE_DB=1 .venv/bin/pytest -q -p no:cacheprovider | tail -1
echo "concurrency tests:"
REQUIRE_DB=1 .venv/bin/pytest -v -p no:cacheprovider tests/test_concurrency.py | grep -E "PASSED|FAILED" | sed 's/ *\[.*//'

echo "== 3. HTTP drills against the running API"
$PY benchmark/http_drills.py

echo "== 4. Load test: locust, ${LOCUST_USERS:-50} users x 10 req/s each, ${LOCUST_RUN_TIME:-60s}"
$PY scripts/check_ledger.py
transfers_before=$(audit_field customer_transfers)
keys_before=$(audit_field idempotency_keys)
stats_file=$(mktemp); locust_file=$(mktemp)
# Sample CPU and memory once, in the middle of the run, to see what the bottleneck is.
(sleep 35; docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' > "$stats_file") &
docker compose --profile loadtest run --rm locust > "$locust_file" 2>&1 \
  || echo "locust exited with an error (non-zero exit code)"
wait
grep -vE "^\[.*INFO" "$locust_file"  # the summary tables, without locust's progress log lines
echo "container CPU/memory 35 s into the load test (cpu 100% = one full core):"
sed 's/project2-payment-ledger-//' "$stats_file"
outcomes=$(grep "client-side transfer outcomes" "$locust_file")
rm -f "$stats_file" "$locust_file"
new_201=$(echo "$outcomes" | grep -oE 'new_201=[0-9]+' | cut -d= -f2)
new_422=$(echo "$outcomes" | grep -oE 'new_422_insufficient=[0-9]+' | cut -d= -f2)
echo "ledger audit after the load test:"
$PY scripts/check_ledger.py
check "new transfers in DB == transfers locust saw succeed (201)" \
  $(( $(audit_field customer_transfers) - transfers_before )) "$new_201"
check "new idempotency keys == new transfer requests (201 + 422)" \
  $(( $(audit_field idempotency_keys) - keys_before )) $(( new_201 + new_422 ))

echo "== 5. Failure drill: SIGKILL Postgres in the middle of 300 transfers"
$PY benchmark/failure_drill.py
echo "ledger audit after the crash:"
$PY scripts/check_ledger.py

echo "== 6. Memory (long-running containers, after all of the above)"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' \
  $(docker compose ps -q) | sed 's/project2-payment-ledger-//'

docker compose stop >/dev/null 2>&1
echo "== Done (stack stopped, data volume kept). Output saved to results/measured_run.txt"
