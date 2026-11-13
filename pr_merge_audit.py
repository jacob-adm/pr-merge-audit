#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The pr-merge-audit Authors
#
# pr-merge-audit: merge-test every open pull request of a repository together, isolate
# conflicts and failing combinations, and publish a deterministic Markdown report.
# Project home: https://github.com/jacob-adm/pr-merge-audit

"""Merge-test the open pull requests of a repository and report how they combine.

Needs only the Python standard library (3.8+) and git (2.38+, for `git merge-tree --write-tree`).

    pr_merge_audit.py --config config.json snapshot                                # network: freeze PR heads
    pr_merge_audit.py --config config.json build --snapshot snapshots/DATE.json    # offline; runs PR code
        [--emit-branch refs/heads/NAME --emit-repo CLONE]                          # keep the merged build as a branch
    pr_merge_audit.py report --results results/DATE.json [--out report.md]
    pr_merge_audit.py verify --results results/DATE.json --golden report.md

Only `snapshot` uses the network (a GitHub-compatible API). The same snapshot, config and machine
give the same report.
"""

# Standard library only, by design: the tool must install and run anywhere Python 3.8+ does.
import argparse
import concurrent.futures
import datetime
import json
import os
import platform
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath

__version__ = "1.1.0"

# Fixed identity, dates and config for every git call, so merge commits (and therefore
# results) are reproducible and the operator's own git configuration never applies.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "audit",
    "GIT_AUTHOR_EMAIL": "audit@localhost",
    "GIT_COMMITTER_NAME": "audit",
    "GIT_COMMITTER_EMAIL": "audit@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
# Fixed locale, time zone and hash seed for test runs, so output is comparable between runs.
TEST_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
}
# File that identifies a directory as a disposable build workspace; nothing else is ever deleted.
MARKER = ".audit-workspace"
# First git release with `git merge-tree --write-tree`, used for conflict detection.
MIN_GIT = (2, 38)
# Upper bound on captured test output per run, so a noisy suite cannot exhaust memory.
OUTPUT_CAP = 4 * 1024 * 1024
# The only caller environment variables passed to subprocesses: enough to find tools and reach
# the network, nothing that carries credentials.
PASS_ENV = ("PATH", "SYSTEMROOT", "PATHEXT", "COMSPEC", "SYSTEMDRIVE", "WINDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")


def load_config(path):
    """Read and validate the project configuration, then fill in defaults.

    Exits with a message on invalid input, so a misconfiguration fails before any work starts.
    """
    # Parse the JSON configuration file.
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    # The test command is the only mandatory setting.
    if not cfg.get("test_cmd"):
        sys.exit(f"{path}: test_cmd is required")
    # Every probe must say how to run it.
    for probe in cfg.get("probes", []):
        if not probe.get("cmd"):
            sys.exit(f"{path}: probe {probe.get('name')!r} needs a cmd")
    # Defaults: the current interpreter, a tests/ directory, a 30-minute run limit, 4 workers.
    cfg.setdefault("python", sys.executable)
    cfg.setdefault("test_prefix", "tests/")
    cfg.setdefault("test_files", "^" + re.escape(cfg["test_prefix"]))
    cfg.setdefault("test_timeout", 1800)
    cfg.setdefault("workers", 4)
    cfg.setdefault("order", [])
    # JSON object keys are strings; PR numbers are used as integers everywhere else.
    for key in ("exclude", "duplicates", "features"):
        cfg[key] = {int(k): v for k, v in cfg.get(key, {}).items()}
    # PRs allowed to change code without changing tests.
    cfg["no_tests_ok"] = set(cfg.get("no_tests_ok", []))
    # Optional sections default to empty.
    cfg.setdefault("probes", [])
    cfg.setdefault("pass_env", [])
    cfg.setdefault("env", {})
    # Validate the environment settings, since they are passed straight to subprocesses.
    if not (isinstance(cfg["pass_env"], list) and all(isinstance(k, str) for k in cfg["pass_env"])):
        sys.exit(f"{path}: pass_env must be a list of variable names")
    if not (isinstance(cfg["env"], dict) and all(isinstance(v, str) for v in cfg["env"].values())):
        sys.exit(f"{path}: env must map variable names to strings")
    # A setup command, when present, must be an argument list.
    if "setup_cmd" in cfg and not (isinstance(cfg["setup_cmd"], list) and cfg["setup_cmd"]):
        sys.exit(f"{path}: setup_cmd must be a non-empty list of arguments")
    # Probe files are resolved relative to the configuration file.
    cfg["_dir"] = Path(path).resolve().parent
    return cfg


def fill(args, **values):
    """Replace `{name}` placeholders by plain substitution, so other braces in arguments are left alone."""
    # Substitute each placeholder in each argument; Path values use forward slashes everywhere.
    out = []
    for a in args:
        for k, v in values.items():
            a = a.replace("{" + k + "}", v.as_posix() if isinstance(v, Path) else str(v))
        out.append(a)
    return out


def base_env():
    """Only the variables a subprocess needs, so the caller's GIT_*, tokens and the like stay out."""
    return {k: os.environ[k] for k in PASS_ENV if k in os.environ}


def windows_job(proc):
    """Put `proc` in a Windows job object, so its whole process tree can be stopped later,
    including children whose parent has already exited. Returns None if that is not possible."""
    import ctypes
    from ctypes import wintypes
    # Declare the Win32 signatures used, so handles are passed at full width on 64-bit Windows.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    # Create an anonymous job and attach the test process to it.
    job = k32.CreateJobObjectW(None, None)
    if job and k32.AssignProcessToJobObject(job, int(proc._handle)):
        return job
    # Assignment failed: release the job and fall back to stopping the single process.
    if job:
        k32.CloseHandle(wintypes.HANDLE(job))
    return None


