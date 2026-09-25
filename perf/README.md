# Performance benchmark

This directory holds the performance metrics measured in CI (`.github/workflows/perf.yml`).
They give everyone working on performance, humans and agents, the same numbers
to beat and a ratchet that prevents regressions.

The approach:

1. Measure a few **user journeys**: things a user does and waits for.
2. For each journey, gate CI on **deterministic counters** (SQL queries, Python
   calls, RPCs, bytes, renders). They don't depend on the machine or its load,
   so they can fail a build without flaking, and an agent can iterate on them
   locally in a few seconds.
3. Record **wall-clock timings** next to them, to check that moving the counters
   actually makes the journeys faster. They are reported, never gated: shared
   CI runners are too noisy.
4. Apply a **ratchet**: gated counters can only go down. A regression fails the
   build, and so does an improvement until `perf/baseline.json` is updated in
   the same PR. That locks the win in so it can't be lost later.

## Journeys

Defined in `journeys.py`. The web client runs in headless Chrome (Playwright)
against a real Odoo server (`--workers=0`) on a database with `sale_management`,
`crm`, `stock` and demo data.

| Journey | What is measured |
|---|---|
| `webclient_cold_load` | `/odoo` with an empty browser cache, from navigation start |
| `webclient_warm_load` | reload of `/odoo` with a populated browser cache |
| `sale_list_open` | open the Sales Orders list from the CRM pipeline |
| `sale_form_open` | click a sales order in the list |
| `crm_pipeline_open` | open the CRM pipeline kanban from the sales list |
| `discuss_channel_open` | open the General channel in Discuss, first time in the page |
| `sale_order_confirm` | click Confirm on a quotation with stored products (stock pickings) |

A journey ends when it is **settled**: its `ready` selector matches, no RPC is
in flight and the DOM has not changed for 300ms. Each journey's `prepare` step
brings the web client to the starting screen and is not measured.

## Metrics

Definitions and tolerances live in `metrics.py`.

**Gated** (median of the count iterations, compared to `baseline.json`):

| Metric | Source | Tolerance |
|---|---|---|
| `sql_queries` | `X-Perf-Sql` response header (`perf_probe` server-wide module) | exact |
| `sql_rows` | rows returned/affected by those queries | exact |
| `py_calls` | Python function calls on the server (`sys.setprofile`, count iterations only) | 1% |
| `server_requests` | requests that reached Odoo (not served from browser cache) | exact |
| `rpc_count` | XHR/fetch requests of the web client | exact |
| `js_bytes`, `css_bytes` | downloaded assets (decoded body size) | exact |
| `rpc_bytes` | RPC response payloads | 1% |
| `owl_renders` | OWL template renders (component renders and `t-call`) | 5% |

**Reported only**: `settled_ms`, `ready_ms`, server/SQL time, script time,
main-thread busy time, JS heap, layout and style recalculation counts. Each
journey is also run with the **CPU throttled 4x** (slow laptop): settled time,
total blocking time and long tasks. Timings are summarized as median / p75 /
MAD.

In addition, the `query-counts` CI job runs the existing `assertQueryCount`
tests listed in `query_count_tests.txt` (`perf/run_query_count_tests.sh`), so a
query regression in them fails the PR. Their expected counts are upper bounds
calibrated for upstream's CI, where many more modules are installed (see the
comments in `addons/web/tests/test_perf_load_menu.py`). Our counts are lower,
so these tests can't be turned into ratchets: lowering an expected count to our
value would break them in a full install. The journeys' baseline is the
ratchet.

### Determinism

The counters are reproducible, both between iterations and between
invocations. A few precautions make that possible:

* every run clones a template database, so earlier runs leave no data behind;
* the server runs with `PYTHONHASHSEED=0`;
* warm-up iterations fill the ORM caches and the browser cache first;
* browser-cached responses are not counted (Chrome replays their `X-Perf-*`
  headers);
* journeys that change data restore it: `sale_order_confirm` cancels the
  previous iteration's order, because the stock it reserved would change what
  the next confirmation does.

A few client-side behaviours still depend on timing (e.g. `/mail/store`
request batching in Discuss). The median of 5 count iterations absorbs them,
and the report lists these as *non-deterministic counters*.

## Running locally

```sh
pip install -r requirements.txt -r perf/requirements.txt
playwright install chromium        # or pass --chrome /usr/bin/google-chrome

python perf/bench.py create-db                         # template database, once (~2 min)
python perf/bench.py run --output perf-results.json    # all journeys (~4 min)
python perf/bench.py run --journeys sale_form_open --runs 3 --throttled-runs 0
python perf/bench.py compare perf-results.json         # exit code 1 if a gated counter changed
perf/run_query_count_tests.sh                          # assertQueryCount tests (~2 min)
```

PostgreSQL is reached through the usual `PG*` environment variables. Databases
and the filestore go to `.perf-data/` (or `$PERF_DATA_DIR`).

The results JSON contains, for every journey, a per-request breakdown
(`requests`). `compare` uses it to show exactly which RPC gained or lost
queries.

## Workflow for a performance change

1. Choose a journey (or add one) and run it locally. Optimize against the gated
   counters, where a change of one query shows up immediately.
2. Open the PR. CI posts a comment with the comparison against the baseline.
3. If counters went down, update the baseline in the PR: download the
   `perf-baseline` artifact of the CI run (or run `python perf/bench.py
   update-baseline perf-results.json`) and commit it as `perf/baseline.json`.
4. If a counter went up for a good reason (feature), do the same and explain
   the trade-off in the PR description. Reviewers own that decision.
5. Check the timings in the report: if a counter improvement doesn't move
   `settled_ms`, say so. The counter may not be the bottleneck.

The committed baseline must come from the CI runner (Python 3.12, the CI
Chrome), because `py_calls` depends on the Python version. Use the
`perf-baseline` artifact rather than a baseline produced on your machine.

## Adding a journey

Add a `Journey` to `JOURNEYS` in `journeys.py`, with a `ready` selector that
only matches once the screen is usable. If the journey changes data, make
`prepare` restore the state touched by the previous iteration. Run it a few
times and check that it reports no non-deterministic counters, then update the
baseline in the same PR.
