# Using it with other tools

pr-merge-audit finds breakage between pull requests by running the tests after each merge. This page
shows the tested stack it is part of: landing a batch.

The commands on this page use [git-overlap](https://github.com/jacob-adm/git-overlap),
[pr-merge-audit](https://github.com/jacob-adm/pr-merge-audit) and
[git-surface](https://github.com/jacob-adm/git-surface). git-surface is not on PyPI yet and installs
from GitHub:

```sh
pip install git-overlap
pip install pr-merge-audit
pip install git+https://github.com/jacob-adm/git-surface
```

The examples come from a test repository whose `main` has a small Python module, its tests, a
`requirements.txt`, a changelog and a Markdown guide, and six branches as an agent might push
them: `agent/a` renames a function, `agent/b` changes the same line, `agent/c` adds a module that
calls the function by its old name, `agent/ci` adds a workflow that passes the pull request title
to a step holding a secret, `agent/deps` bumps a pinned version in `requirements.txt`, and
`agent/docs` adds an HTML comment to the guide. Every command was run on that repository and the
output shown is what it printed.

**Exit status.** `git overlap`, `git pairmerge`, `git surface`, `trust-path` and
`release-drift diff` and `source` exit like `grep`: 0 nothing found, 1 found, 2 error.
`git footprint` and `release-drift fetch` exit 0 on success. With `set -o pipefail`, a pipeline
in which any step found something exits 1: treat 1 as a finding, not a failure, and only 2 as an
error. In a `for` or `while` loop the status is that of the last command only, so read the
output.

## Landing a batch: git-overlap, then pr-merge-audit, then git surface

**1. git-overlap first.** Command 3 above, on the pull request branches, finds conflicting pairs
and branches stale against `main` in seconds, without running tests.

**2. pr-merge-audit next.** It merges the pull requests onto the base one after another, runs the
test suite after each merge, and keeps the batch that merged and passed as a branch. It needs a
configuration with the test command and a snapshot (`snapshots/DATE.json` and
`snapshots/DATE.bundle`), which `snapshot` writes for GitHub and which can be written by hand for
other hosts; see its [Commands](https://github.com/jacob-adm/pr-merge-audit/blob/main/docs/commands.md) page.

```sh
pr-merge-audit --config PROJECT/config.json build --snapshot snapshots/DATE.json \
    --emit-branch refs/heads/integration --emit-repo PATH/TO/CLONE
```

With the six branches as pull requests 1 to 6 (`agent/a`, `agent/b`, `agent/c`, `agent/ci`,
`agent/deps`, `agent/docs`), the report (`pr-merge-audit report --results results/DATE.json`)
says, in part:

```text
### Result: 4 of 6 PRs merged together with every test passing

#1, #4, #5, #6

### Text conflicts; tests pass once swapped in (1)
...
| #2 | #1 |

### No text conflict, but a pair fails its tests (1)
...
| #1 + #3 | fail | `test_welcome` |
```

Pull request 3 (`agent/c`) has no text conflict, so git-overlap does not report it; its test fails
once pull request 1 is merged. `build` exits 0 when it ran, whatever it found: the findings are in
the report, and the branch's contents in the `emitted` key of the results file.

**3. git surface on the branch pr-merge-audit kept.** It shows what the combined batch changes in
what runs:

```sh
cd PATH/TO/CLONE
echo integration | git surface main
```

```text
integration	ci	A	.github/workflows/triage.yml
integration	deps	M git-overlap 0.3.1->0.3.2	requirements.txt
integration	hidden	<!-- line 4	docs/guide.md
```

**What it catches:** conflicts and stale branches (git-overlap), test breakage between pull
requests that merge cleanly (pr-merge-audit), and what the batch changes in CI, dependencies,
build files, hooks and hidden content (git surface). **What it does not catch:** breakage the test
suite does not test for, and whether a change in what runs is safe: git surface reports the
change, not a verdict.

### Reading a pr-merge-audit snapshot with git-overlap

A snapshot bundle holds the base as `refs/audit/base` and each pull request head as
`refs/audit/pr/N`. Fetch those refs into a clone of the audited repository and run git-overlap
from inside that clone:

```sh
git fetch --quiet PATH/TO/snapshots/DATE.bundle 'refs/audit/*:refs/audit/*'
git for-each-ref --format='%(refname)' refs/audit/pr/ | git footprint refs/audit/base | git overlap | git pairmerge --onto refs/audit/base
```

```text
refs/audit/pr/1	refs/audit/pr/2	conflict	CHANGELOG.md
refs/audit/pr/1	refs/audit/pr/2	conflict	app.py
refs/audit/pr/1	refs/audit/pr/2	conflict	tests/test_app.py
```
