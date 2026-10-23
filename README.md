# pr-merge-audit

Merge-tests the open pull requests of a repository against its base branch and writes a
Markdown report of which PRs combine cleanly, which conflict, and which break each other's tests.

No AI or network is involved after the snapshot step: the same snapshot, configuration and
machine always produce the same report.

## Requirements

- Python 3.8 or later, standard library only
- git 2.38 or later (`git merge-tree --write-tree`)
- Optional: `GITHUB_TOKEN` in the environment for the snapshot step (unauthenticated API
  access is rate-limited)

## Usage

```
python3 audit.py snapshot                                  # freeze PR heads and metadata
python3 audit.py build  --snapshot snapshots/DATE.json     # merge, test, swap, bisect (offline)
python3 audit.py report --results results/DATE.json --out reports/DATE.md
python3 audit.py verify --results results/DATE.json --golden reports/DATE.md
```

`snapshot` writes `snapshots/DATE.json` (PR numbers, titles, head commits, changed files,
required-status state, linked issues) and `snapshots/DATE.bundle` (every commit involved), so a
build can be re-run later even after PR branches move or are deleted.

## Procedure

1. **Classify.** Each open PR is either tried or skipped with a reason, in this order: excluded
   in the config, required status check not successful, draft, listed as a duplicate, listed as
   a feature, or changes files without touching the test directory (unless listed in
   `no_tests_ok`).
2. **Sequential build.** Tried PRs are merged with `git merge --no-ff` onto the base in the
   configured order (unlisted PRs follow by number). The test command runs after each merge.
   A conflict is aborted; a test failure is re-run once and the merge is reset. A failure that
   passes on re-run is recorded as flaky.
3. **Swap test.** For each conflicting PR, the merged PRs it conflicts with pairwise
   (`git merge-tree`) are reverted, the PR is merged in their place, and the tests run.
4. **Culprit search.** For each PR that fails tests without a text conflict, or still fails after
   its swap, the merge sequence is bisected to the first PR that makes it fail, and that pair is
   tested alone on the base.
5. **Probes.** Each file in `probes` is copied into the test directory of each listed target
   (the base, or a PR head) and run on its own.

Merge commits use a fixed identity and timestamp, global and system git config are ignored,
and tests run with a fixed locale, time zone and hash seed and without the caller's environment
variables (other than `PATH`, `HOME` and `TMPDIR`).

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `repo`, `remote`, `api`, `base` | Repository, fetch URL, API root, base branch |
| `required_status` | Status or check name a PR must pass to be tried (empty: none) |
| `test_cmd`, `test_prefix`, `test_timeout` | Test command (`{python}` is replaced), test directory, seconds |
| `python`, `workers` | Interpreter for tests (default: the one running the tool), parallel jobs |
| `order` | Merge order; it decides which side of each conflict merged first |
| `exclude`, `duplicates`, `features` | PR number to reason; these PRs are not tried |
| `no_tests_ok` | PRs tried even though they change no tests |
| `probes` | `{name, file, targets}`; targets are `"base"` or PR numbers |

## Self-check

`tests/golden/` holds the snapshot, results and report of a real run (open PRs as of
2026-10-23). Rendering is checked with:

```
python3 audit.py verify --results tests/golden/results.json --golden tests/golden/report.md
```

which renders the report twice and compares both renders with the golden copy byte for byte.
Re-running the build itself needs the snapshot's `.bundle`, which is kept out of the repository
because of its size; `snapshot` recreates one for the current state of the PRs.
