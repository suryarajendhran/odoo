#!/usr/bin/env python3
"""Odoo performance benchmark.

    # once: create the template database (modules + demo data)
    python perf/bench.py create-db

    # measure every journey, write the results
    python perf/bench.py run --output perf-results.json

    # compare with the committed baseline (exit code 1 on any gated change)
    python perf/bench.py compare perf-results.json

    # lock in an improvement (or accept a justified regression)
    python perf/bench.py update-baseline perf-results.json

See perf/README.md for the metrics and the ratchet policy.
"""
import argparse
import contextlib
import json
import logging
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

PERF_DIR = Path(__file__).resolve().parent
ROOT_DIR = PERF_DIR.parent
sys.path.insert(0, str(PERF_DIR))

from compare import compare_main, update_baseline_main  # noqa: E402
from journeys import JOURNEYS, JOURNEYS_BY_NAME  # noqa: E402
from metrics import COUNT_METRICS, GATED_METRICS  # noqa: E402

_logger = logging.getLogger('perf')

MODULES = 'sale_management,crm,stock'
DEFAULT_TEMPLATE_DB = 'perf_bench_template'
DEFAULT_RUN_DB = 'perf_bench_run'
DEFAULT_DATA_DIR = ROOT_DIR / '.perf-data'
ADDONS_PATH = ','.join(str(ROOT_DIR / p) for p in ('odoo/addons', 'addons', 'perf/addons'))
VIEWPORT = {'width': 1366, 'height': 768}
CPU_THROTTLING = 4
COUNT_CALLS_HEADER = 'X-Perf-Count-Calls'
RPC_TYPES = ('XHR', 'Fetch')


# ---------------------------------------------------------------------------
# database and server
# ---------------------------------------------------------------------------

def odoo_cmd(args, *extra):
    return [
        args.odoo_python, str(ROOT_DIR / 'odoo-bin'), 'server',
        f'--addons-path={ADDONS_PATH}',
        f'--data-dir={args.data_dir}',
        '--max-cron-threads=0',
        '--workers=0',
        *extra,
    ]


def server_env():
    env = dict(os.environ)
    # make set/dict iteration order, hence query and call counts, reproducible
    env['PYTHONHASHSEED'] = '0'
    return env


def db_exists(name):
    out = subprocess.run(['psql', '-d', 'postgres', '-Atc', f"SELECT 1 FROM pg_database WHERE datname = '{name}'"],
                         check=True, capture_output=True, text=True).stdout
    return out.strip() == '1'


def filestore(args, db):
    return Path(args.data_dir) / 'filestore' / db


def create_db_main(args):
    if db_exists(args.template_db):
        if not args.force:
            _logger.info("database %s already exists, use --force to recreate it", args.template_db)
            return 0
        drop_db(args, args.template_db)
    _logger.info("creating %s with %s and demo data", args.template_db, MODULES)
    t0 = time.time()
    subprocess.run(
        odoo_cmd(args, '-d', args.template_db, '-i', MODULES, '--with-demo', '--stop-after-init', '--log-level=warn'),
        check=True, env=server_env(),
    )
    for query in TEMPLATE_SETUP_QUERIES:
        subprocess.run(['psql', '-d', args.template_db, '-v', 'ON_ERROR_STOP=1', '-qc', query], check=True)
    _logger.info("database created in %.0fs", time.time() - t0)
    return 0


# Run on the template database once the modules are installed, to keep the
# journeys independent of external services.
TEMPLATE_SETUP_QUERIES = [
    # partner_autocomplete enriches the company through IAP (an outgoing HTTP
    # request) the first time an admin loads the web client; the result
    # changes the company data. Mark it as done, as Odoo's tests do.
    "UPDATE res_company SET iap_enrich_auto_done = true",
]


def drop_db(args, db):
    subprocess.run(['dropdb', '--if-exists', '--force', db], check=True)
    shutil.rmtree(filestore(args, db), ignore_errors=True)


def clone_db(args, source, target):
    """Every run starts from an identical copy of the template database, so
    that counters do not drift with data created by previous runs."""
    drop_db(args, target)
    subprocess.run(['createdb', '-T', source, target], check=True)
    if filestore(args, source).exists():
        shutil.copytree(filestore(args, source), filestore(args, target))


