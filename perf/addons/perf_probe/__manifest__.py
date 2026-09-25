{
    'name': 'Performance Probe',
    'summary': 'Per-request SQL/Python counters exposed as response headers (benchmarks only)',
    'description': """
Server-wide module used by the CI performance benchmark (see perf/README.md).

Load it with ``--load=base,web,perf_probe``. Never load it in production: it
adds X-Perf-* response headers that reveal server-side timing information.
""",
    'version': '1.0',
    'category': 'Hidden',
    'depends': ['base'],
    'author': 'Odoo S.A.',
    'license': 'LGPL-3',
}
