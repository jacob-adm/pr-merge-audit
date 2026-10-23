# pr-merge-audit

Merge-tests the open pull requests of a repository against its base branch and writes a
Markdown report of which PRs combine cleanly, which conflict, and which break each other's tests.

No AI or network is involved after the snapshot step: the same snapshot, configuration and
machine always produce the same report.

The tool is not tied to any project, language or test runner. Everything about the repository
under test (where it lives, how its tests run, how their output reads) is in a configuration
file kept with that project, not here. `config.example.json` is a starting point.

## Requirements

- Python 3.8 or later, standard library only
- git 2.38 or later (`git merge-tree --write-tree`)
- For `snapshot` only: a GitHub-compatible API and `refs/pull/N/head` refs. Optional:
  `GITHUB_TOKEN` in the environment (unauthenticated API access is rate-limited). `build`,
  `report` and `verify` read only the snapshot file and bundle, so a snapshot made any other
  way works too.

## Usage

```
python3 audit.py --config PROJECT/config.json snapshot   # freeze PR heads and metadata
python3 audit.py --config PROJECT/config.json build --snapshot snapshots/DATE.json   # offline
python3 audit.py report --results results/DATE.json --out reports/DATE.md
python3 audit.py verify --results results/DATE.json --golden reports/DATE.md
python3 audit.py --config PROJECT/config.json estimate --snapshot snapshots/DATE.json \
    --reference results/EARLIER.json                     # rough running time on this machine
```

Paths are relative to the current directory, so run it from wherever you keep that project's
snapshots and results. `report` and `verify` need no configuration.

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
5. **Probes.** Each file in `probes` is copied into each listed target (the base, or a PR head)
   and run with the probe's own command.

Merge commits use a fixed identity and timestamp, global and system git config are ignored,
and tests run with a fixed locale, time zone and hash seed, a private `HOME` and `TMPDIR`, and
none of the caller's environment variables other than `PATH` (plus proxy and certificate settings).

## Security

`build` runs the test code of every PR it tries, and also runs each probe on its listed targets
whatever the filters decided about them; `estimate` runs the base's tests. That code runs as you,
with access to everything your user can reach. **Run builds on a disposable machine or
container that holds no credentials, keys or tokens.** Nothing in this tool is a sandbox.

Because a PR's code runs inside the test runner, a PR written to fake a pass can do so. A pass
needs exit status 0 and, when `count_pattern` is set, at least one test counted. That catches an
early exit or an empty run, but not output crafted to look like a real one.

What the tool does to limit damage short of that:

- Tests run in a fresh copy of the commit's files, made with `git archive`, with no `.git`
  directory, in a new temporary directory with its own `HOME` and `TMPDIR`. Test code cannot
  change the shared repository's config, hooks or refs through its working tree. The commit's
  own `.gitattributes` cannot drop files from the copy (`export-ignore`) or rewrite them
  (`export-subst`).
- No archive entry is written through a link or over an entry already there (on a
  case-insensitive disk, `t` after a link `T`), and links that resolve outside the copy are
  removed before the tests run. Probe files are written only if no directory on the way is a
  symlink and no file of that name exists.
- Each test run gets its own process group, which is killed when the run ends or times out, so
  processes the tests leave in the background stop with it (unless they start their own session).
- Git runs with system and global config ignored, hooks pointed at the null device,
  `core.fsmonitor` off and only `PATH` (plus proxy and certificate settings) taken from your
  environment. Subprocesses never go through a shell.
- Test output is written to disk and at most 4 MB of it is read back.
- Text taken from test output is put in the report only inside code spans, with backticks
  replaced, `|` escaped in tables, control characters removed and a length cap, so it cannot add
  mentions, links, images or table rows to a comment.
- The work directory is only ever deleted if it holds the `.audit-workspace` marker the tool
  wrote there.
- `GITHUB_TOKEN` is sent only to the configured API host and is not passed on by redirects.

## How long it takes

Almost all the time goes into running the test suite, so the cost of a build is counted in
test-suite runs rather than seconds. Each results file records how many runs each phase used,
and the report states them:

- **Sequential build:** one run on the base, one per PR that merges, two per PR that fails
  (the re-run that checks for flakiness), none for a conflict. These run one at a time.
- **Swap tests:** one run per conflicting PR, two if it fails.
- **Culprit searches:** about log2(number of merged PRs) + 3 bisection steps per failing PR,
  each of which also rebuilds part of the merge sequence.
- **Probes:** one run per target.

Swap tests, culprit searches and probes run on `workers` parallel jobs. To estimate the
running time on your machine, time one test-suite run here and scale it by the counts of a
reference build of the same project:

```
python3 audit.py --config PROJECT/config.json estimate --snapshot snapshots/DATE.json --reference results/EARLIER.json
```

The estimate uses the counts in the reference results, scaled by how many PRs the new snapshot
would try. It is a guide, not a promise: the number of conflicts and failures, and so
the number of follow-up runs, depends on the PRs.

## Configuration

| Key | Meaning |
|---|---|
| `repo`, `remote`, `api`, `base` | Repository, fetch URL, API root, base branch |
| `required_status` | Status or check name a PR must pass to be tried (empty: none) |
| `test_cmd` | Required. Test command as a list of arguments; `{python}` is replaced |
| `test_prefix`, `test_timeout` | Test directory (default `tests/`), seconds per run (default 1800) |
| `failure_pattern` | Regex whose group 1 is a failing test's name in the output (unset: none named) |
| `count_pattern` | Regex whose group 1 is the number of tests run (unset: exit status alone decides) |
| `skipped_pattern`, `message_pattern` | Regexes for the skipped count and the first error message |
| `test_files` | Regex for test files, used to decide whether a PR changes tests (default: anything under `test_prefix`) |
| `python`, `workers` | Interpreter for tests (default: the one running the tool), parallel jobs |
| `order` | Merge order; it decides which side of each conflict merged first |
| `exclude`, `duplicates`, `features` | PR number to reason; these PRs are not tried |
| `no_tests_ok` | PRs tried even though they change no tests |
| `probes` | `{name, file, cmd, targets}`, optional `dest`; see below |

A probe's `file` (relative to the configuration file) is written to `dest` in the target, by
default `test_prefix` + `audit_probe_` + its file name, and `cmd` is run there. In `cmd`,
`{python}`, `{dest}` and `{module}` (`dest` without its suffix, with `.` for `/`) are replaced.
Targets are `"base"` or PR numbers. Probe output is read with the same patterns as the tests.

## Tests

```
python3 -m unittest discover -s tests
```

`tests/test_audit.py` builds small git repositories in temporary directories and runs the real
tool on them (no mocks): merge, conflict and swap, a cross-PR test failure found by bisection, a
PR that fails on the base alone, the not-tried reasons, probes, run counts, determinism across
two builds, test isolation from `.git`, archive and probe containment, hidden and early-exiting
tests, background processes, report escaping, output capping, the work-directory marker and
token handling on redirects. It also checks the golden report. The suite takes a few seconds.

## Self-check

`tests/golden/` holds the results of a build of the test fixture in `tests/test_audit.py` and
the report they render to. Rendering is checked with:

```
python3 audit.py verify --results tests/golden/results.json --golden tests/golden/report.md
```

which renders the report twice and compares both renders with the golden copy byte for byte.
After a deliberate change to the report, rebuild the fixture and replace both files.
