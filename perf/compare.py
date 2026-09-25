"""Compare benchmark results with the committed baseline (the ratchet).

For every journey and every gated metric:

* current worse than baseline (beyond tolerance)  -> regression, fail
* current better than baseline (beyond tolerance) -> improvement, fail until
  the baseline is updated in the same PR (``bench.py update-baseline``) so
  that the win cannot be silently lost later
* otherwise                                       -> ok

Wall-clock timings are reported next to the baseline ones but never fail.
"""
import json
import logging
from pathlib import Path

from metrics import (
    COUNT_METRICS_BY_NAME,
    GATED_METRICS,
    THROTTLED_METRICS,
    TIMING_METRICS,
)

_logger = logging.getLogger('perf')

OK, REGRESSION, IMPROVEMENT = 'ok', 'regression', 'improvement'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def compare(baseline, results):
    """Return a report dict: per journey, the status of every gated metric."""
    report = {'journeys': {}, 'missing': [], 'new': [], 'failed': results['meta'].get('failed_journeys', [])}
    base_journeys = baseline.get('journeys', {})
    cur_journeys = results.get('journeys', {})
    report['missing'] = sorted(set(base_journeys) - set(cur_journeys) - set(report['failed']))
    report['new'] = sorted(set(cur_journeys) - set(base_journeys))
    for name, current in cur_journeys.items():
        base = base_journeys.get(name)
        if base is None:
            continue
        rows = []
        for metric in GATED_METRICS:
            if metric.name not in base['counts']:
                continue
            b, c = base['counts'][metric.name], current['counts'][metric.name]
            delta = c - b
            status = OK
            if abs(delta) > metric.tolerance(b):
                status = REGRESSION if delta > 0 else IMPROVEMENT
            rows.append({'metric': metric.name, 'baseline': b, 'current': c, 'delta': delta, 'status': status})
        report['journeys'][name] = {
            'metrics': rows,
            'requests': diff_requests(base.get('requests', []), current.get('requests', [])),
            'unstable': current.get('unstable_counts', []),
        }
    return report


def diff_requests(base, current):
    """Per request (route) differences of the server counters, to point at
    the RPC that regressed. Requests are matched by route and occurrence."""
    def index(requests):
        seen, out = {}, {}
        for request in requests:
            n = seen[request['request']] = seen.get(request['request'], 0) + 1
            out[request['request'], n] = request
        return out
    base_idx, cur_idx = index(base), index(current)
    diffs = []
    for key in sorted(set(base_idx) | set(cur_idx)):
        b, c = base_idx.get(key), cur_idx.get(key)
        if b and c and (b['sql_queries'], b['sql_rows']) == (c['sql_queries'], c['sql_rows']):
            continue
        diffs.append({
            'request': key[0] + (f' (#{key[1]})' if key[1] > 1 else ''),
            'change': 'added' if not b else 'removed' if not c else 'changed',
            'sql_queries': [b and b['sql_queries'], c and c['sql_queries']],
            'sql_rows': [b and b['sql_rows'], c and c['sql_rows']],
        })
    return diffs


