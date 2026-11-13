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
- Linux, macOS or Windows. Each test run gets a private home and temp directory, and the tool
  stops every process the run started when it ends (a process group on Linux and macOS, a job
  object on Windows). The target project's own test command must of course run on that platform.
- For `snapshot` only: GitHub (github.com, or GitHub Enterprise through `api`). Optional:
  `GITHUB_TOKEN` in the environment (unauthenticated API access is rate-limited).

`snapshot` is the only GitHub-specific step. For another host (GitLab, Gitea, Bitbucket), write
the two snapshot files yourself and run `build` on them:

- `snapshots/DATE.json`: `{"date", "repo", "base_ref", "base_sha", "prs": [{"number", "title",
  "head", "draft", "status", "files"}]}`, where `status` is `SUCCESS` when the PR passes your
  required check and `files` lists the paths it changes against the base.
- `snapshots/DATE.bundle`: a `git bundle` holding `refs/audit/base` and `refs/audit/pr/N` for
  each PR.

## Install

```
pip install git+https://github.com/jacob-adm/pr-merge-audit
pr-merge-audit --help
```

or run `python3 pr_merge_audit.py` from a clone; it has no dependencies. The commands below use
`python3 pr_merge_audit.py`; `pr-merge-audit` takes the same arguments.

## Usage

```
python3 pr_merge_audit.py --config PROJECT/config.json snapshot   # freeze PR heads and metadata
python3 pr_merge_audit.py --config PROJECT/config.json build --snapshot snapshots/DATE.json   # offline
python3 pr_merge_audit.py --config PROJECT/config.json build --snapshot snapshots/DATE.json \
    --emit-branch refs/heads/integration --emit-repo PATH/TO/CLONE   # also keep the merged build as a branch
python3 pr_merge_audit.py report --results results/DATE.json --out reports/DATE.md
python3 pr_merge_audit.py verify --results results/DATE.json --golden reports/DATE.md
python3 pr_merge_audit.py --config PROJECT/config.json estimate --snapshot snapshots/DATE.json \
    --reference results/EARLIER.json                     # rough running time on this machine
```

Paths are relative to the current directory, so run it from wherever you keep that project's
snapshots and results. `report` and `verify` need no configuration.

`snapshot` writes `snapshots/DATE.json` (PR numbers, titles, head commits, changed files,
draft flag, required-status state) and `snapshots/DATE.bundle` (every commit involved), so a
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
none of the caller's environment variables other than `PATH` (plus proxy and certificate settings)
and those listed in `pass_env`.

## Keeping the merged build as a branch

`build --emit-branch REF --emit-repo PATH` keeps the result of the sequential build: a new branch
`REF` at its last commit, which is the base with every PR that merged and passed, merged in the
configured order. Land it as one pull request instead of landing the PRs one by one.

- **Which repository.** The build runs in a private repository loaded from the snapshot bundle and
  deleted at the end, so the branch goes to the local git repository at `PATH` (found as
  `git -C PATH` would), usually your clone of the project. Nothing else receives it.
- **What is written there.** The commits the branch needs, the one ref `REF`, and that ref's
  reflog entry, which carries your own git identity and the current time (the commits keep the
  fixed merge identity). No remote, tag, `FETCH_HEAD` or other ref changes, and nothing is
  pushed: push the branch yourself when you are ready.
- **Checked before the build.** The two flags go together. `REF` must be a full branch name
  (`refs/heads/...`) that is not a symbolic ref, and `PATH` must use the snapshot's object format
  (SHA-1 or SHA-256). A dry run of the final write, which locks `REF` and lets it go, finds an
  existing branch of that name and clashes such as `x` beside `x/y`. Any of these stops the build
  before any test runs, with git's own message where git gave one.
- **Never overwrites.** The write itself requires that `REF` still does not exist and is not a
  symbolic ref. If either has changed during the build, the results are written without the
  branch and the build exits with an error.
- **Manifest.** The results file gains an `emitted` key: the repository's git directory, the
  ref, the commit, the PRs included (in merge order) and every PR excluded with its reason
  (`conflict`, `fail` or `flaky` for PRs that were tried, `not tried: ...` for the rest). The
  report does not read it, so it is byte-identical to that of a build without the flag.
- **Reproducible.** Merge commits use a fixed identity and timestamp, so the same snapshot,
  configuration and test outcomes give the same commit id.

Swap tests, culprit searches and probes do not change the branch. A PR that conflicts and
passes once swapped in is excluded: it conflicts with a PR on the branch, so it cannot join as it is.
If no PR merged, the branch is the base itself.

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
  environment. Subprocesses never go through a shell. Test runs also get the variables you list in
  `pass_env` and `env`; anything listed there is visible to every PR's code.
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
python3 pr_merge_audit.py --config PROJECT/config.json estimate --snapshot snapshots/DATE.json --reference results/EARLIER.json
```

The estimate uses the counts in the reference results, scaled by how many PRs the new snapshot
would try. It is a guide, not a promise: the number of conflicts and failures, and so
the number of follow-up runs, depends on the PRs.

The results file also records the measured time of every run under `timing`, by phase:
the count, total, median and maximum seconds, and the wall-clock time of the sequential build
and of the follow-ups.

### A measured build

One full build of a Python project with 111 open PRs, on a 14-core laptop
with `workers: 4`. The tool tried 76 PRs. 53 merged, 18 conflicted and 5 failed.
The suite grew from 70 tests on the base to 248 after the last merge.

| Phase | Runs | Median run | Longest run |
| :--- | ---: | ---: | ---: |
| Sequential build | 64 | 76 s | 139 s |
| Swap tests | 14 | 163 s | 189 s |
| Culprit searches (5 PRs) | 57 | 62 s | 148 s |
| Probes | 3 | 0.5 s | 0.8 s |

Wall clock: 72 min for the sequential build, 38 min for the follow-ups (swaps, culprit
searches and probes on 4 workers), 110 min in total.

Use these numbers to estimate a build of your own repository:

```
sequential  ≈ (1 + merged + 2 × failed) × suite time
follow-ups  ≈ (conflicts + 11 × failed) × suite time ÷ workers   (at least one culprit search, ~11 runs, end to end)
total       ≈ sequential + follow-ups
```

Here "suite time" is the average time of one test-suite run across the build. Since every
merge can add tests, it grows as the build goes on, so time the suite on a branch near the
end state rather than on the base. In the example the suite ran in 68 s on average, so the
formula gives (1 + 53 + 10) × 68 s ≈ 73 min, and (18 + 55) × 68 s ÷ 4 ≈ 21 min, which is
lower than the 38 min measured. The difference is the longest culprit search, which bisects
one step at a time, and the swap tests, which run the full suite after the whole merge
sequence. Treat the follow-up figure as a minimum.

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
| `pass_env` | Names of variables copied from your environment into test runs (e.g. `RUSTUP_HOME`, `CARGO_HOME`, `GOPATH`, `VIRTUAL_ENV`) |
| `env` | Variables set in test runs, applied last (can override `HOME`) |
| `allow_incomplete_checkout` | Run even though the base uses submodules or Git LFS, which test runs lack (shown in the report) |
| `setup_cmd` | Command run before `test_cmd` in the same copy (e.g. installing dependencies); if it fails, the run fails as `(setup failed)` |
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
After a deliberate change to the report, rebuild the fixture and replace both files.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for how the project is developed and what is enforced,
and [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

## License

Apache License 2.0; see `LICENSE`.