@contextlib.contextmanager
def odoo_server(args, db):
    log_path = Path(args.server_log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = odoo_cmd(
        args,
        '-d', db, f'--db-filter=^{db}$',
        '--load=base,web,perf_probe',
        f'--http-port={args.port}',
        '--http-interface=127.0.0.1',
        '--log-level=warn',
        f'--logfile={log_path}',
    )
    _logger.info("starting server: %s", ' '.join(cmd))
    process = subprocess.Popen(cmd, env=server_env())
    url = f'http://127.0.0.1:{args.port}'
    try:
        deadline = time.time() + 180
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Odoo server exited with code {process.returncode}, see {log_path}")
            try:
                with urllib.request.urlopen(f'{url}/web/health', timeout=5) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.time() > deadline:
                raise RuntimeError(f"Odoo server not ready after 180s, see {log_path}")
            time.sleep(0.5)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()


# ---------------------------------------------------------------------------
# browser side collection
# ---------------------------------------------------------------------------

class NetworkCollector:
    """Record the requests of one page through the Chrome DevTools protocol."""

    def __init__(self, page):
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.send('Network.enable')
        self.cdp.send('Performance.enable')
        self.requests = {}
        self.active = False
        self.cdp.on('Network.requestWillBeSent', self._on_request)
        self.cdp.on('Network.responseReceived', self._on_response)
        self.cdp.on('Network.dataReceived', self._on_data)
        self.cdp.on('Network.requestServedFromCache', self._on_cached)

    def set_count_calls(self, enabled):
        headers = {COUNT_CALLS_HEADER: '1'} if enabled else {}
        self.cdp.send('Network.setExtraHTTPHeaders', {'headers': headers})

    def set_cpu_throttling(self, rate):
        self.cdp.send('Emulation.setCPUThrottlingRate', {'rate': rate})

    def performance_metrics(self):
        return {m['name']: m['value'] for m in self.cdp.send('Performance.getMetrics')['metrics']}

    def start(self):
        self.requests = {}
        self.active = True
        self.perf_before = self.performance_metrics()

    def stop(self):
        self.active = False
        self.perf_after = self.performance_metrics()
        return list(self.requests.values())

    def _on_request(self, event):
        if not self.active or event['request']['url'].startswith('data:'):
            return
        self.requests[event['requestId']] = {
            'url': event['request']['url'],
            'method': event['request']['method'],
            'type': event.get('type', 'Other'),
            'cached': False,
            'bytes': 0,
            'headers': {},
        }

    def _on_response(self, event):
        if request := self.requests.get(event['requestId']):
            response = event['response']
            request['status'] = response['status']
            request['headers'] = {k.lower(): v for k, v in response['headers'].items()}
            request['cached'] = request['cached'] or response.get('fromDiskCache', False) \
                or response.get('fromPrefetchCache', False)

    def _on_cached(self, event):
        if request := self.requests.get(event['requestId']):
            request['cached'] = True

    def _on_data(self, event):
        if request := self.requests.get(event['requestId']):
            request['bytes'] += event['dataLength']

    def perf_delta(self, name):
        return self.perf_after.get(name, 0) - self.perf_before.get(name, 0)


def request_key(request):
    path = urlsplit(request['url']).path
    if path.startswith('/web/assets/'):
        # /web/assets/<checksum>/<bundle>: the checksum changes with the code,
        # keep the keys comparable across commits
        path = '/web/assets/*/' + path.rsplit('/', 1)[-1]
    return f"{request['method']} {path}"


def summarize_requests(requests, page_result, collector):
    counts = {m.name: 0 for m in COUNT_METRICS}
    timings = {'server_ms': 0.0, 'sql_ms': 0.0}
    breakdown = []
    for request in requests:
        headers = request['headers']
        downloaded = 0 if request['cached'] else request['bytes']
        if request['type'] == 'Script':
            counts['js_bytes'] += downloaded
        elif request['type'] == 'Stylesheet':
            counts['css_bytes'] += downloaded
        elif request['type'] in RPC_TYPES:
            counts['rpc_count'] += 1
            counts['rpc_bytes'] += downloaded
        if request['cached'] or 'x-perf-sql' not in headers:
            # not served by odoo; cached responses replay the headers of the
            # original response, including the X-Perf ones
            continue
        counts['server_requests'] += 1
        sql = int(headers['x-perf-sql'])
        rows = int(headers.get('x-perf-sql-rows', 0))
        calls = int(headers.get('x-perf-py-calls', 0))
        counts['sql_queries'] += sql
        counts['sql_rows'] += rows
        counts['py_calls'] += calls
        timings['server_ms'] += float(headers.get('x-perf-server-ms', 0))
        timings['sql_ms'] += float(headers.get('x-perf-sql-ms', 0))
        breakdown.append({'request': request_key(request), 'sql_queries': sql, 'sql_rows': rows, 'py_calls': calls})
    counts['owl_renders'] = page_result['owl_renders']
    counts['layout_count'] = int(collector.perf_delta('LayoutCount'))
    counts['style_recalc_count'] = int(collector.perf_delta('RecalcStyleCount'))
    timings.update({
        'settled_ms': page_result['settled_ms'],
        'ready_ms': page_result['ready_ms'],
        'script_ms': collector.perf_delta('ScriptDuration') * 1000,
        'task_ms': collector.perf_delta('TaskDuration') * 1000,
        'js_heap_mb': (page_result['js_heap_bytes'] or 0) / 2**20,
        'total_blocking_ms': page_result['total_blocking_ms'],
        'long_tasks': page_result['long_tasks'],
    })
    breakdown.sort(key=lambda r: r['request'])
    return counts, timings, breakdown


class Runner:
    def __init__(self, args, playwright, url):
        self.args = args
        self.url = url
        launch = {'headless': True, 'args': ['--disable-extensions', '--no-first-run']}
        if args.chrome:
            launch['executable_path'] = args.chrome
        self.browser = playwright.chromium.launch(**launch)
        self.init_script = (PERF_DIR / 'page_instrumentation.js').read_text(encoding='utf-8')
        self.storage_state = self._login()

    def new_page(self, storage_state=True):
        context = self.browser.new_context(
            viewport=VIEWPORT,
            storage_state=self.storage_state if storage_state else None,
            locale='en-US',
            timezone_id='UTC',
        )
        context.set_default_timeout(60_000)
        context.add_init_script(self.init_script)
        page = context.new_page()
        page.on('pageerror', lambda exc: _logger.warning("page error: %s", exc))
        return page

    def _login(self):
        page = self.new_page(storage_state=False)
        page.goto(f'{self.url}/web/login')
        page.fill('input[name="login"]', self.args.login)
        page.fill('input[name="password"]', self.args.password)
        page.click('button[type="submit"]')
        page.wait_for_selector('.o_main_navbar')
        # only keep the session cookie: the web client writes to localStorage
        # while it loads, capturing it here would make the first page state
        # of the journeys depend on timing
        state = {'cookies': page.context.storage_state()['cookies'], 'origins': []}
        page.context.close()
        return state

    def wait_settled(self, page, journey):
        return page.evaluate(
            "([selector, quiet, timeout]) => __perf.waitSettled(selector, quiet, timeout)",
            [journey.ready, journey.quiet_ms, self.args.journey_timeout * 1000],
        )

    # -- one measured iteration ------------------------------------------------

    def iterate(self, journey, mode, session):
        """Measure one iteration of ``journey``; ``session`` holds the page
        (and state) reused between iterations of in-app journeys."""
        if journey.kind == 'page_load':
            return self._iterate_page_load(journey, mode, session)
        return self._iterate_in_app(journey, mode, session)

    def _configure(self, collector, mode):
        collector.set_count_calls(mode == 'count')
        collector.set_cpu_throttling(CPU_THROTTLING if mode == 'throttled' else 1)

    def _iterate_page_load(self, journey, mode, session):
        if journey.cold_cache or 'page' not in session:
            if 'page' in session:
                session.pop('page').context.close()
            page = self.new_page()
            collector = NetworkCollector(page)
            if not journey.cold_cache:
                # prime the HTTP cache
                page.goto(self.url + journey.url)
                self.wait_settled(page, journey)
            session.update(page=page, collector=collector)
        page, collector = session['page'], session['collector']
        self._configure(collector, mode)
        collector.start()
        if journey.cold_cache:
            page.goto(self.url + journey.url, wait_until='commit')
        else:
            page.reload(wait_until='commit')
        result = self.wait_settled(page, journey)
        requests = collector.stop()
        if journey.cold_cache:
            session.pop('page').context.close()
        return summarize_requests(requests, result, collector)

    def _iterate_in_app(self, journey, mode, session):
        if 'page' not in session:
            page = self.new_page()
            page.goto(f'{self.url}/odoo')
            page.wait_for_selector('.o_action_manager > *')
            session.update(page=page, collector=NetworkCollector(page), state={})
        page, collector = session['page'], session['collector']
        self._configure(collector, 'timing')
        if journey.prepare:
            journey.prepare(page, session['state'])
        # let the prepared screen settle so that it is not accounted for
        page.evaluate("() => __perf.start()")
        page.evaluate("([q, t]) => __perf.waitSettled('body', q, t)", [journey.quiet_ms, 30_000])
        self._configure(collector, mode)
        collector.start()
        page.evaluate("() => __perf.start()")
        journey.trigger(page, session['state'])
        result = self.wait_settled(page, journey)
        requests = collector.stop()
        return summarize_requests(requests, result, collector)

    # -- one journey -----------------------------------------------------------

    def run_journey(self, journey):
        args = self.args
        session = {}
        try:
            # warm up in count mode: Chrome does not reuse some cached
            # resources (e.g. images) once the extra request header changes
            for _ in range(args.warmup):
                self.iterate(journey, 'count', session)
            count_runs = [self.iterate(journey, 'count', session) for _ in range(args.count_runs)]
            timing_runs = [self.iterate(journey, 'timing', session) for _ in range(args.runs)]
            throttled_runs = [self.iterate(journey, 'throttled', session) for _ in range(args.throttled_runs)]
        finally:
            if 'page' in session:
                session['page'].context.close()

        counts, unstable = {}, []
        for metric in COUNT_METRICS:
            values = [run[0][metric.name] for run in count_runs]
            counts[metric.name] = int(statistics.median_low(values))
            if metric.gated and max(values) - min(values) > metric.tolerance(counts[metric.name]):
                unstable.append({'metric': metric.name, 'values': values})
        timing_names = ('settled_ms', 'ready_ms', 'server_ms', 'sql_ms', 'script_ms', 'task_ms', 'js_heap_mb')
        throttled_names = ('settled_ms', 'total_blocking_ms', 'long_tasks')
        result = {
            'description': journey.description,
            'counts': counts,
            'unstable_counts': unstable,
            'timings': {name: stats([run[1][name] for run in timing_runs]) for name in timing_names},
            'throttled': {name: stats([run[1][name] for run in throttled_runs]) for name in throttled_names},
            # per request breakdown, used to explain count changes
            'requests': count_runs[-1][2],
        }
        if unstable:
            result['unstable_requests'] = [run[2] for run in count_runs]
        return result


def stats(values):
    if not values:
        return None
    values = sorted(values)
    median = statistics.median(values)
    p75 = values[min(len(values) - 1, round(0.75 * (len(values) - 1)))]
    return {
        'median': round(median, 2),
        'p75': round(p75, 2),
        'mad': round(statistics.median(abs(v - median) for v in values), 2),
        'min': round(values[0], 2),
        'max': round(values[-1], 2),
        'runs': [round(v, 2) for v in values],
    }


def git_info():
    def git(*cmd):
        with contextlib.suppress(Exception):
            return subprocess.run(['git', *cmd], cwd=ROOT_DIR, check=True, capture_output=True, text=True).stdout.strip()
        return None
    return {'commit': git('rev-parse', 'HEAD'), 'branch': git('rev-parse', '--abbrev-ref', 'HEAD')}


def run_main(args):
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    journeys = JOURNEYS
    if args.journeys:
        unknown = set(args.journeys.split(',')) - set(JOURNEYS_BY_NAME)
        if unknown:
            raise SystemExit(f"unknown journeys: {', '.join(sorted(unknown))}; available: {', '.join(JOURNEYS_BY_NAME)}")
        journeys = [j for j in JOURNEYS if j.name in args.journeys.split(',')]

    if not db_exists(args.template_db):
        raise SystemExit(f"template database {args.template_db} not found, run `python perf/bench.py create-db` first")
    clone_db(args, args.template_db, args.db)

    results = {
        'meta': {
            **git_info(),
            'date': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'python': platform.python_version(),
            'platform': platform.platform(),
            'cpu_count': os.cpu_count(),
            'modules': MODULES,
            'warmup': args.warmup,
            'count_runs': args.count_runs,
            'runs': args.runs,
            'throttled_runs': args.throttled_runs,
            'cpu_throttling': CPU_THROTTLING,
            'gated_metrics': [m.name for m in GATED_METRICS],
        },
        'journeys': {},
    }
    failed = []
    try:
        with odoo_server(args, args.db) as url, sync_playwright() as playwright:
            runner = Runner(args, playwright, url)
            results['meta']['chrome'] = runner.browser.version
            for journey in journeys:
                _logger.info("journey %s", journey.name)
                t0 = time.time()
                try:
                    results['journeys'][journey.name] = result = runner.run_journey(journey)
                except Exception:
                    _logger.exception("journey %s failed", journey.name)
                    failed.append(journey.name)
                    continue
                counts = result['counts']
                _logger.info(
                    "  %.0fs  sql=%s rows=%s py_calls=%s rpcs=%s owl=%s settled=%sms (4x: %sms, TBT %sms)%s",
                    time.time() - t0, counts['sql_queries'], counts['sql_rows'], counts['py_calls'],
                    counts['rpc_count'], counts['owl_renders'], result['timings']['settled_ms']['median'],
                    result['throttled']['settled_ms']['median'] if result['throttled']['settled_ms'] else '-',
                    result['throttled']['total_blocking_ms']['median'] if result['throttled']['total_blocking_ms'] else '-',
                    f"  UNSTABLE: {result['unstable_counts']}" if result['unstable_counts'] else '',
                )
    finally:
        if not args.keep_db:
            drop_db(args, args.db)

    results['meta']['failed_journeys'] = failed
    Path(args.output).write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    _logger.info("results written to %s", args.output)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', default=os.environ.get('PERF_DATA_DIR', str(DEFAULT_DATA_DIR)),
                        help="Odoo data dir (filestore) used by the benchmark databases")
    parser.add_argument('--template-db', default=DEFAULT_TEMPLATE_DB)
    parser.add_argument('--odoo-python', default=os.environ.get('PERF_ODOO_PYTHON', sys.executable),
                        help="Python interpreter with the Odoo requirements (default: the one running this script); "
                             "the benchmark driver can live in its own virtualenv, see perf/README.md")
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('create-db', help="create the template database")
    p.add_argument('--force', action='store_true', help="drop and recreate it if it exists")
    p.set_defaults(func=create_db_main)

    p = sub.add_parser('run', help="run the journeys and write the results")
    p.add_argument('--output', default='perf-results.json')
    p.add_argument('--journeys', help="comma separated journey names (default: all)")
    p.add_argument('--db', default=DEFAULT_RUN_DB, help="scratch database cloned from the template")
    p.add_argument('--keep-db', action='store_true')
    p.add_argument('--port', type=int, default=8269)
    p.add_argument('--server-log', default='perf-server.log')
    p.add_argument('--chrome', default=os.environ.get('PERF_CHROME'), help="Chrome executable (default: playwright's)")
    p.add_argument('--login', default='admin')
    p.add_argument('--password', default='admin')
    p.add_argument('--warmup', type=int, default=2, help="discarded iterations")
    p.add_argument('--count-runs', type=int, default=5, help="iterations measuring the deterministic counters")
    p.add_argument('--runs', type=int, default=7, help="wall-clock iterations")
    p.add_argument('--throttled-runs', type=int, default=3, help=f"wall-clock iterations at {CPU_THROTTLING}x CPU throttling")
    p.add_argument('--journey-timeout', type=int, default=60, help="seconds")
    p.set_defaults(func=run_main)

    p = sub.add_parser('compare', help="compare results with the baseline")
    p.add_argument('results')
    p.add_argument('--baseline', default=str(PERF_DIR / 'baseline.json'))
    p.add_argument('--markdown', help="write a markdown report to this file")
    p.set_defaults(func=compare_main)

    p = sub.add_parser('update-baseline', help="write the gated counters of the results as the new baseline")
    p.add_argument('results')
    p.add_argument('--baseline', default=str(PERF_DIR / 'baseline.json'))
    p.set_defaults(func=update_baseline_main)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
