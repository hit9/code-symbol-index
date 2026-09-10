# Restart from master: measured results

Baseline: `55e843a`. Measurements use independent CLI processes, alternating
before/after order, the same Python environment and source fixture, and temporary
databases on local `/tmp`. They do not measure cold filesystem caches or promise
the same absolute latency on a shared mount. No user index is refreshed.

## Step 1: byte positions and symbol-only extraction

Reproduce: `python benchmarks/bench_restart.py --samples 5 --output /tmp/step1.json`.
The script records Python/platform metadata and all samples. These are medians
of five runs, not p95 estimates.

| Fixture / command | master ms | step 1 ms |
| --- | ---: | ---: |
| 16 small files / new index | 115.71 | 111.52 |
| 16 small files / unchanged index | 88.03 | 89.33 |
| 16 small files / update one | 89.53 | 86.85 |
| 16 small files / update two | 97.33 | 98.90 |
| 4 large files / new index | 263.99 | 175.25 |
| 4 large files / unchanged index | 84.20 | 84.12 |
| 4 large files / update one | 210.19 | 119.83 |
| 4 large files / update two | 246.33 | 152.99 |
| 4 large files / inspect target | 2373.86 | 670.47 |
| 4 large files / refs target | 987.10 | 143.24 |

Large files contain 1,000 functions each. Small cases are dominated by interpreter
startup; the 1–2 ms differences above need more samples before being treated as a
regression. Large-file indexing and updates improved rather than paying for query
acceleration. No schema or stored-data change: the benchmark compares files,
symbols and refs rows after every write case, and query stdout against master.
Existing and new tests: 145 passed. This step does not resolve the full real-world
query bottleneck or implement automatic stale warnings.
