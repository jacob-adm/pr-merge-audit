# Configuration

The tool is not tied to any project, language or test runner. Everything about the repository
under test (where it lives, how its tests run, how their output reads) is in a configuration
file kept with that project, not here. `config.example.json` is a starting point.

| Key | Meaning |
|---|---|
| `repo`, `remote`, `api`, `base` | Repository, fetch URL, API root, base branch |
| `required_status` | Status or check name a PR must pass to be tried (empty: none) |
| `test_cmd` | Required, except for `build --merge-only`. Test command as a list of arguments; `{python}` is replaced |
| `test_prefix`, `test_timeout` | Test directory (default `tests/`), seconds per run (default 1800) |
| `failure_pattern` | Regex whose group 1 is a failing test's name in the output (unset: none named) |
| `count_pattern` | Regex whose group 1 is the number of tests run (unset: exit status alone decides) |
| `skipped_pattern`, `message_pattern` | Regexes for the skipped count and the first error message |
| `test_files` | Regex for test files, used to decide whether a PR changes tests (default: anything under `test_prefix`) |
| `python`, `workers` | Interpreter for tests (default: the one running the tool), parallel jobs |
| `pass_env` | Names of variables copied from your environment into test runs, if they are set (e.g. `RUSTUP_HOME`, `CARGO_HOME`, `GOPATH`, `VIRTUAL_ENV`) |
| `env` | Variables set in test runs, applied last (can override `HOME`). Test runs have their own `HOME`, so a toolchain that finds itself under your home directory by default needs its location here, for example rustup and cargo: `{"RUSTUP_HOME": "/Users/me/.rustup", "CARGO_HOME": "/Users/me/.cargo"}` |
| `allow_incomplete_checkout` | Run even though the base uses submodules or Git LFS, which test runs lack (shown in the report) |
| `setup_cmd` | Command run before `test_cmd` in the same copy (e.g. installing dependencies); if it fails, the run fails as `(setup failed)` |
| `order` | Merge order; it decides which side of each conflict merged first |
| `exclude`, `duplicates`, `features` | PR number to reason; these PRs are not tried |
| `no_tests_ok` | PRs tried even though they change no tests |
| `probes` | `{name, file, cmd, targets}`, optional `dest`; see below |

## Probes

A probe's `file` (relative to the configuration file) is written to `dest` in the target, by
default `test_prefix` + `audit_probe_` + its file name, and `cmd` is run there. In `cmd`,
`{python}`, `{dest}` and `{module}` (`dest` without its suffix, with `.` for `/`) are replaced.
Targets are `"base"` or PR numbers. Probe output is read with the same patterns as the tests. `dest`
must be a relative path inside the target: an absolute path, a drive or a `..` part stops the
build before any test runs.
