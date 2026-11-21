# How it works

`snapshot` freezes the open PRs; `build` then works offline on that snapshot. No AI or network is
involved after the snapshot step: the same snapshot, configuration and machine produce the same
report, provided the project's own tests give the same result on every run. The tool cannot make
a test suite deterministic; tests that depend on randomness, timing or the clock can change the
report. `build --repeat` checks a particular build: see [Commands](commands.md#build).

## Procedure

1. **Classify.** Each open PR is either tried or skipped with a reason, in this order: excluded
   in the config, required status check not successful, draft, listed as a duplicate, listed as
   a feature, or changes files without touching the test directory (unless listed in
   `no_tests_ok`).
2. **Sequential build.** Tried PRs are merged with `git merge --no-ff` onto the base in the
   configured order (unlisted PRs follow by number). The test command runs after each merge.
   A conflict is aborted; a test failure is re-run once and the merge is reset. A failure that
   passes on re-run is recorded as flaky, with the tests that failed on the first run.
3. **Swap test.** A conflicting PR that does not merge onto the base by itself is listed as
   conflicting with the base and is not tested further: it needs a rebase, and no other PR is the
   cause. For each other conflicting PR, the merged PRs it conflicts with pairwise
   (`git merge-tree`) are reverted, the PR is merged in their place, and the tests run.
4. **Culprit search.** For each PR that fails tests without a text conflict, or still fails after
   its swap, the merge sequence is bisected for a PR that makes it fail, and that pair is tested
   alone on the base. Bisection assumes a failure, once it appears, stays as more PRs are merged;
   a later PR that repairs an earlier interaction can mislead it. The report names a pair as
   failing only when the pair fails alone; otherwise it lists the PR under Other with the
   candidate bisection pointed at.
5. **Probes.** Each file in `probes` is copied into each listed target (the base, or a PR head)
   and run with the probe's own command.

## Determinism

Merge commits use a fixed identity and timestamp, global and system git config are ignored,
and tests run with a fixed locale, time zone and hash seed, a private `HOME` and `TMPDIR`, and
none of the caller's environment variables other than `PATH` (plus proxy and certificate settings)
and those listed in `pass_env`.

Setup, test and probe commands also run without network, so nothing they fetch can change a
result: on macOS in a sandbox, on Linux in a network namespace that holds only loopback, or as
they are on a machine with no network. Localhost still works. Where none of these is possible the
build stops before running anything. With `"network": "allowed"` in the config they reach the
network, and the report says so. See [Configuration](configuration.md).

## Isolation

Each test run gets a private home and temp directory, and the tool stops every process the run
started when it ends (a process group on Linux and macOS, a job object on Windows). The target
project's own test command must of course run on that platform. See [Security](security.md) for
what this does and does not protect against.
