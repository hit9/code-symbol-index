# Language support acceptance review

## Decision

**Accepted, and released as 0.6.0.** The maintainer accepted the remaining
fresh-index regression on C/C++-heavy sources described below. The
correctness defects found during this review are fixed and regression-tested, and
the write and query costs are now measured against a like-for-like baseline. One
regression remains: a fresh index of C/C++-heavy sources is up to 8% slower
because the corrected rules publish up to 36% more symbols. Every other measured
operation is within noise or faster.

Baseline: `4d942db` (0.5.5). The reviewed implementation and its fixes stay on the
local `feature/c-cpp-language-support` branch. No merge, tag or push was performed.

## Correctness and simplification

- Definition preference previously replaced an exact variable match with an
  unrelated prefix function. Preference now requires a bounded set of callable
  candidates with the same name and language.
- Python nested defaults/decorators were assigned to the inner function by callers
  while callees assigned them to the outer function. Both now use callable body
  boundaries; module-level defaults have no function caller.
- Qualified C++ namespace functions were classified as methods, and out-of-class
  constructors disagreed with their declarations. Namespace and type scopes are
  separated. Changed C++ rules use revision `cpp:2`.
- Header-language caches outlived a request. Reused repository objects now observe
  another writer's conversion. `status --check` uses the persisted header setting.
- Permission failures could delete existing rows. Refresh aborts on unreadable
  directory traversal; unreadable files retain their previous symbols/revision.
  Filtered scans cannot infer deletion of headers they excluded during a partial
  language conversion, or acknowledge an unseen Git checkout globally.
- Python, TypeScript, Swift and Kotlin multi-binding previews missed later names.
  Native lookup and traversal now share the binding rules and agree on LF/CRLF.
- Decorated Python classes lost their fields. Decorated nested classes now retain
  fields too, while function locals stay excluded. These rules use `python:2`.
- `inspect` counted a plain read of a function value as a call relation. A small
  C++ corpus reported 50 callers that never call the function. Call relations now
  come from call references only.
- Removed unused C declaration flags and unused point/pattern helpers. Writers no
  longer collect query-only callable bodies. Nested-body exclusion merges intervals
  once. Worker payloads use plain symbol rows, and C/C++ definition preference
  checks candidate ancestors instead of extracting the entire file again.

Public signatures remain compatible with existing calls; new options are optional.
Results intentionally change where language extraction was incorrect: more symbols
are published, and `inspect` no longer reports read-only references as calls. That
is a corrected result set, not a compatibility break. Schema stays 5 with additive
nullable file metadata; explicit writes upgrade affected files. Reads do not
perform the upgrade.

## Optimizations kept, and what was rejected

Kept, each with its own commit: batching small files through the process pool and
scheduling large files first; reusing bounded parse trees inside one query request;
skipping identical symbol/FTS rows during a rule upgrade; flattened worker payloads;
bounded C/C++ definition preference; and, on the symbol-only write path for C/C++,
skipping punctuation and keyword leaves during traversal.

Rejected after measurement:

- A separate C/C++ symbol-only traversal (`_c_write_symbols`, commit `b6081b2`)
  produced symbol lists identical to the general walk, and cut extraction of 36
  public headers from 74.9 ms to 68.6 ms, but was worth only about 0.8%
  end-to-end (libstdc++ fresh index 1.064x of baseline with it, 1.072x without,
  reproduced in both running orders). That did not pay for a second copy of the
  C/C++ declaration and container rules, so it was reverted.
- Populating the FTS table with `INSERT ... SELECT` from `symbols` instead of
  Python-built rows: 11.8 ms to 10.9 ms per 4,138 symbols, about 1% of a fresh
  index of the public headers. Not taken: too little for the added coupling.
- Native tree-sitter queries (about 8 ms of per-process C++ query compilation,
  a net loss), multi-VALUES FTS inserts (slower), larger SQLite cache/page sizes
  (no measurable change), and deferred index creation (about 3 ms).

## Method

Environment: Linux 6.8.0 aarch64, local temporary storage, Python 3.13.13,
tree-sitter 0.26.0, tree-sitter-language-pack 1.17.0, pathspec 1.1.1.
Each measurement runs seven alternating independent CLI processes on the same
interpreter, with copied implementation modules on the same storage and warm
filesystem and bytecode caches. Tests were not running during measurement. No
private source tree or index was read or written; corpora are generated fixtures
and installed public sources. **Cold-cache latency, p95 and shared-mount
behaviour were not measured and nothing here should be extrapolated to them.**

