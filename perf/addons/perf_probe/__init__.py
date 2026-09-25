"""Per-request performance counters for the CI benchmark.

Every HTTP response gets the following headers:

* ``X-Perf-Sql``: number of SQL queries executed while handling the request
* ``X-Perf-Sql-Rows``: number of rows returned/affected by those queries
* ``X-Perf-Sql-Ms``: time spent in SQL (wall-clock, noisy)
* ``X-Perf-Server-Ms``: total time spent in the WSGI application (noisy)
* ``X-Perf-Py-Calls``: number of Python function calls, only when the request
  carries an ``X-Perf-Count-Calls: 1`` header, because counting calls slows
  the request down considerably.
"""
import logging
import sys
import threading
import time

from odoo.http.router import Application

_logger = logging.getLogger(__name__)

COUNT_CALLS_ENVIRON_KEY = 'HTTP_X_PERF_COUNT_CALLS'


class _RequestCounters:
    __slots__ = ('py_calls', 'sql_rows')

    def __init__(self):
        self.py_calls = 0
        self.sql_rows = 0

    def query_hook(self, cr, query, params, start, depth):
        def on_query_end(delay):
            rowcount = cr._obj.rowcount
            if rowcount > 0:
                self.sql_rows += rowcount
        return on_query_end

    def profile(self, frame, event, arg):
        if event == 'call':
            self.py_calls += 1


_original_call = Application.__call__


def _probed_call(self, environ, start_response):
    current_thread = threading.current_thread()
    counters = _RequestCounters()
    count_calls = environ.get(COUNT_CALLS_ENVIRON_KEY) == '1'
    query_count0 = getattr(current_thread, 'query_count', 0)
    query_time0 = getattr(current_thread, 'query_time', 0)
    if not hasattr(current_thread, 'query_hooks'):
        current_thread.query_hooks = []
    current_thread.query_hooks.append(counters.query_hook)
    start = time.perf_counter()
    profiling = False

    def probed_start_response(status, headers, exc_info=None):
        nonlocal profiling
        if profiling:
            sys.setprofile(None)
            profiling = False
        headers = [
            *headers,
            ('X-Perf-Sql', str(getattr(current_thread, 'query_count', 0) - query_count0)),
            ('X-Perf-Sql-Rows', str(counters.sql_rows)),
            ('X-Perf-Sql-Ms', f"{(getattr(current_thread, 'query_time', 0) - query_time0) * 1000:.3f}"),
            ('X-Perf-Server-Ms', f"{(time.perf_counter() - start) * 1000:.3f}"),
        ]
        if count_calls:
            headers.append(('X-Perf-Py-Calls', str(counters.py_calls)))
        if exc_info is None:
            return start_response(status, headers)
        return start_response(status, headers, exc_info)

    if count_calls:
        profiling = True
        sys.setprofile(counters.profile)
    try:
        return _original_call(self, environ, probed_start_response)
    finally:
        if profiling:
            sys.setprofile(None)
        try:
            current_thread.query_hooks.remove(counters.query_hook)
        except ValueError:
            pass


Application.__call__ = _probed_call
_logger.warning("perf_probe loaded: X-Perf-* headers are added to every HTTP response (benchmark use only)")
