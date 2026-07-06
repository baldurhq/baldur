#!/usr/bin/env bash
# Generate demo traffic so the Baldur Overview (OSS) dashboard panels populate,
# then assert the new OSS panels are actually populated (not just registered).
#
# Hits the demo app's endpoints in a loop:
#   /demo/            happy path     -> CB CLOSED + latency histogram
#   /flaky/           intermittent   -> retries + CB trip + error traces
#   /idempotent/      dedup gate     -> idempotency decision counter
#   /system-control/  kill switch    -> system-control enabled gauge + changes
#   /api/baldur/health/?nocache=true -> health check status (recompute each hit)
#
# Run it after:
#
#   docker compose -f examples/docker/docker-compose.yml up -d
#
# then open Grafana at http://localhost:3000 (Baldur folder -> Baldur Overview).
#
# The demo app is published on host port 8080 (container port 8000). To point
# your OWN app at the stack instead of using the demo, set BASE_URL to its URL
# and skip this script.
#
# After the traffic window, the script polls the demo stack's Mimir
# Prometheus-compatible query API for each new OSS series and exits non-zero if
# any returns an empty result within the bounded retry window — turning the old
# "query it yourself in Grafana Explore" step into an automated gate that
# catches the registration != population trap. Requires jq.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
MIMIR_URL="${MIMIR_URL:-http://localhost:9009}"
DURATION_SECONDS="${DURATION_SECONDS:-60}"
SLEEP_SECONDS="${SLEEP_SECONDS:-0.2}"
ASSERT_TIMEOUT_SECONDS="${ASSERT_TIMEOUT_SECONDS:-45}"

echo "Driving demo traffic at ${BASE_URL} for ${DURATION_SECONDS}s..."
end=$((SECONDS + DURATION_SECONDS))
while [ "${SECONDS}" -lt "${end}" ]; do
    # /flaky/ returns 5xx on the simulated-fault path; -f makes curl exit
    # non-zero there, so swallow it and keep the loop going.
    curl -fsS "${BASE_URL}/demo/" >/dev/null 2>&1 || true
    curl -fsS "${BASE_URL}/flaky/" >/dev/null 2>&1 || true
    curl -fsS "${BASE_URL}/idempotent/" >/dev/null 2>&1 || true
    curl -fsS "${BASE_URL}/system-control/" >/dev/null 2>&1 || true
    # nocache=true forces the health service to recompute (and record) each hit.
    curl -fsS "${BASE_URL}/api/baldur/health/?nocache=true" >/dev/null 2>&1 || true
    sleep "${SLEEP_SECONDS}"
done
echo "Traffic done. Verifying new OSS panels are populated via Mimir..."

if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required for the population assertion. Install jq and " \
        "re-run, or query each series manually in Grafana Explore." >&2
    exit 1
fi

# Demo-drivable OSS series (Idempotency / Health / System Control). PRO panels
# are reference-only on this OSS stack and are verified on the PRO smoke (7.6B).
NEW_OSS_SERIES=(
    "baldur_idempotency_gate_decision_total"
    "baldur_health_check_status"
    "baldur_system_control_enabled"
)

assert_populated() {
    # Poll the Mimir query API until the series returns >=1 sample or the
    # bounded window elapses. OTEL scrape + Mimir ingestion lag means samples
    # are not instant, so a single immediate query would false-fail.
    local series="$1"
    local deadline=$((SECONDS + ASSERT_TIMEOUT_SECONDS))
    local count
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        count=$(curl -fsS --get "${MIMIR_URL}/prometheus/api/v1/query" \
            --data-urlencode "query=${series}" 2>/dev/null \
            | jq -r '.data.result | length' 2>/dev/null || echo 0)
        [[ "${count}" =~ ^[0-9]+$ ]] || count=0
        if [ "${count}" -gt 0 ]; then
            echo "  OK   ${series} (${count} series)"
            return 0
        fi
        sleep 2
    done
    echo "  FAIL ${series} returned no samples within ${ASSERT_TIMEOUT_SECONDS}s" >&2
    return 1
}

rc=0
for series in "${NEW_OSS_SERIES[@]}"; do
    assert_populated "${series}" || rc=1
done

if [ "${rc}" -ne 0 ]; then
    echo "Population check FAILED — at least one new OSS series is empty " \
        "(registration != population)." >&2
    exit 1
fi

echo "All new OSS series populated. Open Grafana -> Baldur folder -> Baldur Overview (OSS)."