Ratios are current/baseline unless stated otherwise; below 1 is faster. Times are
median milliseconds. Every raw sample is in `language-support-review.json`.

Reproduce:

```sh
.venv/bin/python benchmarks/bench_public_language_support.py --samples 7 --baseline 4d942db --output /tmp/csi-public-final.json
.venv/bin/python benchmarks/bench_language_support.py --samples 7 --baseline 4d942db --corpora c_small,cpp_small,control_small,c_large --cases all --temp-parent /tmp --output /tmp/csi-small-final.json
.venv/bin/python benchmarks/bench_language_support.py --samples 7 --baseline 4d942db --corpora c_1k,c_10k --cases index_new,index_no_change,upgrade,update_one,update_two --temp-parent /tmp --output /tmp/csi-scale-final.json
```

## Write performance

| Corpus / operation | Baseline | Current | Ratio |
|---|---:|---:|---:|
| 65 small C files / fresh index | 79.36 | 84.21 | **1.061** |
| 65 small C++ files / fresh index | 83.92 | 86.16 | 1.027 |
| 64 Go files (control) / fresh index | 77.78 | 76.55 | 0.984 |
| Four large C files + header / fresh index | 207.94 | 180.85 | 0.870 |
| Large C / update one | 124.34 | 117.60 | 0.946 |
| Large C / update two | 183.87 | 164.25 | 0.893 |
| 1,001 small C files / fresh index | 251.07 | 178.13 | 0.709 |
| 1,001 small C files / unchanged index | 56.08 | 58.47 | 1.043 |
| 10,001 small C files / fresh index | 3031.94 | 2688.71 | 0.887 |
| 10,001 small C files / unchanged index | 134.84 | 140.43 | 1.041 |
| 10,001 small C files / update one | 65.35 | 63.87 | 0.977 |
| 36 public C++ headers / fresh index | 135.19 | 146.04 | **1.080** |
| Public C++ headers / unchanged index | 47.61 | 49.00 | 1.029 |
| Public C++ headers / update one selected file | 62.96 | 64.79 | 1.029 |
| 633 Python stdlib files / fresh index | 735.19 | 733.61 | 0.998 |
| Python stdlib / unchanged index | 54.10 | 56.29 | 1.040 |
| Python stdlib / update one selected file | 63.86 | 59.80 | 0.936 |

Published symbols per corpus: 65 small C 514→898, 65 small C++ 449→641, Go control
320→320, large C 8,001→8,001, 1,001 C files 4,001→6,001, 10,001 C files
40,001→60,001, public C++ headers 3,033→4,138 (+36%), Python stdlib
27,392→29,705. The Go control corpus is unchanged in both symbols and time, which
is what isolates the cost to the changed C/C++ rules.

The public C++ set is the first 36 sorted `.h` files of installed libstdc++ 13
`bits` (894,912 bytes), copied with `.hpp` suffixes so **both** versions parse C++.
The single-file update target is chosen deterministically and recorded in the raw
JSON (`algorithmfwd.h` here). The Python set is all 633 stdlib `.py` files outside
site-packages (11,295,146 bytes). A per-file update measurement does not represent
every file.

### The remaining regression

Two fresh-index cases stay above the 5% screening line: 65 small C files at 1.061x
(+4.9 ms absolute, samples 83.0–85.8 ms) and 36 public C++ headers at 1.080x
(+10.9 ms absolute, samples 144.3–148.0 ms). Both are the corpora whose symbol
count grows most (+75% and +36%). Storing correct extra symbols is real work:
inserting the public set's 4,138 symbol rows costs 11.0 ms and their FTS rows
11.8 ms, measured directly. Normalised per symbol, the current code is about 19%
*cheaper* than the baseline on that corpus (roughly 23 µs versus 28 µs per symbol
once the ~48 ms unchanged-index floor is subtracted).

