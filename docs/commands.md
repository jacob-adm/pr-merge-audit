# Commands

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

`pr-merge-audit`, installed by pip, takes the same arguments as `python3 pr_merge_audit.py`.

Paths are relative to the current directory, so run it from wherever you keep that project's
snapshots and results. `report` and `verify` need no configuration. The configuration file is
described in [Configuration](configuration.md).

## snapshot

`snapshot` writes `snapshots/DATE.json` (PR numbers, titles, head commits, changed files,
draft flag, required-status state) and `snapshots/DATE.bundle` (every commit involved), so a
build can be re-run later even after PR branches move or are deleted.

It is the only GitHub-specific step and the only one that uses the network. It needs GitHub
(github.com, or GitHub Enterprise Server with `api` set to `https://HOST/api/v3`). Optional:
`GITHUB_TOKEN` in the environment (unauthenticated API access is rate-limited).

`snapshot` has run against github.com. It has not run against a live Enterprise Server: the three
endpoints it calls, with every query parameter and response field it uses, were checked against
GitHub's published REST API descriptions for Enterprise Server 3.19 to 3.22. They are listing open
pull requests, the combined commit status and the check runs for a commit. PR heads are fetched
from `refs/pull/N/head` with git, which Enterprise Server also provides.

### Private repositories

`GITHUB_TOKEN` is used only for the API. The PR heads are fetched from `remote` with git, which runs
without your credentials or git configuration, so `remote` must be readable without them. For a
private repository, fetch the pull request heads into a local mirror with your own credentials and
point `remote` at it:

```
git clone --bare https://github.com/OWNER/NAME.git mirror.git
git -C mirror.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
```

Then set `"remote": "/path/to/mirror.git"` and run `snapshot` with `GITHUB_TOKEN` set. Run the same
`fetch` again before each snapshot so the mirror has the current PR heads.

### Other hosts

For another host (GitLab, Gitea, Bitbucket), write the two snapshot files yourself and run
`build` on them:

- `snapshots/DATE.json`: `{"date", "repo", "base_ref", "base_sha", "prs": [{"number", "title",
  "head", "draft", "status", "files"}]}`, where `status` is `SUCCESS` when the PR passes your
  required check and `files` lists the paths it changes against the base.
- `snapshots/DATE.bundle`: a `git bundle` holding `refs/audit/base` and `refs/audit/pr/N` for
  each PR.

## build

`build` runs the procedure described in [How it works](how-it-works.md) on a snapshot, offline,
and writes `results/DATE.json` (or `--out`). `--ignore-status` tries PRs whatever their required
status check says; the report shows that it was used.

`--merge-only` runs no tests, setup command or probes, so no code from any PR is executed. A PR
counts as merged when `git merge` succeeds. It needs no `test_cmd`, tries PRs that change no
tests, and skips the checks for submodules and Git LFS, which only matter to test runs. The
report says at the top that no tests were run, lists the PRs that conflict with the base and the
text conflicts between PRs, and leaves out the test-run counts. It cannot be combined with
`--emit-branch`, which needs a tested build.

`--repeat` builds the snapshot twice and compares the two reports. The first build runs in
`WORK-repeat` and keeps its results next to the main ones as `NAME.repeat.json`; no branch is
emitted from it. When the reports match, the results file gains `"reproduced": true`, the report
says so under Procedure, and the command prints `reproduced`. When they differ, the results file
gains `"reproduced": false`, the report says so, the command prints the diff between the two
reports, creates no branch for `--emit-branch`, and exits with status 1. A build with `--repeat`
takes twice as long.

`build` refuses a snapshot whose `base_sha` or a PR's `head` is not a full commit id, that lists a
PR number twice, or whose ids differ from the bundle's refs, before any test runs.

### Keeping the merged build as a branch

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
  branch and the build exits with an error. The write never follows a symbolic ref, so no other
  branch is created: a symbolic ref to an existing branch makes it fail, and so does one that
  points nowhere, created in the moment between the last check and the write, with recent git
  (2.56 refuses it; 2.50 replaces it with `REF` itself).
- **Manifest.** The results file gains an `emitted` key: the repository's git directory, the
  ref, the commit, the PRs included (in merge order) and every PR excluded with its reason
  (`conflict`, `fail` or `flaky` for PRs that were tried, `not tried: ...` for the rest). The
  report does not read it, so it is byte-identical to that of a build without the flag.
- **Reproducible.** Merge commits use a fixed identity and timestamp, so the same snapshot,
  configuration and test outcomes give the same commit id.

Swap tests, culprit searches and probes do not change the branch. A PR that conflicts and
passes once swapped in is excluded: it conflicts with a PR on the branch, so it cannot join as it is.
If no PR merged, the branch is the base itself.

## report

`report` renders a results file as a Markdown report, to `--out` or to standard output.

## verify

`verify` renders the report twice and checks that both renders are identical; with `--golden`,
it also compares them with that file byte for byte. See [Testing](testing.md#self-check).

## estimate

`estimate` times one test-suite run on the base on this machine and scales it by the run counts
of a reference build of the same project. See [Timings](timings.md).
