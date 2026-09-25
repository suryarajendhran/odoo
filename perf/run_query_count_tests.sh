#!/usr/bin/env bash
# Run the query count tests listed in perf/query_count_tests.txt on a fresh
# database with the modules they belong to.
#
#   perf/run_query_count_tests.sh [database]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="${1:-perf_query_counts}"
DATA_DIR="${PERF_DATA_DIR:-$ROOT/.perf-data}"
PYTHON="${PYTHON:-python3}"
LOG="${QUERY_COUNT_LOG:-query-count-tests.log}"
TESTS="$(grep -v '^\s*\(#\|$\)' "$ROOT/perf/query_count_tests.txt")"
TAGS="$(paste -sd, - <<< "$TESTS")"
MODULES="$(grep -v '^-' <<< "$TESTS" | sed 's#^/\([^:]*\):.*#\1#' | sort -u | grep -vx base | paste -sd, -)"

dropdb --if-exists --force "$DB"
: > "$LOG"  # odoo appends to the log file
rm -rf "$DATA_DIR/filestore/$DB"

echo "Installing $MODULES and running $TAGS"
rc=0
PYTHONHASHSEED=0 "$PYTHON" "$ROOT/odoo-bin" server \
    --addons-path="$ROOT/odoo/addons,$ROOT/addons" \
    --data-dir="$DATA_DIR" \
    -d "$DB" -i "$MODULES" \
    --test-tags "$TAGS" \
    --stop-after-init --max-cron-threads=0 --workers=0 \
    --http-port="${PERF_TEST_PORT:-8369}" \
    --log-level=test --logfile="$LOG" || rc=$?

grep -E "Query count|odoo.tests.result|Failed:" "$LOG" | sed 's/^[0-9-]* [0-9:,]* [0-9]* //' || true
if [ $rc -ne 0 ]; then
    echo "Query count tests failed (exit code $rc), see $LOG"
    exit $rc
fi
echo "Query count tests passed"
