"""Metrics collected for every journey and how they are enforced in CI.

*Gated* metrics are deterministic counters. They are compared to
perf/baseline.json with a *ratchet*: a value worse than the baseline fails
the build, and so does a value better than the baseline (beyond the
tolerance), so that the win is locked in by updating the baseline in the
same PR.

*Reported* metrics are wall-clock measurements. They are too noisy on shared
CI runners to fail a build; they are published for every run so that we can
check that improving the gated counters actually makes journeys faster.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    unit: str
    description: str
    gated: bool = False
    # a value is considered different from the baseline when the difference
    # exceeds max(abs_tolerance, rel_tolerance * baseline)
    abs_tolerance: float = 0
    rel_tolerance: float = 0

    def tolerance(self, baseline):
        return max(self.abs_tolerance, self.rel_tolerance * abs(baseline))


# Deterministic counters, measured on the "count" iterations.
COUNT_METRICS = [
    Metric('sql_queries', "SQL queries", '', "SQL queries executed by the server for all requests of the journey", gated=True),
    Metric('sql_rows', "SQL rows", '', "Rows returned or affected by those queries", gated=True),
    Metric('py_calls', "Python calls", '', "Python function calls executed by the server (sys.setprofile)",
           gated=True, rel_tolerance=0.01),
    Metric('server_requests', "Server requests", '', "HTTP requests that reached the Odoo server (not served from browser cache)", gated=True),
    Metric('rpc_count', "RPCs", '', "XHR/fetch requests issued by the web client", gated=True),
    Metric('js_bytes', "JS bytes", 'B', "JavaScript downloaded (decoded body size)", gated=True),
    Metric('css_bytes', "CSS bytes", 'B', "Stylesheets downloaded (decoded body size)", gated=True),
    Metric('rpc_bytes', "RPC bytes", 'B', "XHR/fetch response payloads (decoded body size)",
           gated=True, rel_tolerance=0.01),
    Metric('owl_renders', "OWL renders", '', "OWL template renders (component renders and t-call)",
           gated=True, rel_tolerance=0.05),
    Metric('layout_count', "Layouts", '', "Browser layouts (CDP Performance.LayoutCount)"),
    Metric('style_recalc_count', "Style recalcs", '', "Browser style recalculations (CDP Performance.RecalcStyleCount)"),
]

# Wall-clock measurements, reported as median / p75 / MAD over the timing
# iterations. Never gated.
TIMING_METRICS = [
    Metric('settled_ms', "Settled", 'ms', "Trigger to settled: ready selector present, no RPC in flight, DOM idle"),
    Metric('ready_ms', "Ready", 'ms', "Trigger to ready selector present"),
    Metric('server_ms', "Server time", 'ms', "Sum of server-side time of the journey requests"),
    Metric('sql_ms', "SQL time", 'ms', "Sum of SQL time of the journey requests"),
    Metric('script_ms', "Script time", 'ms', "Main thread time spent running JS (CDP Performance.ScriptDuration)"),
    Metric('task_ms', "Main-thread busy", 'ms', "Main thread busy time (CDP Performance.TaskDuration)"),
    Metric('js_heap_mb', "JS heap", 'MB', "Used JS heap once settled"),
]

# Measured with the browser CPU throttled (slow laptop / phone), never gated.
THROTTLED_METRICS = [
    Metric('settled_ms', "Settled (4x CPU)", 'ms', "Trigger to settled with the CPU throttled"),
    Metric('total_blocking_ms', "Total blocking time (4x CPU)", 'ms', "Sum over long tasks of (duration - 50ms)"),
    Metric('long_tasks', "Long tasks (4x CPU)", '', "Main thread tasks longer than 50ms"),
]

COUNT_METRICS_BY_NAME = {metric.name: metric for metric in COUNT_METRICS}
GATED_METRICS = [metric for metric in COUNT_METRICS if metric.gated]
