# pr-merge-audit

Merge-tests the open pull requests of a repository against its base branch and writes a
Markdown report of which PRs combine cleanly, which conflict, and which break each other's tests.

No AI or network is involved after the snapshot step: the same snapshot, configuration and
machine always produce the same report. The tool is not tied to any project, language or test
runner; everything about the repository under test is in a configuration file kept with that
project.

## Requirements

- Python 3.9 or later, standard library only.
- git 2.38 or later (`git merge-tree --write-tree`).
- Linux, macOS or Windows.
- For `snapshot` only: GitHub (github.com, or GitHub Enterprise). Optional: `GITHUB_TOKEN` in
  the environment (unauthenticated API access is rate-limited). For other hosts, see
  [Other hosts](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/commands.md#other-hosts).

## Install

```
pip install pr-merge-audit
pr-merge-audit --help
```

or run `python3 pr_merge_audit.py` from a clone; it has no dependencies and takes the same
arguments.

## How it works

1. **Freeze the PRs.** `snapshot` saves every open PR's head commit, changed files, draft flag
   and check status, plus every commit involved, so the build can be re-run later even after
   branches move or are deleted.
2. **Pick the PRs to try.** Drafts, PRs whose required check did not pass, PRs that change no
   tests, and PRs you exclude in the config are skipped, each with its reason.
3. **Merge them one by one.** `build` merges each PR onto the base in order and runs your tests
   after each merge. A PR that conflicts or fails is backed out; a failure that passes on a
   re-run is marked flaky.
4. **Find out why.** For a conflicting PR, the PRs it conflicts with are swapped out and the
   tests run again. For a failing PR, the merge sequence is bisected for the PR that breaks it,
   and that pair is tested alone on the base.
5. **Write the report.** `report` turns the results into Markdown: what merged, what conflicted
   or failed and the culprit where one was found, the test runs each phase used, and how many
   runs landing the PRs one at a time would have needed.

The tests of every PR run as you. **Run builds on a disposable machine or container that holds
no credentials.** See [Security](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/security.md).

## Commands

```
pr-merge-audit --config CONFIG snapshot                        freeze PR heads and metadata (GitHub)
pr-merge-audit --config CONFIG build --snapshot FILE           merge-test offline, write results
      [--emit-branch REF --emit-repo PATH]                     also keep the merged build as a branch
      [--ignore-status] [--out FILE]
pr-merge-audit report --results FILE [--out FILE]              render the Markdown report
pr-merge-audit verify --results FILE [--golden FILE]           check the report renders the same
pr-merge-audit --config CONFIG estimate --snapshot FILE --reference FILE   rough running time here
```

See [Commands](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/commands.md) for what each one does.

## Quick start

```sh
pip install pr-merge-audit
mkdir myproject && cd myproject
curl -so config.json https://raw.githubusercontent.com/jacob-adm/pr-merge-audit/main/config.example.json
# edit config.json: repo, remote, base and test_cmd (see Configuration)
pr-merge-audit --config config.json snapshot
pr-merge-audit --config config.json build --snapshot snapshots/DATE.json
pr-merge-audit report --results results/DATE.json --out reports/DATE.md
```

`DATE` is the UTC date of the snapshot. Paths are relative to the current directory, so keep
each project's config, snapshots and results in a directory of its own.

## Documentation

- [How it works](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/how-it-works.md): each step of the build, and what makes it deterministic.
- [Commands](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/commands.md): each command, snapshots from other hosts, and keeping
  the merged build as a branch.
- [Configuration](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/configuration.md): every config key, and probes.
- [Timings](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/timings.md): what a build costs in test runs, estimating it, the
  serial-landing comparison and a measured build.
- [Security](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/security.md): what runs as you, and what the tool does to limit
  damage.
- [Testing](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/testing.md): the test suite and the golden-report self-check.
- [Contributing](https://github.com/jacob-adm/pr-merge-audit/blob/main/CONTRIBUTING.md): how the project is developed and what is enforced.
- [Reporting a vulnerability](https://github.com/jacob-adm/pr-merge-audit/blob/main/SECURITY.md)

## License

Apache License 2.0; see [LICENSE](https://github.com/jacob-adm/pr-merge-audit/blob/main/LICENSE).
