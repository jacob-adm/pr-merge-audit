# Testing

## Tests

```
python3 -m unittest discover -s tests
```

`tests/test_pr_merge_audit.py` builds small git repositories in temporary directories and runs the real
tool on them (no mocks): merge, conflict and swap, a cross-PR test failure found by bisection, a
PR that fails on the base alone, the not-tried reasons, probes, run counts, determinism across
two builds, test isolation from `.git`, archive and probe containment, hidden and early-exiting
tests, the emitted branch and its manifest, background processes, report escaping, output capping, the work-directory marker and
token handling on redirects. It also checks the golden report. The suite takes a few seconds.

## Self-check

`tests/golden/` holds the results of a build of the test fixture in `tests/test_pr_merge_audit.py` and
the report they render to. Rendering is checked with:

```
python3 pr_merge_audit.py verify --results tests/golden/results.json --golden tests/golden/report.md
```

which renders the report twice and compares both renders with the golden copy byte for byte.
Version 1.1 adds the serial-landing line to the report, so a report written by 1.0.x no longer
matches its results under `verify`; render it again with `report` first.
After a deliberate change to the report, rebuild the fixture and replace both files.