def has_failures(report):
    return bool(
        report['missing'] or report['new'] or report['failed']
        or any(row['status'] != OK for j in report['journeys'].values() for row in j['metrics']),
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def fmt(value):
    if value is None:
        return '-'
    if isinstance(value, float):
        return f'{value:,.1f}'
    return f'{value:,}'


def fmt_delta(delta, base):
    if not delta:
        return '='
    pct = f' ({delta / base:+.1%})' if base else ''
    return f'{delta:+,}{pct}'


def timing(journey, group, name):
    stat = (journey or {}).get(group, {}).get(name)
    return stat and stat['median']


def render_markdown(baseline, results, report):
    lines = []
    status_icon = {OK: '✅', REGRESSION: '🔴 regression', IMPROVEMENT: '🟢 improved, update the baseline'}
    regressions = [(j, r) for j, d in report['journeys'].items() for r in d['metrics'] if r['status'] == REGRESSION]
    improvements = [(j, r) for j, d in report['journeys'].items() for r in d['metrics'] if r['status'] == IMPROVEMENT]

    lines.append('## Performance benchmark')
    lines.append('')
    if not has_failures(report):
        lines.append('✅ **All gated counters match the baseline.**')
    else:
        if regressions:
            lines.append(f'🔴 **{len(regressions)} gated counter(s) regressed.** Fix the regression, or if it is '
                         'justified, update the baseline and explain why in the PR description.')
        if improvements:
            lines.append(f'🟢 **{len(improvements)} gated counter(s) improved.** Lock the win in: run '
                         '`python perf/bench.py update-baseline perf-results.json` (or download the '
                         '`perf-baseline` artifact of this run) and commit `perf/baseline.json`.')
        if report['failed']:
            lines.append(f"❌ **Journeys failed to run:** {', '.join(report['failed'])} (see the server log artifact).")
        if report['missing']:
            lines.append(f"❌ **Journeys missing from the results:** {', '.join(report['missing'])}.")
        if report['new']:
            lines.append(f"❌ **Journeys not in the baseline:** {', '.join(report['new'])}. Update the baseline.")
    unstable = {j: d['unstable'] for j, d in report['journeys'].items() if d['unstable']}
    if unstable:
        lines.append('')
        lines.append('⚠️ Non-deterministic counters (values differ between identical iterations): ' + '; '.join(
            f"{j}: " + ', '.join(f"{u['metric']} {u['values']}" for u in us) for j, us in unstable.items()))
    lines.append('')

    # overview: one line per journey
    lines.append('| Journey | SQL | Py calls | RPCs | OWL renders | Settled (median) | Settled 4x CPU | TBT 4x CPU |')
    lines.append('|---|---:|---:|---:|---:|---:|---:|---:|')
    base_journeys = baseline.get('journeys', {})
    for name, current in results['journeys'].items():
        base = base_journeys.get(name)
        rows = {r['metric']: r for r in report['journeys'].get(name, {}).get('metrics', [])}

        def cell(metric):
            row = rows.get(metric)
            value = fmt(current['counts'][metric])
            if row and row['status'] != OK:
                value += f" ({'🔴' if row['status'] == REGRESSION else '🟢'} {fmt_delta(row['delta'], row['baseline'])})"
            return value

        def tcell(group, metric):
            cur, ref = timing(current, group, metric), timing(base, group, metric)
            if cur is None:
                return '-'
            text = f'{cur:,.0f} ms'
            if ref:
                text += f' ({(cur - ref) / ref:+.0%})'
            return text

        lines.append(f"| `{name}` | {cell('sql_queries')} | {cell('py_calls')} | {cell('rpc_count')} | "
                     f"{cell('owl_renders')} | {tcell('timings', 'settled_ms')} | "
                     f"{tcell('throttled', 'settled_ms')} | {tcell('throttled', 'total_blocking_ms')} |")
    lines.append('')
    lines.append('<sub>SQL, Py calls, RPCs and OWL renders are gated (ratchet). Timings are informative only: '
                 'percentages compare with the timings recorded with the baseline, possibly on another machine.</sub>')
    lines.append('')

    # details of every changed gated metric, and the requests that explain it
    changed = [name for name, d in report['journeys'].items() if any(r['status'] != OK for r in d['metrics'])]
    for name in changed:
        data = report['journeys'][name]
        lines.append(f'### `{name}`')
        lines.append('')
        lines.append('| Metric | Baseline | Current | Delta | |')
        lines.append('|---|---:|---:|---:|---|')
        for row in data['metrics']:
            if row['status'] == OK:
                continue
            label = COUNT_METRICS_BY_NAME[row['metric']].label
            lines.append(f"| {label} | {fmt(row['baseline'])} | {fmt(row['current'])} | "
                         f"{fmt_delta(row['delta'], row['baseline'])} | {status_icon[row['status']]} |")
        if data['requests']:
            lines.append('')
            lines.append('<details><summary>Server requests with different SQL counters</summary>')
            lines.append('')
            lines.append('| Request | Change | SQL queries | SQL rows |')
            lines.append('|---|---|---:|---:|')
            for diff in data['requests']:
                q, r = diff['sql_queries'], diff['sql_rows']
                lines.append(f"| `{diff['request']}` | {diff['change']} | {fmt(q[0])} → {fmt(q[1])} | "
                             f"{fmt(r[0])} → {fmt(r[1])} |")
            lines.append('')
            lines.append('</details>')
        lines.append('')

    # full timing details
    lines.append('<details><summary>Wall-clock details (median / p75 / MAD)</summary>')
    lines.append('')
    lines.append('| Journey | Metric | Median | p75 | MAD | Baseline median |')
    lines.append('|---|---|---:|---:|---:|---:|')
    for name, current in results['journeys'].items():
        base = base_journeys.get(name)
        for group, metrics in (('timings', TIMING_METRICS), ('throttled', THROTTLED_METRICS)):
            for metric in metrics:
                stat = current.get(group, {}).get(metric.name)
                if not stat:
                    continue
                lines.append(f"| `{name}` | {metric.label} | {fmt(stat['median'])} | {fmt(stat['p75'])} | "
                             f"{fmt(stat['mad'])} | {fmt(timing(base, group, metric.name))} |")
    lines.append('')
    meta = results['meta']
    lines.append(f"Commit `{(meta.get('commit') or '?')[:12]}`, Python {meta.get('python')}, Chrome {meta.get('chrome')}, "
                 f"{meta.get('cpu_count')} CPUs; {meta.get('runs')} runs, {meta.get('throttled_runs')} throttled runs. "
                 f"Baseline from commit `{(baseline.get('meta', {}).get('commit') or '?')[:12]}`.")
    lines.append('')
    lines.append('</details>')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def compare_main(args):
    results = load(args.results)
    baseline = load(args.baseline) if Path(args.baseline).exists() else {'journeys': {}}
    report = compare(baseline, results)
    markdown = render_markdown(baseline, results, report)
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding='utf-8')
    print(markdown)  # noqa: T201
    return 1 if has_failures(report) else 0


def make_baseline(results):
    """Keep what is needed to compare: gated counters, the per request
    breakdown, and timing medians for reference."""
    return {
        'meta': {k: results['meta'].get(k) for k in ('commit', 'date', 'python', 'chrome', 'cpu_count', 'modules')},
        'journeys': {
            name: {
                'description': journey['description'],
                'counts': {m.name: journey['counts'][m.name] for m in GATED_METRICS},
                'requests': journey['requests'],
                'timings': {k: {'median': v['median']} for k, v in journey['timings'].items() if v},
                'throttled': {k: {'median': v['median']} for k, v in journey['throttled'].items() if v},
            }
            for name, journey in sorted(results['journeys'].items())
        },
    }


def update_baseline_main(args):
    results = load(args.results)
    if results['meta'].get('failed_journeys'):
        _logger.error("refusing to update the baseline, journeys failed: %s", results['meta']['failed_journeys'])
        return 1
    Path(args.baseline).write_text(json.dumps(make_baseline(results), indent=1) + '\n', encoding='utf-8')
    _logger.info("baseline written to %s", args.baseline)
    return 0