def stop_windows_job(job):
    """Terminate every process in a Windows job object and release its handle."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    # Stop every process in the job, then release it.
    k32.TerminateJobObject(job, 1)
    k32.CloseHandle(job)


def write_text(path, text):
    """Write UTF-8 with LF line ends on every platform, so results and reports are identical."""
    # Explicit encoding and newline override the platform defaults.
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def remove_tree(path, strict=False):
    """Delete a directory tree. Windows refuses to delete read-only files, which git creates
    for its objects, so those are made writable and tried again."""
    def retry(func, p, _exc):
        """Clear the read-only flag on the failing path and repeat the failed operation."""
        os.chmod(p, stat.S_IWRITE)
        func(p)
    # Python 3.12 renamed the callback; keep both versions supported.
    hook = {"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}
    # Delete the tree; a remaining failure is ignored unless the caller requires success.
    try:
        shutil.rmtree(path, **hook)
    except OSError:
        if strict:
            raise


def git(repo, *args, check=True, binary=False, data=None, env=None):
    """Run git in `repo` with the fixed environment; raise on failure unless `check` is False.

    Hooks and filesystem monitors are disabled, so repository content cannot run code here.
    `data` is sent to standard input; `env` replaces the fixed environment for that one call.
    """
    # Minimal environment plus the fixed git identity, unless the caller supplies one.
    env = env or dict(base_env(), **GIT_ENV)
    # gc.auto=0: parallel jobs merge in worktrees of one repository; a background gc must not race them.
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "gc.auto=0", *args]
    text = {} if binary else {"encoding": "utf-8", "errors": "replace"}  # not the locale's code page
    # Capture output; raise with git's own message when the command fails and `check` is set.
    r = subprocess.run(argv, cwd=repo, env=env, capture_output=True, input=data, **text)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def git_version(text):
    """(major, minor) from `git --version` output, (0, 0) if it has none."""
    # Extract the first major.minor pair, e.g. 2.50 from `git version 2.50.1`.
    m = re.search(r"(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def check_git():
    """Exit early with a clear message if git is missing or older than MIN_GIT."""
    try:
        # Query the installed git; a missing executable counts as no git.
        found = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    except OSError:
        found = ""
    # Compare against the minimum supported release.
    if git_version(found) < MIN_GIT:
        sys.exit(f"git {MIN_GIT[0]}.{MIN_GIT[1]} or later is needed (for `git merge-tree --write-tree`); "
                 f"found: {found or 'no git on PATH'}")


def check_probes(cfg, snap):
    """Stop before any test runs if a probe names a missing file or a target not in the snapshot."""
    # PR numbers in the snapshot, the only valid PR targets.
    numbers = {p["number"] for p in snap["prs"]}
    for probe in cfg["probes"]:
        # The probe file must exist next to the configuration.
        if not (cfg["_dir"] / probe["file"]).is_file():
            sys.exit(f"probe {probe['name']!r}: file {probe['file']} not found next to the config")
        if not probe.get("targets"):
            sys.exit(f"probe {probe['name']!r}: no targets; list \"base\" and/or PR numbers")
        # Each target must be the base or a PR in this snapshot.
        for target in probe["targets"]:
            if target != "base" and target not in numbers:
                sys.exit(f"probe {probe['name']!r}: target {target!r} is neither \"base\" nor an open PR in the snapshot")


# ---------------------------------------------------------------- snapshot


def api(cfg, path):
    """GET a GitHub-compatible REST endpoint and return the decoded JSON.

    HTTP and network errors end the run with an actionable message, including rate-limit reset times.
    """
    # Build the request against the configured API root, asking for the GitHub JSON media type.
    req = urllib.request.Request(cfg["api"].rstrip("/") + path)
    req.add_header("Accept", "application/vnd.github+json")
    # Authenticate when a token is available; otherwise use anonymous, rate-limited access.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        # Unredirected, so the token is never sent on to another host.
        req.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    # HTTP error: explain rate limiting when that is the cause, then stop.
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
            hint = "; API rate limit reached"
            reset = exc.headers.get("X-RateLimit-Reset", "")
            if reset.isdigit():
                at = datetime.datetime.fromtimestamp(int(reset), datetime.timezone.utc)
                hint += f" (resets {at:%Y-%m-%d %H:%M} UTC)"
            if not token:
                hint += "; set GITHUB_TOKEN for a higher limit"
        sys.exit(f"{req.full_url}: HTTP {exc.code} {exc.reason}{hint}")
    # Network error (DNS, TLS, connection): stop with the reason.
    except urllib.error.URLError as exc:
        sys.exit(f"{req.full_url}: {exc.reason}")


def required_status(cfg, sha):
    """State of the configured required check on `sha`: SUCCESS, FAILURE, PENDING, MISSING, etc.

    Looks at commit statuses first, then at check runs, since projects use either mechanism.
    """
    # No required check configured: every PR counts as passing.
    name = cfg.get("required_status")
    if not name:
        return "SUCCESS"
    # Commit statuses (the older API, used by many CLA and CI bots).
    combined = api(cfg, f"/repos/{cfg['repo']}/commits/{sha}/status?per_page=100")
    for s in combined.get("statuses", []):
        if s["context"] == name:
            return s["state"].upper()
    # Check runs (the newer API, used by GitHub Actions).
    runs = api(cfg, f"/repos/{cfg['repo']}/commits/{sha}/check-runs?check_name={urllib.parse.quote(name, safe='')}")
    # The API returns the most recent run first.
    for run in runs.get("check_runs", []):
        return (run.get("conclusion") or run.get("status") or "").upper()
    # Neither API reports the check on this commit.
    return "MISSING"


def cmd_snapshot(cfg, args):
    """`snapshot` command: freeze the open PRs against the base branch.

    Writes DATE.json (PR metadata) and DATE.bundle (every commit involved), so the build can be
    re-run offline and reproduced later, even after branches move or are deleted.
    """
    check_git()
    # The snapshot is named after its UTC date unless one is given.
    date = args.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pulls, page = [], 1
    # Page through all open PRs; keep only those that target the configured base branch.
    while True:
        batch = api(cfg, f"/repos/{cfg['repo']}/pulls?state=open&per_page=100&page={page}")
        pulls += [p for p in batch if p["base"]["ref"] == cfg["base"]]
        if len(batch) < 100:
            break
        page += 1
    # A persistent bare cache makes repeated snapshots fetch only new objects.
    cache = Path(args.work) / "cache.git"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        git(".", "init", "-q", "--bare", str(cache))
    # Drop refs from earlier snapshots, so closed PRs do not leak into this bundle.
    stale = git(cache, "for-each-ref", "--format=delete %(refname)", "refs/audit").stdout
    subprocess.run(["git", "update-ref", "--stdin"], cwd=cache, input=stale.encode(), check=True,
                   env=dict(base_env(), **GIT_ENV))
    # Fetch the base branch and every PR head into a private ref namespace.
    specs = [f"+refs/heads/{cfg['base']}:refs/audit/base"]
    specs += [f"+refs/pull/{p['number']}/head:refs/audit/pr/{p['number']}" for p in pulls]
    git(cache, "fetch", "-q", "--no-tags", cfg["remote"], *specs)
    # Record the exact base commit the PRs are measured against.
    base_sha = git(cache, "rev-parse", "refs/audit/base").stdout.strip()
    prs = []
    # One record per PR: its head commit, draft flag, required-check state and changed files.
    for p in sorted(pulls, key=lambda p: p["number"]):
        n = p["number"]
        head = git(cache, "rev-parse", f"refs/audit/pr/{n}").stdout.strip()
        files = names(git(cache, "diff", "--name-only", "-z", f"{base_sha}...{head}").stdout)
        prs.append({
            "number": n,
            "title": p["title"],
            "head": head,
            "draft": bool(p.get("draft")),
            "status": required_status(cfg, head),
            "files": sorted(files),
        })
    # Write the metadata and a bundle of every commit involved, then summarise.
    snap = {"date": date, "repo": cfg["repo"], "base_ref": cfg["base"], "base_sha": base_sha, "prs": prs}
    write_text(out / f"{date}.json", json.dumps(snap, indent=1, sort_keys=True) + "\n")
    git(cache, "bundle", "create", "-q", str((out / f"{date}.bundle").resolve()), "--glob=refs/audit")
    print(f"{out / date}.json: {len(prs)} open PRs against {cfg['base']} {base_sha[:7]}")


# ---------------------------------------------------------------- build


def names(z):
    """Split NUL-separated git output (from `-z`) into a list of paths."""
    return [x for x in z.split("\0") if x]


def absolute(name):
    """Whether an archive path is absolute on any platform: `/x`, `\\x`, `C:x` and `\\\\host\\x` all are.
    (`os.path.isabs` alone is not enough: on Windows with Python 3.13 it says no to `/x`.)"""
    return name.startswith(("/", "\\")) or bool(PureWindowsPath(name).drive) or os.path.isabs(name)


def safe_member(root, m):
    """Whether a tar member from `git archive` can be written under `root` without escaping it."""
    # Refuse absolute paths, parent-directory steps and special files (devices, FIFOs, hard links).
    parts = Path(m.name).parts
    if absolute(m.name) or ".." in parts or not (m.isfile() or m.isdir() or m.issym()):
        return False
    # A symlink may only point inside the export directory.
    if m.issym():
        target = os.path.normpath(os.path.join(os.path.dirname(m.name), m.linkname))
        if absolute(m.linkname) or target == ".." or target.startswith(".." + os.sep):
            return False
    # No directory on the way to the member may be a symlink.
    parent = root
    for part in parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            return False
    # Never write over an entry already there: on a case-insensitive disk, `t` after a link `T`
    # would otherwise be written through the link.
    dest = root / m.name
    if os.path.lexists(dest) and not (m.isdir() and dest.is_dir() and not dest.is_symlink()):
        return False
    return True


def prune_links(root):
    """Remove links that resolve outside `root`; the text check alone misses chains through other links."""
    # Resolve every link fully and delete those that end up outside the export.
    top = os.path.realpath(root)
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(d, name)
            if os.path.islink(p):
                real = os.path.realpath(p)
                if real != top and not real.startswith(top + os.sep):
                    os.unlink(p)


def scan(cfg, rc, out):
    """Outcome of a test run from its exit status and the configured output patterns.

    Patterns are optional; without them only the exit status counts. With `count_pattern` set,
    exit status 0 with no tests counted (an early exit, an empty run) is not a pass.
    """
    def first(key):
        """First capture group of the configured pattern `key`, or an empty string."""
        m = re.search(cfg[key], out, re.M) if cfg.get(key) else None
        return m.group(1).strip() if m else ""

    def number(key):
        """First integer in the capture of pattern `key`, or 0."""
        m = re.search(r"\d+", first(key))
        return int(m.group()) if m else 0

    # A run passes on exit status 0, and, when a count pattern is set, only if tests actually ran.
    ran = number("count_pattern")
    res = {"outcome": "pass" if rc == 0 and (ran or not cfg.get("count_pattern")) else "fail"}
    # Non-zero exit: collect the failing test names, or fall back to the exit status.
    if rc:
        found = [m.group(1) for m in re.finditer(cfg["failure_pattern"], out, re.M)] if cfg.get("failure_pattern") else []
        res["failures"] = sorted(set(found)) or [f"(exit status {rc})"]
        res["message"] = first("message_pattern")
    # Exit status 0 but no tests counted: an early exit or an empty run.
    elif res["outcome"] == "fail":
        res.update(failures=["(no tests ran)"], message="")
    # Record counts for the report.
    res["ran"] = ran
    res["skipped"] = number("skipped_pattern")
    return res


def setup_failure(rc, out):
    """Result for a run whose setup_cmd failed; the tests never ran."""
    # The last non-empty output line usually states why setup failed.
    lines = [x for x in out.splitlines() if x.strip()]
    detail = "timeout" if rc is None else f"exit status {rc}"
    return {"outcome": "fail", "failures": ["(setup failed)"],
            "message": f"setup_cmd {detail}" + (f": {lines[-1].strip()}" if lines else "")}


def read_capped(path):
    """Test output from `path`, keeping the head and tail when it exceeds OUTPUT_CAP."""
    # Small output is read whole; large output keeps the first and last halves of the cap.
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size <= OUTPUT_CAP:
            data = f.read()
        else:
            data = f.read(OUTPUT_CAP // 2)
            f.seek(size - OUTPUT_CAP // 2)
            data += b"\n[output truncated]\n" + f.read()
    return data.decode("utf-8", "replace")


def classify(cfg, snap):
    """Split the snapshot into PRs to merge (in order) and PRs not tried, with the reason."""
    # Apply the rules in priority order; the first that matches decides why a PR is not tried.
    eligible, not_tried = [], []
    for p in snap["prs"]:
        n = p["number"]
        # A PR counts as tested if it changes a file that matches the test-file pattern.
        has_tests = any(re.search(cfg["test_files"], f) for f in p["files"])
        if n in cfg["exclude"]:
            reason, detail = "excluded", cfg["exclude"][n]
        elif cfg.get("required_status") and p["status"] != "SUCCESS":
            reason, detail = "status", p["status"].lower()
        elif p["draft"]:
            reason, detail = "draft", ""
        elif n in cfg["duplicates"]:
            reason, detail = "duplicate", cfg["duplicates"][n]
        elif n in cfg["features"]:
            reason, detail = "feature", cfg["features"][n]
        elif not has_tests and n not in cfg["no_tests_ok"]:
            reason, detail = "no_tests", ""
        # No rule matched: the PR is eligible.
        else:
            eligible.append(n)
            continue
        # Keep the reason and detail for the report's Not tried section.
        not_tried.append({"pr": n, "reason": reason, "detail": detail})
    # Configured order first; every other PR follows in PR-number order.
    rank = {n: i for i, n in enumerate(cfg["order"])}
    eligible.sort(key=lambda n: (rank.get(n, len(rank)), n))
    return eligible, not_tried


class Workspace:
    """A private git repository holding the snapshot, plus helpers to merge, export and test.

    Shared by the worker threads of the follow-up phase; `lock` serialises worktree changes and
    run counting, and `local` records which phase each thread is working in.
    """
    def __init__(self, cfg, snap, bundle, work):
        """Create a fresh workspace under `work` and load the snapshot bundle into it."""
        # Paths, synchronisation primitives and per-phase counters.
        self.cfg, self.snap = cfg, snap
        self.work = Path(work).resolve()
        self.repo = self.work / "repo"
        self.lock = threading.Lock()
        self.local = threading.local()
        self.runs = {"sequence": 0, "swap": 0, "culprit": 0, "probe": 0}
        self.seconds = {k: [] for k in self.runs}  # duration of each test-suite run (setup included)
        # Clear a previous build, but only if it carries the workspace marker.
        if self.work.exists() and any(self.work.iterdir()):
            if not (self.work / MARKER).is_file():
                sys.exit(f"refusing to clear {self.work}: no {MARKER} marker, so not a build directory")
            remove_tree(self.work, strict=True)
        # Create the marker first, so a crashed build can still be cleared next time.
        self.repo.mkdir(parents=True)
        write_text(self.work / MARKER, "created by pr_merge_audit.py; deleted and recreated on each build\n")
        # A private repository, so PR code never touches the operator's own clones.
        git(self.repo, "init", "-q")
        # Overrides each commit's own .gitattributes, so a PR cannot drop files from the test copy.
        info = self.repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        write_text(info / "attributes", "* -export-ignore -export-subst\n")
        # Load the frozen snapshot; no network access is needed from here on.
        git(self.repo, "fetch", "-q", str(Path(bundle).resolve()), "+refs/audit/*:refs/audit/*")
        # Commit ids for the base and each PR head.
        self.base = snap["base_sha"]
        self.heads = {p["number"]: p["head"] for p in snap["prs"]}

    def ref(self, n):
        """Commit for a target: the base, or the head of PR `n`."""
        return self.base if n == "base" else self.heads[n]

    def tree(self, name, commit):
        """Add a detached worktree at `commit` for one job; worktree changes are serialised."""
        path = self.work / "wt" / name
        with self.lock:
            git(self.repo, "worktree", "add", "-q", "--detach", str(path), commit)
        return path

    def drop(self, path):
        """Remove a job's worktree."""
        with self.lock:
            git(self.repo, "worktree", "remove", "--force", str(path), check=False)

    def merge(self, wt, n):
        """Merge PR `n` into worktree `wt`.

        Returns None on success; on a conflict, aborts the merge and returns the conflicting paths.
        """
        # Always create a merge commit, so each PR can be reverted as one unit later.
        r = git(wt, "merge", "-q", "--no-ff", "--no-edit", "-m", f"merge #{n}", self.heads[n], check=False)
        # Merge failed: list the conflicted paths, then restore the worktree.
        if r.returncode:
            files = names(git(wt, "diff", "--name-only", "-z", "--diff-filter=U").stdout)
            git(wt, "merge", "--abort", check=False)
            return sorted(files) or ["(merge failed)"]
        return None

    def conflicts(self, a, b):
        """Whether targets `a` and `b` have a text conflict, computed in memory without a worktree."""
        # Exit 0 means a clean merge, exit 1 means conflicts.
        r = git(self.repo, "merge-tree", "--write-tree", "--name-only", self.ref(a), self.ref(b), check=False)
        # A result (clean or conflicted) starts with the merged tree's id; anything else is an error,
        # including exit 1 for an unmergeable ref and 129 from a git without --write-tree.
        if r.returncode not in (0, 1) or not re.match(r"[0-9a-f]{40,64}$", r.stdout.split("\n", 1)[0]):
            raise RuntimeError(f"git merge-tree {a} {b} failed: {r.stderr.strip()}")
        # True when the two targets conflict.
        return r.returncode == 1

    def incomplete_checkout(self, commit):
        """What test runs of `commit` would lack: `git archive` leaves out submodules, and LFS files stay pointers."""
        missing = []
        # List every entry of the commit's tree.
        rows = [r for r in git(self.repo, "ls-tree", "-r", "-z", commit).stdout.split("\0") if r]
        # Mode 160000 marks a submodule (gitlink) entry.
        if any(r.startswith("160000 ") for r in rows):
            missing.append("submodules")
        # Any .gitattributes that routes files through the LFS filter means LFS content.
        for r in rows:
            meta, _, path = r.partition("\t")
            if path.rsplit("/", 1)[-1] == ".gitattributes" and meta.split()[1] == "blob":
                if re.search(r"\bfilter=lfs\b", git(self.repo, "cat-file", "blob", meta.split()[2]).stdout):
                    missing.append("Git LFS files")
                    break
        return missing

    def phase(self, name):
        """Record the current thread's phase, for per-phase run counts and timings."""
        self.local.phase = name

    def count(self):
        """Count one test-suite run against the current thread's phase."""
        with self.lock:
            self.runs[getattr(self.local, "phase", "sequence")] += 1

    def export(self, commit):
        """Copy of `commit`'s files, without .git, in a fresh temporary directory.

        PR code runs only here, so it cannot reach the shared repository's config, hooks or refs
        through the working tree.
        """
        # Fresh scratch area: src (the files), home and tmp (private to this run).
        scratch = Path(tempfile.mkdtemp(prefix="audit-run-"))
        root = scratch / "src"
        for d in (root, scratch / "home", scratch / "tmp"):
            d.mkdir()
        try:
            # Stream `git archive` and extract member by member, refusing anything that could escape `root`.
            proc = subprocess.Popen(["git", "-c", "core.hooksPath=" + os.devnull, "archive", "--format=tar", commit],
                                    cwd=self.repo, env=dict(base_env(), **GIT_ENV), stdout=subprocess.PIPE)
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for m in tar:
                    if safe_member(root, m):
                        kw = {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
                        tar.extract(m, root, **kw)
            # A failed archive is an error, not a partial copy.
            proc.stdout.close()
            if proc.wait():
                raise RuntimeError(f"git archive {commit} failed")
            # Second pass: remove symlinks that escape `root` through chains of other links.
            prune_links(root)
        # Never leave a half-built export behind, even on interrupt.
        except BaseException:
            remove_tree(scratch)
            raise
        return scratch, root

    def run_in(self, scratch, root, argv):
        """Run `argv` in `root` with a private HOME and TMPDIR; output is kept on disk and capped.

        Variables named in `pass_env` are copied from the caller and `env` is applied last, so a
        toolchain that lives outside PATH (rustup, Go, a virtualenv) can still be reached.
        """
        # Build the run environment: minimal caller variables, fixed test settings, opted-in variables,
        # private home and temp directories, then explicit overrides from the configuration.
        env = dict(base_env(), **TEST_ENV)
        env.update({k: os.environ[k] for k in self.cfg.get("pass_env", []) if k in os.environ})
        env.update(HOME=str(scratch / "home"), TMPDIR=str(scratch / "tmp"))
        if os.name == "nt":  # where Windows programs look for the home and temp directories
            home, tmp = str(scratch / "home"), str(scratch / "tmp")
            env.update(USERPROFILE=home, APPDATA=home, LOCALAPPDATA=home, TEMP=tmp, TMP=tmp)
        env.update(self.cfg.get("env", {}))
        # Output goes to a file, not a pipe, so a chatty suite cannot block on a full buffer.
        log = scratch / "output.log"
        # Each run gets its own process group (POSIX) or job object (Windows), so the whole process
        # tree can be stopped when the run ends or times out.
        posix = hasattr(os, "killpg")
        with open(log, "wb") as f:
            proc = subprocess.Popen(argv, cwd=root, env=env, stdout=f, stderr=subprocess.STDOUT,
                                    start_new_session=posix)
            job = None if posix else windows_job(proc)
            # Wait for the run, bounded by the configured timeout; None marks a timeout.
            try:
                rc = proc.wait(timeout=self.cfg["test_timeout"])
            except subprocess.TimeoutExpired:
                rc = None
            finally:
                # Also stop whatever the tests left running in the background.
                if posix:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                elif job:
                    stop_windows_job(job)
                else:
                    proc.kill()
                proc.wait()
        # Exit status (None on timeout) and the captured output.
        return rc, read_capped(log)

    def execute(self, scratch, root, argv):
        """Run `setup_cmd` (if configured), then `argv`, in the same copy.

        Returns (rc, output, setup_failed); a failed setup skips `argv`.
        """
        # Time the whole run, setup included, and file it under the current phase.
        start = time.monotonic()
        try:
            return self._execute(scratch, root, argv)
        finally:
            with self.lock:
                self.seconds[getattr(self.local, "phase", "sequence")].append(time.monotonic() - start)

    def _execute(self, scratch, root, argv):
        """Run `setup_cmd`, then `argv`; stop after a failed setup. See `execute`."""
        # Optional setup step, such as installing dependencies, in the same copy as the tests.
        setup = self.cfg.get("setup_cmd")
        if setup:
            rc, out = self.run_in(scratch, root, fill(setup, python=self.cfg["python"]))
            if rc != 0:
                return rc, out, True
        # Then the test command itself.
        rc, out = self.run_in(scratch, root, argv)
        return rc, out, False

    def test_once(self, wt, argv):
        """One test-suite run of the commit checked out in `wt`, in a fresh isolated copy."""
        # Count the run, export the worktree's commit and run in that copy.
        self.count()
        scratch, root = self.export(git(wt, "rev-parse", "HEAD").stdout.strip())
        try:
            rc, out, setup_failed = self.execute(scratch, root, argv)
        finally:
            remove_tree(scratch)
        # Translate the raw result into an outcome record.
        if setup_failed:
            return setup_failure(rc, out)
        if rc is None:
            return {"outcome": "fail", "failures": ["(timeout)"]}
        return scan(self.cfg, rc, out)

    def test(self, wt, argv=None):
        """Run the tests, re-running once on failure.

        A pass on the re-run is reported as `flaky`; two failures report the first run's result.
        """
        # Default to the configured test command.
        argv = argv or fill(self.cfg["test_cmd"], python=self.cfg["python"])
        first = self.test_once(wt, argv)
        if first["outcome"] == "pass":
            return first
        # Failed once: re-run to tell a flaky suite from a real failure.
        second = self.test_once(wt, argv)
        if second["outcome"] == "pass":
            return dict(second, outcome="flaky")
        return first


def run_sequence(ws, seq):
    """Merge `seq` in order onto the base, keeping only PRs that merge and pass the tests."""
    ws.phase("sequence")
    # Start from the base and test it first; the build only proceeds if the base itself passes.
    wt = ws.tree("sequence", ws.base)
    steps, merged, final = [], [], ws.test(wt)
    base_result = final
    if base_result["outcome"] not in ("pass", "flaky"):
        ws.drop(wt)
        return steps, merged, ws.base, base_result, final
    # Each PR stays merged only if it merges cleanly and the tests pass; otherwise it is held for
    # the follow-up phase and the build continues without it.
    for n in seq:
        # Merge the next PR; a conflict leaves the build unchanged.
        files = ws.merge(wt, n)
        if files:
            steps.append({"pr": n, "outcome": "conflict", "files": files})
            continue
        # Test the build with this PR added.
        res = ws.test(wt)
        # Failing or flaky: drop the merge commit and hold the PR.
        if res["outcome"] != "pass":
            git(wt, "reset", "-q", "--hard", "HEAD~1")
            steps.append(dict(res, pr=n))
            continue
        # Passing: keep the merge and remember its commit for later reverts.
        commit = git(wt, "rev-parse", "HEAD").stdout.strip()
        merged.append({"pr": n, "commit": commit})
        steps.append({"pr": n, "outcome": "merged"})
        final = res
    # The tip is the combined build of every PR that merged.
    tip = git(wt, "rev-parse", "HEAD").stdout.strip()
    ws.drop(wt)
    return steps, merged, tip, base_result, final


def build_from(ws, name, prs):
    """Worktree with `prs` merged onto the base, skipping any that conflict."""
    wt = ws.tree(name, ws.base)
    # Merge the PRs in order; record which ones actually went in.
    used = [n for n in prs if ws.merge(wt, n) is None]
    return wt, used


def find_culprit(ws, n, seq, name):
    """First PR in `seq` whose addition makes PR `n` fail its tests, by bisection."""
    ws.phase("culprit")
    def fails_with(k):
        """Whether PR `n` fails (or conflicts) when merged after the first `k` PRs of `seq`."""
        wt, used = build_from(ws, f"{name}-{k}", seq[:k])
        try:
            if ws.merge(wt, n):
                return "conflict"
            return ws.test(wt)["outcome"] not in ("pass", "flaky")
        finally:
            ws.drop(wt)

    # First, PR `n` on the base alone: if it already fails or conflicts there, no other PR is to blame.
    on_base = fails_with(0)
    if on_base == "conflict":
        return {"on_base": "conflict"}
    if on_base:
        return {"on_base": "fail"}
    # Then with every earlier PR: if it passes there, the failure needs the later state of the build.
    lo, hi = 0, len(seq)
    if not fails_with(hi):
        return {"on_base": "pass", "culprit": None}
    # Bisect for the smallest prefix of `seq` with which PR `n` fails.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fails_with(mid):
            hi = mid
        else:
            lo = mid
    # Confirm the finding: merge the culprit and PR `n` onto the base alone and test the pair.
    culprit = seq[hi - 1]
    wt, _ = build_from(ws, f"{name}-pair", [culprit])
    try:
        pair = {"outcome": "conflict"} if ws.merge(wt, n) else ws.test(wt)
    finally:
        ws.drop(wt)
    return {"on_base": "pass", "culprit": culprit, "pair": pair}


def swap(ws, n, merged, tip):
    """Revert the merged PRs that conflict with `n`, merge `n` and run the tests."""
    ws.phase("swap")
    # Blockers are the merged PRs that conflict with PR `n` on their own.
    blockers = [m["pr"] for m in merged if ws.conflicts(n, m["pr"])]
    # Work on a copy of the combined build.
    res = {"blockers": blockers}
    wt = ws.tree(f"swap-{n}", tip)
    try:
        # Revert blockers newest first, then merge PR `n` in their place.
        for m in reversed(merged):
            if m["pr"] in blockers and git(wt, "revert", "--no-edit", "-m", "1", m["commit"], check=False).returncode:
                git(wt, "revert", "--abort", check=False)
                return dict(res, outcome="revert_conflict")
        # The swap itself conflicts: report it as such.
        if ws.merge(wt, n):
            return dict(res, outcome="conflict")
        # Test the build with PR `n` in place of its blockers.
        res.update(ws.test(wt))
    finally:
        ws.drop(wt)
    # Tests still fail after the swap: look for the merged PR that breaks PR `n`.
    if res["outcome"] not in ("pass", "flaky"):
        # Search only among merged PRs that were not reverted.
        rest = [m["pr"] for m in merged if m["pr"] not in blockers]
        try:
            res["culprit"] = find_culprit(ws, n, rest, f"cul-swap-{n}")
        except Exception as exc:  # keep the finished swap test
            res["culprit"] = {"error": f"{type(exc).__name__}: {exc}"}
    return res


def place_probe(root, rel, data):
    """Write the probe file under `root`, refusing symlinks anywhere on the way and existing files."""
    # Create parent directories one by one, refusing any that is a symlink.
    path = root
    for part in Path(rel).parts[:-1]:
        path = path / part
        if path.is_symlink():
            return f"{rel}: a directory on the path is a symlink"
        path.mkdir(exist_ok=True)
    # Exclusive create and no-follow: never overwrite a file or write through a link the PR placed.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root / rel, flags, 0o644)
    except OSError as exc:
        return f"{rel}: {exc.strerror}"
    # Write the probe's bytes as-is.
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return None


def run_probe(ws, probe, target):
    """Copy a probe test file into a target (the base or a PR) and run it there.

    Probes check a specific behaviour that the project's own tests may not cover.
    """
    ws.phase("probe")
    # Where the probe goes in the target, and its dotted module name for test runners that need one.
    rel = Path(probe.get("dest") or Path(ws.cfg["test_prefix"]) / ("audit_probe_" + Path(probe["file"]).name))
    module = str(rel.with_suffix("")).replace(os.sep, ".")
    # Resolve the probe command's placeholders.
    argv = fill(probe["cmd"], python=ws.cfg["python"], dest=rel, module=module)
    row = {"probe": probe["name"], "target": target}
    # Export the target, add the probe, then run it.
    scratch, root = ws.export(ws.ref(target))
    try:
        refused = place_probe(root, rel, (ws.cfg["_dir"] / probe["file"]).read_bytes())
        if refused:
            return dict(row, outcome="error", message=refused)
        ws.count()
        rc, out, setup_failed = ws.execute(scratch, root, argv)
    finally:
        remove_tree(scratch)
    if setup_failed:
        return dict(row, outcome="fail", message=setup_failure(rc, out)["message"])
    if rc is None:
        return dict(row, outcome="fail", message="(timeout)")
    # Reduce the run to an outcome and a one-line message.
    res = scan(ws.cfg, rc, out)
    message = res.get("message") or ("(no tests ran)" if res.get("failures") == ["(no tests ran)"] else "")
    return dict(row, outcome=res["outcome"], message=message)


def tool_versions(cfg):
    """git, OS and (when used) Python versions, recorded so a report states its environment."""
    # git version and operating system always; Python only if a command uses it.
    gv = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip().split()[2]
    tools = {"git": gv, "os": platform.system()}
    cmds = [cfg["test_cmd"], cfg.get("setup_cmd") or []] + [p["cmd"] for p in cfg["probes"]]
    if any("{python}" in a for cmd in cmds for a in cmd):
        tools["python"] = subprocess.run([cfg["python"], "-c", "import platform;print(platform.python_version())"],
                                         capture_output=True, text=True).stdout.strip()
    return tools


def job_error(kind, n, exc):
    """The result recorded for a follow-up job that raised."""
    error = f"{type(exc).__name__}: {exc}"
    if kind == "swap":
        return {"blockers": [], "outcome": "error", "message": error}
    if kind == "culprit":
        return {"error": error}
    return {"probe": n[0], "target": n[1], "outcome": "error", "message": error}


def timing_summary(wall, seconds):
    """Wall time per phase and test-run durations, for sizing later builds. Never rendered in the report."""
    def stats(xs):
        """Count, total, median and maximum of a list of durations, in seconds."""
        xs = sorted(xs)
        return {"count": len(xs), "total": round(sum(xs), 1), "median": round(statistics.median(xs), 2) if xs else 0,
                "max": round(xs[-1], 2) if xs else 0}
    # Rounded, so results stay readable and stable.
    return {"wall_seconds": {k: round(v, 1) for k, v in wall.items()},
            "run_seconds": {k: stats(v) for k, v in seconds.items()}}


def check_emit(args, snap):
    """Validate `--emit-branch` and `--emit-repo` before any work; return the receiving git directory.

    The build's own repository is private and deleted afterwards, so the branch must go into a
    repository the operator names. Every refusal git would make when the branch is written at the
    end is checked here, so a long build is not wasted. Returns None when no branch is to be emitted.
    """
    # A repository without a branch to put in it is a usage mistake, not something to ignore.
    if args.emit_repo and not args.emit_branch:
        sys.exit("usage: --emit-repo needs --emit-branch, the branch to create there")
    # Nothing to check unless a branch was asked for.
    if not args.emit_branch:
        return None
    # The receiving repository is never guessed: it must be named explicitly.
    if not args.emit_repo:
        sys.exit("usage: --emit-branch needs --emit-repo, the local git repository that receives the branch")
    path = Path(args.emit_repo).resolve()
    if not path.is_dir():
        sys.exit(f"--emit-repo {path}: no such directory")
    # Resolve to the repository's git directory, as `git -C PATH` would find it; show git's own reason
    # (not a repository, a safe.directory refusal, ...) when it cannot.
    found = git(path, "rev-parse", "--absolute-git-dir", check=False)
    if found.returncode:
        sys.exit(f"--emit-repo {path}: {found.stderr.strip()}")
    gitdir = found.stdout.strip()
    ref = args.emit_branch
    # Only a full, well-formed branch name, so the ref written is exactly the one given.
    if not ref.startswith("refs/heads/") or git(gitdir, "check-ref-format", ref, check=False).returncode:
        sys.exit(f"--emit-branch {ref}: give a full branch name, such as refs/heads/integration")
    # The snapshot's commits must be storable there: SHA-1 ids cannot go into a SHA-256 repository.
    want = "sha256" if len(snap["base_sha"]) == 64 else "sha1"
    have = git(gitdir, "rev-parse", "--show-object-format").stdout.strip()
    if have != want:
        sys.exit(f"--emit-repo {gitdir} uses {have} object ids, but the snapshot uses {want}")
    # A symbolic ref, even one pointing nowhere, would make git write a different branch.
    if symref(gitdir, ref):
        sys.exit(f"--emit-branch {ref} is a symbolic ref to {symref(gitdir, ref)} in {gitdir}; choose another name")
    # Dry run of the final write: lock the ref, requiring that it does not exist, then let go. This
    # catches an existing ref and a clash with a directory or file of refs (`x` versus `x/y`).
    probe = git(gitdir, "update-ref", "--stdin", check=False,
                data=f"start\nverify {ref} {'0' * len(snap['base_sha'])}\nprepare\nabort\n")
    if probe.returncode:
        sys.exit(f"--emit-branch {ref} cannot be created in {gitdir}: {probe.stderr.strip()}")
    return gitdir


def symref(gitdir, ref):
    """What `ref` points to if it is a symbolic ref in `gitdir`, else None; writing it would write that."""
    target = git(gitdir, "symbolic-ref", "-q", ref, check=False)
    return None if target.returncode else target.stdout.strip()


def caller_env():
    """The caller's environment without GIT_* variables: their own identity and the current time."""
    return dict({k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, GIT_TERMINAL_PROMPT="0")


def emit_branch(ws, gitdir, ref, tip):
    """Create branch `ref` at `tip` in the repository at `gitdir`; nothing is pushed or overwritten."""
    # Name the tip inside the private workspace, so it can be fetched from there.
    git(ws.repo, "update-ref", "refs/audit/emit", tip)
    # Copy its commits into the receiving repository; no remote, tag or FETCH_HEAD is touched.
    git(gitdir, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(ws.repo), "refs/audit/emit")
    # The ref may have become a symbolic ref during the build; check again right before writing.
    if symref(gitdir, ref):
        raise RuntimeError(f"{ref} is now a symbolic ref to {symref(gitdir, ref)}")
    # Create the branch; the empty old value makes git refuse if the ref exists by now. The reflog
    # entry carries the operator's own identity and the real time, not the fixed merge identity;
    # the commit itself, and so its id, are unchanged.
    git(gitdir, "update-ref", "-m", "pr-merge-audit build", ref, tip, "", env=caller_env())


def cmd_build(cfg, args):
    """`build` command: merge-test the snapshot offline and write the results file.

    Phases: pre-flight checks, the sequential merge build, then parallel follow-ups (swap tests,
    culprit searches, probes). Runs PR code; see SECURITY.md.
    """
    check_git()
    # Load the snapshot and validate probes and the branch to emit before doing any work.
    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    check_probes(cfg, snap)
    emit_to = check_emit(args, snap)
    # The bundle sits next to the snapshot unless given explicitly.
    bundle = args.bundle or str(Path(args.snapshot).with_suffix(".bundle"))
    print("note: this runs code from every PR it tries; use a disposable machine without credentials",
          file=sys.stderr)
    # Every deviation from the default rules is recorded and shown at the top of the report.
    overrides = []
    # Operator chose to try PRs regardless of their required check.
    if args.ignore_status and cfg.get("required_status"):
        overrides.append(f"required status check `{cfg['required_status']}` ignored")
        cfg = dict(cfg, required_status="")
    # Start the clock and set up the workspace.
    started = time.monotonic()
    ws = Workspace(cfg, snap, bundle, args.work)
    # Pre-flight: stop if test runs would lack submodules or LFS content, unless explicitly allowed.
    missing = " and ".join(ws.incomplete_checkout(ws.base))
    if missing and not cfg.get("allow_incomplete_checkout"):
        remove_tree(ws.work)
        sys.exit(f"the base {snap['base_sha'][:7]} uses {missing}, which test runs would not contain; "
                 f"set allow_incomplete_checkout to run anyway")
    if missing:
        overrides.append(f"test runs lack {missing} (allow_incomplete_checkout)")
    # Decide which PRs to try, then check their heads for content test runs would lack.
    seq, not_tried = classify(cfg, snap)
    lacking = {n: ws.incomplete_checkout(ws.heads[n]) for n in seq}
    lacking = ", ".join(f"#{n} ({' and '.join(m)})" for n, m in lacking.items() if m)
    if lacking and not cfg.get("allow_incomplete_checkout"):
        remove_tree(ws.work)
        sys.exit(f"PRs {lacking} add content test runs would not contain; "
                 f"set allow_incomplete_checkout to run anyway")
    if lacking:
        overrides.append(f"test runs of PRs {lacking} lack that content (allow_incomplete_checkout)")
    print(f"merging {len(seq)} PRs in order", flush=True)
    # Phase 1: sequential build, one test-suite run at a time.
    steps, merged, tip, base_result, final = run_sequence(ws, seq)
    # A failing base makes every later result meaningless: stop and say so.
    sequence_done = time.monotonic()
    if base_result["outcome"] not in ("pass", "flaky"):
        remove_tree(ws.work)
        detail = ", ".join(base_result.get("failures", [])) + (": " + base_result["message"] if base_result.get("message") else "")
        sys.exit(f"tests fail on the base {snap['base_sha'][:7]} before any PR is merged ({detail}); "
                 f"fix the test environment or test_cmd first")
    # Phase 2: follow-up jobs for every held PR, plus probes, on `workers` threads.
    held = [s for s in steps if s["outcome"] != "merged"]
    print(f"{len(merged)} merged, {len(held)} held; running follow-ups", flush=True)
    # Conflicts get a swap test; failures get a culprit search; flaky PRs get neither.
    swaps, culprits, probes = {}, {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        jobs = {}
        for s in held:
            n = s["pr"]
            if s["outcome"] == "conflict":
                jobs[pool.submit(swap, ws, n, merged, tip)] = ("swap", n)
            elif s["outcome"] != "flaky":  # it passed on re-run, so there is no failure to trace
                merged_before = [m["pr"] for m in merged if seq.index(m["pr"]) < seq.index(n)]
                jobs[pool.submit(find_culprit, ws, n, merged_before, f"cul-{n}")] = ("culprit", n)
        for probe in cfg["probes"]:
            for target in probe["targets"]:
                jobs[pool.submit(run_probe, ws, probe, target)] = ("probe", (probe["name"], target))
        # Collect results as jobs finish; a job that raised is recorded as an error.
        for fut in concurrent.futures.as_completed(jobs):
            kind, n = jobs[fut]
            try:
                result = fut.result()
            except Exception as exc:  # one broken job must not discard hours of other results
                result = job_error(kind, n, exc)
            if kind == "swap":
                swaps[str(n)] = result
            elif kind == "culprit":
                culprits[str(n)] = result
            else:
                probes.append(result)
    order = {(p["name"], str(t)): i for p in cfg["probes"] for i, t in enumerate(p["targets"])}
    follow_ups_done = time.monotonic()
    # Jobs finish in any order; sort results back into configuration order so output is deterministic.
    probes.sort(key=lambda r: ([p["name"] for p in cfg["probes"]].index(r["probe"]), order[(r["probe"], str(r["target"]))]))
    # The results file: everything the report needs, plus run counts and timings for planning.
    results = {
        "snapshot": {k: snap[k] for k in ("date", "repo", "base_ref", "base_sha")},
        "open_prs": len(snap["prs"]),
        "tools": tool_versions(cfg),
        "test_cmd": " ".join(a.replace("{python}", "python") for a in cfg["test_cmd"]),
        "rules": {
            "required_status": cfg.get("required_status") or "",
            "test_files": cfg["test_files"],
            "order_listed": len(cfg["order"]),
            "overrides": overrides,
        },
        "base_tests": base_result,
        "final_tests": final,
        "order": seq,
        "not_tried": not_tried,
        "steps": steps,
        "merged": [m["pr"] for m in merged],
        "swaps": dict(sorted(swaps.items(), key=lambda kv: seq.index(int(kv[0])))),
        "culprits": dict(sorted(culprits.items(), key=lambda kv: seq.index(int(kv[0])))),
        "probes": probes,
        "test_runs": dict(ws.runs, workers=cfg["workers"]),
        "timing": timing_summary({"total": follow_ups_done - started, "sequence": sequence_done - started,
                                  "follow_ups": follow_ups_done - sequence_done}, ws.seconds),
    }
    # Optionally keep the sequential build's tip as a branch, with a manifest of what it holds.
    # The manifest is a results key the report never reads, so the report is unchanged.
    emit_error = None
    if emit_to:
        try:
            emit_branch(ws, emit_to, args.emit_branch, tip)
            results["emitted"] = {
                "repo": emit_to, "ref": args.emit_branch, "commit": tip,
                "included": [m["pr"] for m in merged],
                "excluded": [{"pr": s["pr"], "reason": s["outcome"]} for s in held]
                + [{"pr": r["pr"], "reason": "not tried: " + r["reason"]} for r in not_tried],
            }
        except RuntimeError as exc:  # the results are still worth keeping
            emit_error = str(exc)
    # Write the results file and clear the workspace.
    out = Path(args.out or f"results/{snap['date']}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_text(out, json.dumps(results, indent=1, sort_keys=True) + "\n")
    remove_tree(ws.work)
    print(f"wrote {out}")
    # Say where the branch went, or why it was not created.
    if emit_error:
        sys.exit(f"{args.emit_branch} was not created: {emit_error}")
    if emit_to:
        print(f"created {args.emit_branch} at {tip[:7]} in {emit_to}")


# ---------------------------------------------------------------- report


def clean(text, table=False):
    """Text from test output, made safe inside a Markdown code span (and a table cell)."""
    # Collapse control characters, neutralise backticks and cap the length.
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).replace("`", "'")
    if len(text) > 200:
        text = text[:200] + "..."
    return text.replace("|", "\\|") if table else text


def refs(ns):
    """PR numbers as a comma-separated list of `#N` references."""
    return ", ".join(f"#{n}" for n in ns)


def code(names, table=False):
    """Names as a comma-separated list of Markdown code spans."""
    return ", ".join(f"`{clean(x, table)}`" for x in names)


def render(cfg, res):
    """Render a results file as a GitHub-flavoured Markdown report.

    Pure function of `res`: the same results always produce the same text (see `verify`).
    """
    # Index the results for lookup by PR number.
    snap = res["snapshot"]
    base = f"`{snap['base_ref']}` ({snap['base_sha'][:7]})"
    tools = res["tools"]
    steps = {s["pr"]: s for s in res["steps"]}
    swaps = {int(k): v for k, v in res["swaps"].items()}
    culprits = {int(k): v for k, v in res["culprits"].items()}
    tried = len(res["order"])
    # The report is assembled line by line in `L`; `w` appends one line.
    L = []
    w = L.append
    # Title, then any overrides, so deviations are seen first.
    w(f"**Merge test of the open PRs against {base}**")
    w("")
    rules = res.get("rules", {})
    for o in rules.get("overrides", []):
        w(f"**Override:** {o}.")
        w("")
    # Collapsible section describing exactly how the build was run.
    w("<details>")
    w("<summary><b>Procedure</b></summary>")
    w("")
    # Base, environment and test command, as recorded in the results.
    w(f"- **Base:** `{snap['base_ref']}` at `{snap['base_sha'][:7]}`, with PR heads fetched on {snap['date']}.")
    py = f"Python {tools['python']}, " if tools.get("python") else ""
    w(f"- **Environment:** {py}git {tools['git']}, {tools['os']}.")
    skipped = res["final_tests"].get("skipped", 0)
    note = f" {skipped} tests were skipped in the final run." if skipped else ""
    w(f"- **Test run:** `{res['test_cmd']}` after every merge.{note}")
    # How each phase works, in plain terms.
    w("- **Merging:** each PR is merged with `git merge --no-ff` onto the build so far, in a fixed order "
      "taken from the configuration; PRs not listed there follow in PR-number order.")
    w("  - If a merge conflicts, it's aborted and the PR is recorded as conflicting.")
    w("  - If tests fail after a merge, they are run once more; if they fail again the merge is reset "
      "and the PR is recorded as failing. A pass on the second run is recorded as flaky and also reset.")
    w("- **Swap test:** for each conflicting PR, the merged PR(s) it conflicts with on its own "
      "(`git merge-tree`) are reverted, it is merged in their place, and the tests are run again.")
    w("- **Culprit search:** for each PR whose tests fail with no text conflict, or still fail after "
      "its swap, the merge sequence is bisected to the first merged PR that makes it fail; that pair "
      "is then merged onto the base alone and tested.")
    # The selection rules in force.
    if rules:
        status = f"`{rules['required_status']}` must pass" if rules["required_status"] else "none required"
        w(f"- **Rules:** status check: {status}; a PR counts as changing tests if a file matches "
          f"`{clean(rules['test_files'])}`; {rules['order_listed']} PRs have a configured position in the "
          f"order. Exclusions, duplicates and features are listed under Not tried.")
    w("")
    w(f"The merge order decides which side of each conflict merged first. A different order would "
      f"give a different set.")
    w("")
    w("</details>")
    w("")
    # Warn if the base itself is unreliable.
    if res["base_tests"]["outcome"] == "flaky":
        w("**Warning:** the tests on the base are flaky (failed, then passed on re-run).")
        w("")
    elif res["base_tests"]["outcome"] != "pass":
        w(f"**Warning:** the tests do not pass on the base itself ({code(res['base_tests'].get('failures', []))}).")
        w("")
    # Headline: how many of the tried PRs combine with all tests passing.
    merged = res["merged"]
    if tried:
        w(f"### Result: {len(merged)} of {tried} PRs merged together with every test passing")
        w("")
        w(refs(merged) or "None of them merged.")
    else:
        w("### Result: no PRs were eligible to try")
    w("")

    # Conflicting PRs, split by whether their tests pass once swapped in.
    ok_swaps = [n for n in res["order"] if n in swaps and swaps[n].get("outcome") == "pass"]
    bad_swaps = [n for n in res["order"] if n in swaps and swaps[n].get("outcome") != "pass"]
    # Section: text conflicts that pass once swapped in.
    if ok_swaps:
        w(f"### Text conflicts; tests pass once swapped in ({len(ok_swaps)})")
        w("")
        w("Each pair changes the same lines. One may replace the other, or a rebase may combine them.")
        w("")
        w("| PR | Conflicts with |")
        w("|---|---|")
        for n in ok_swaps:
            w(f"| #{n} | {refs(swaps[n]['blockers']) or '(only the combined build)'} |")
        w("")

    # Section: conflicts whose tests still fail after the swap. Culprit pairs found here are
    # collected for the next section.
    pairs = []
    if bad_swaps:
        w(f"### Tests fail even after swapping ({len(bad_swaps)})")
        w("")
    # One line per PR, worded by what happened to its swap test.
    for n in bad_swaps:
        s = swaps[n]
        out = s.get("outcome")
        if out == "error":
            w(f"- **#{n}:** the swap test could not run ({code([s.get('message', '')])}).")
            continue
        if out in ("conflict", "revert_conflict"):
            what = "still conflicts after the swap" if out == "conflict" else "a blocking PR could not be reverted"
            w(f"- **#{n}:** conflicts with {refs(s['blockers']) or 'the combined build only'}; {what}.")
            continue
        verb = "is flaky on" if out == "flaky" else "fails"
        reverted = f"with {refs(s['blockers'])} reverted" if s["blockers"] else "with nothing to revert"
        w(f"- **#{n}:** {reverted}, {verb} {code(s.get('failures', []))}.")
        c = s.get("culprit", {})
        if c.get("on_base") == "fail":
            w(f"  It also fails on `{snap['base_ref']}` alone.")
        elif c.get("on_base") == "conflict":
            w(f"  It does not merge onto `{snap['base_ref']}` alone.")
        if c.get("culprit"):
            pairs.append((c["culprit"], n, c["pair"]))
    if bad_swaps:
        w("")

    # Collect culprit pairs from the failures of the sequential build.
    for n in res["order"]:
        c = culprits.get(n)
        if c and c.get("culprit"):
            pairs.append((c["culprit"], n, c["pair"]))
    # Section: pairs that merge cleanly but fail their tests together.
    if pairs:
        w(f"### No text conflict, but a pair fails its tests ({len(pairs)})")
        w("")
        w(f"Each pair merged onto `{snap['base_ref']}` alone:")
        w("")
        w("| Pair | Result | Failing tests |")
        w("|---|---|---|")
        for a, b, p in pairs:
            w(f"| #{a} + #{b} | {p['outcome']} | {code(p.get('failures', []), table=True)} |")
        w("")

    # Failures without a confirmed culprit, culprit-search errors and flaky PRs.
    other = []
    # One entry per PR whose failure has no confirmed culprit, or that was flaky.
    for n in res["order"]:
        st = steps.get(n, {})
        c = culprits.get(n, {})
        if c.get("error"):
            other.append(f"- **Culprit search could not run:** #{n} ({code([c['error']])}).")
        elif c.get("on_base") == "fail":
            other.append(f"- **Fails its own tests on `{snap['base_ref']}`:** #{n} ({code(st.get('failures', []))}).")
        elif c.get("on_base") == "conflict":
            other.append(f"- **Fails in the build and does not merge onto `{snap['base_ref']}` alone:** #{n} ({code(st.get('failures', []))}).")
        elif c and not c.get("culprit"):
            other.append(f"- **Fails only in the full build, no single culprit found:** #{n} ({code(st.get('failures', []))}).")
        if st.get("outcome") == "flaky":
            other.append(f"- **Flaky (failed, then passed on re-run):** #{n}.")
    # Section: everything else worth a reader's attention.
    if other:
        w("### Other")
        w("")
        L.extend(other)
        w("")

    # Section: probe results, one row per probe and target.
    if res["probes"]:
        w("### Probes")
        w("")
        w("| Probe | Target | Result | Message |")
        w("|---|---|---|---|")
        for p in res["probes"]:
            t = f"`{snap['base_ref']}`" if p["target"] == "base" else f"#{p['target']}"
            w(f"| {p['probe']} | {t} | {p['outcome']} | {('`' + clean(p['message'], table=True) + '`') if p['message'] else ''} |")
        w("")

    # PRs not tried, grouped by reason, in a fixed order.
    labels = [
        ("excluded", "Excluded"),
        ("status", f"Required status check not successful"),
        ("draft", "Draft"),
        ("duplicate", "Same change as another open PR"),
        ("feature", "Feature, left for separate review"),
        ("no_tests", "Changes code without tests"),
    ]
    # Section: PRs not tried, by reason.
    if res["not_tried"]:
        w(f"### Not tried ({len(res['not_tried'])} of {res['open_prs']} open PRs)")
        w("")
    for key, label in labels:
        rows = [r for r in res["not_tried"] if r["reason"] == key]
        if not rows:
            continue
        if key in ("excluded", "duplicate"):
            w(f"- **{label}:**")
            for r in rows:
                w(f"  - #{r['pr']}: {r['detail']}")
        elif key == "status":
            w(f"- **{label}:** " + ", ".join(f"#{r['pr']} ({r['detail']})" for r in rows))
        else:
            w(f"- **{label}:** {refs(r['pr'] for r in rows)}")
    if res["not_tried"]:
        w("")
    # Cost summary, so readers can estimate the effort of a re-run.
    runs = res.get("test_runs")
    # Footer: how many test-suite runs the build took.
    if runs:
        total = runs["sequence"] + runs["swap"] + runs["culprit"] + runs["probe"]
        w(f"**Test-suite runs:** {total} in total: {runs['sequence']} for the sequential build, "
          f"{runs['swap']} for swap tests, {runs['culprit']} for culprit searches and {runs['probe']} "
          f"for probes. The sequential build runs one at a time; the rest ran on {runs['workers']} "
          f"parallel workers. Running time scales with the time one test-suite run takes on the "
          f"machine used.")
        w("")
    # Closing caveat on what the results prove.
    w("Tests passing only shows that the PRs combine. It doesn't show each fix is correct.")
    return "\n".join(L) + "\n"


def cmd_report(cfg, args):
    """`report` command: render a results file to Markdown (stdout or `--out`)."""
    res = json.loads(Path(args.results).read_text(encoding="utf-8"))
    # Render, then write to a file or standard output.
    text = render(cfg, res)
    if args.out:
        write_text(Path(args.out), text)
    else:
        sys.stdout.write(text)


def cmd_verify(cfg, args):
    """`verify` command: check that rendering is stable and, optionally, matches a golden report."""
    res = json.loads(Path(args.results).read_text(encoding="utf-8"))
    # Render twice from independent reads; any difference means non-deterministic output.
    a, b = render(cfg, res), render(cfg, json.loads(Path(args.results).read_text(encoding="utf-8")))
    if a != b:
        sys.exit("FAIL: two renders of the same results differ")
    # Optionally compare with a reviewed report and show a diff on mismatch.
    if args.golden:
        want = Path(args.golden).read_text(encoding="utf-8")
        if a != want:
            import difflib
            sys.stdout.writelines(difflib.unified_diff(want.splitlines(True), a.splitlines(True), "golden", "rendered"))
            sys.exit("FAIL: report differs from the golden copy")
    print("PASS: report is stable" + (" and matches the golden copy" if args.golden else ""))


def cmd_estimate(cfg, args):
    """Time one test-suite run here and scale it by the run counts of a reference build."""
    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    ref = json.loads(Path(args.reference).read_text(encoding="utf-8"))
    # The reference build must carry per-phase run counts.
    runs = ref.get("test_runs")
    if not runs:
        sys.exit(f"{args.reference} has no test_runs counts; use results from a newer build")
    check_git()
    bundle = args.bundle or str(Path(args.snapshot).with_suffix(".bundle"))
    # Time one real test-suite run on the base, on this machine.
    ws = Workspace(cfg, snap, bundle, args.work)
    wt = ws.tree("estimate", ws.base)
    argv = fill(cfg["test_cmd"], python=cfg["python"])
    start = time.monotonic()
    ws.test_once(wt, argv)
    one = time.monotonic() - start
    ws.drop(wt)
    remove_tree(ws.work)
    # Scale the reference build's run counts by the number of PRs this snapshot would try.
    scale = len(classify(cfg, snap)[0]) / max(1, len(ref["order"]))
    # Sequential runs happen one at a time; follow-up runs share the workers.
    seq = runs["sequence"] * scale * one
    rest = (runs["swap"] + runs["culprit"] + runs["probe"]) * scale * one / max(1, cfg["workers"])
    print(f"one test-suite run here: {one:.0f} s")
    print(f"scaled from the reference build ({len(ref['order'])} PRs tried; {scale:.2f}x for this snapshot):")
    print(f"  sequential build  ~{seq / 60:.0f} min")
    print(f"  follow-up jobs    ~{rest / 60:.0f} min on {cfg['workers']} workers")
    print(f"  total             ~{(seq + rest) / 60:.0f} min (merges and bisection rebuilds add some more)")


def main():
    """Command-line entry point: parse arguments and dispatch to the sub-command."""
    for stream in (sys.stdout, sys.stderr):  # a PR title the console's code page lacks must not crash
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    # Command-line interface: a global --config and one sub-command per phase.
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # snapshot: freeze PR metadata and commits (network).
    s = sub.add_parser("snapshot")
    s.add_argument("--date")
    s.add_argument("--out", default="snapshots")
    s.add_argument("--work", default="work")
    # build: run the merge test on a snapshot (offline; runs PR code).
    b = sub.add_parser("build")
    b.add_argument("--snapshot", required=True)
    b.add_argument("--bundle")
    b.add_argument("--work", default="work/build")
    b.add_argument("--out")
    b.add_argument("--ignore-status", action="store_true",
                   help="try PRs whatever their required status check says (shown in the report)")
    b.add_argument("--emit-branch", metavar="REF",
                   help="create branch REF (e.g. refs/heads/integration) at the merged, passing build; never overwrites")
    b.add_argument("--emit-repo", metavar="PATH", help="local git repository that receives --emit-branch; nothing is pushed")
    # report: render a results file as Markdown.
    r = sub.add_parser("report")
    r.add_argument("--results", required=True)
    r.add_argument("--out")
    # verify: check the report is stable and matches a golden copy.
    v = sub.add_parser("verify")
    v.add_argument("--results", required=True)
    v.add_argument("--golden")
    # estimate: predict the running time of a build on this machine.
    e = sub.add_parser("estimate")
    e.add_argument("--snapshot", required=True)
    e.add_argument("--bundle")
    e.add_argument("--reference", required=True, help="results file of an earlier build")
    e.add_argument("--work", default="work/estimate")
    # Parse, load the configuration where needed, and dispatch.
    args = ap.parse_args()
    # report and verify read only a results file, so they need no project config.
    cfg = load_config(args.config) if args.cmd in ("snapshot", "build", "estimate") else {}
    {"snapshot": cmd_snapshot, "build": cmd_build, "report": cmd_report, "verify": cmd_verify, "estimate": cmd_estimate}[args.cmd](cfg, args)


# Run as a script.
if __name__ == "__main__":
    main()