So the regression is a volume effect, not a per-unit slowdown, and it does not
scale: the 1,001- and 10,001-file C corpora also gain 50% more symbols yet index
29% and 11% *faster* than the baseline, because batching dominates at that size.
Reducing it further would mean publishing fewer correct symbols. The remaining
offsets that were found are about 1% each and cost more complexity than they save
(see rejected list). The maintainer accepted this regression for 0.6.0.

## Rule upgrade

A one-time upgrade re-extracts files whose language rules changed. It runs on an
explicit `index`, never on a read.

Comparing against the baseline's *no-change* index is not a like-for-like
comparison — the baseline has no upgrade to perform, so it measures nothing.
Those ratios (1.7x–38x, in the raw JSON) are the cost of work the baseline never
does, not a regression.

Against the previous upgrade implementation (`aa9f266`, same upgrade work, same
baseline-written database, seven alternating runs):

| Corpus | Previous | Current | Ratio |
|---|---:|---:|---:|
| 65 small C files | 83.8 | 84.2 | 1.004 |
| 65 small C++ files | 83.0 | 84.6 | 1.019 |
| Four large C files + header | 223.0 | 128.5 | **0.576** |
| 1,001 small C files | 301.3 | 222.7 | 0.739 |
| 10,001 small C files | 5101.0 | 5112.0 | 1.002 |

The identical-symbol skip pays off exactly where the new rules leave a file's
symbols unchanged (large C: 8,001 symbols in, 8,001 out, 42% faster). Where the
rules genuinely change the symbols, there is nothing to skip and the upgrade costs
what it costs; the 1,001-file gain comes from the parse batching, and at 10,001
files the added symbol writes cancel it. Absolute upgrade cost for 10,001 C files
is about 5.1 seconds, once.

Upgrade behaviour is regression-tested: revision recording per language, refs
cleanup, stale symbols replaced, failed files retried next time without reparsing
successes, language-filtered refresh not deleting other languages, path updates not
upgrading unrelated files, old `files` table layouts staying readable, and a second
`index` parsing nothing.

## Query performance

| Corpus / command | Baseline | Current | Ratio |
|---|---:|---:|---:|
| Small C++ / inspect | 61.72 | 57.99 | 0.940 |
| Small C++ / callers | 53.19 | 55.81 | 1.049 |
| Small C++ / refs | 48.46 | 49.65 | 1.025 |
| Large C / inspect | 340.30 | 279.27 | 0.821 |
| Large C / refs | 84.02 | 61.82 | 0.736 |
| Large C / callers | 230.95 | 211.26 | 0.915 |
| Large C / callees | 141.13 | 138.77 | 0.983 |
| Large C / search | 82.33 | 84.36 | 1.025 |
| Large C / outline | 66.45 | 67.45 | 1.015 |
| Go control / inspect | 49.90 | 52.10 | 1.044 |
| Go control / refs | 49.02 | 50.39 | 1.028 |
| `status` (large C) | 34.92 | 35.75 | 1.024 |
| `version` (large C) | 30.55 | 31.65 | 1.036 |

The query regressions reported earlier in this review are gone: large C `inspect`
1.125→0.821, `refs` 1.388→0.736, `callers` 1.149→0.915, small C++ `inspect`
1.147→0.940. Bounded definition preference and per-request parse-tree reuse account
for the gains. What remains at 1.02–1.05 is within the ±5% band that `version` and
`status` — which do no query work at all — also show, i.e. process start-up noise
on this machine, not per-query cost.

Outputs were checked, not just timings: large-file call-graph queries carry a path
filter because the fixture deliberately repeats function names across four files,
so an unqualified query returns an ambiguity error. A fast ambiguity error is not
a speed-up and is not counted as one.

## Verification

444 tests pass on Python 3.11.15, 3.12.3, 3.13.13, 3.14.4 and 3.15.0a8. Ruff
passes. `make check smoke` passes. The equivalence of the two C/C++ traversal
paths — queries walk every node, writes skip leaves — is locked by a test over
nested namespaces, templates, function-local types, broken syntax with recovery and
CRLF sources; widening the leaf-skip rule makes it fail.

## Known limitations

Unchanged by this review: C/C++ resolution is syntactic; one `.h` language setting
cannot represent mixed C and C++ headers in a single repository; Rust receiver
dispatch is not type-resolved; declaration preference is bounded but may still parse
several candidate files. Cold-cache, p95 and network/shared-mount performance are
unmeasured.
