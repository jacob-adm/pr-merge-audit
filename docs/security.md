# Security

`build` runs the test code of every PR it tries, and also runs each probe on its listed targets
whatever the filters decided about them; `estimate` runs the base's tests. That code runs as you,
with access to everything your user can reach. **Run builds on a disposable machine or
container that holds no credentials, keys or tokens.** Nothing in this tool is a sandbox.

`build --merge-only` is the exception: it runs no tests, setup command or probes and never copies
a PR's files out of git, so no code from any PR is executed. It still runs git on the fetched
commits, with the protections listed below.

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
  On Windows a job object does the same; if Windows refuses to put the run in one, the tool prints
  a warning and stops only the test process itself, so its children may survive the run.
- Probe names and the reasons from `exclude`, `duplicates` and status checks are escaped in the
  report, so they cannot add table cells, list items, links, mentions or HTML.
- The config is checked when it is loaded: commands must be non-empty lists of strings, `workers`
  and `test_timeout` positive numbers, and each output pattern a valid regular expression with a
  capturing group.
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
- Setup, test and probe commands run without network unless the config says `"network":
  "allowed"`: code from a PR cannot connect out or resolve names. Localhost still works. See
  [Configuration](configuration.md) for how, and where it is not possible.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
