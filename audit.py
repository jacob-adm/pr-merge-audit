#!/usr/bin/env python3
"""Merge-test the open pull requests of a repository and report how they combine.

Needs only the Python standard library (3.8+) and git (2.38+, for `git merge-tree --write-tree`).

    audit.py snapshot --config config.json            # network: freeze PR heads and metadata
    audit.py build    --config config.json --snapshot snapshots/DATE.json
    audit.py report   --config config.json --results results/DATE.json
    audit.py verify   --config config.json --results results/DATE.json --golden report.md

`build` and `report` are offline. The same snapshot, config and machine give the same report.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
TEST_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
}
LINK_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)", re.I)
MARKER = ".audit-workspace"
MIN_GIT = (2, 38)
OUTPUT_CAP = 4 * 1024 * 1024
PASS_ENV = ("PATH", "SYSTEMROOT", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    if not cfg.get("test_cmd"):
        sys.exit(f"{path}: test_cmd is required")
    for probe in cfg.get("probes", []):
        if not probe.get("cmd"):
            sys.exit(f"{path}: probe {probe.get('name')!r} needs a cmd")
    cfg.setdefault("python", sys.executable)
    cfg.setdefault("test_prefix", "tests/")
    cfg.setdefault("test_files", "^" + re.escape(cfg["test_prefix"]))
    cfg.setdefault("test_timeout", 1800)
    cfg.setdefault("workers", 4)
    cfg.setdefault("order", [])
    for key in ("exclude", "duplicates", "features"):
        cfg[key] = {int(k): v for k, v in cfg.get(key, {}).items()}
    cfg["no_tests_ok"] = set(cfg.get("no_tests_ok", []))
    cfg.setdefault("probes", [])
    cfg.setdefault("pass_env", [])
    cfg.setdefault("env", {})
    if not (isinstance(cfg["pass_env"], list) and all(isinstance(k, str) for k in cfg["pass_env"])):
        sys.exit(f"{path}: pass_env must be a list of variable names")
    if not (isinstance(cfg["env"], dict) and all(isinstance(v, str) for v in cfg["env"].values())):
        sys.exit(f"{path}: env must map variable names to strings")
    if "setup_cmd" in cfg and not (isinstance(cfg["setup_cmd"], list) and cfg["setup_cmd"]):
        sys.exit(f"{path}: setup_cmd must be a non-empty list of arguments")
    cfg["_dir"] = Path(path).resolve().parent
    return cfg


def fill(args, **values):
    """Replace `{name}` placeholders by plain substitution, so other braces in arguments are left alone."""
    out = []
    for a in args:
        for k, v in values.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


def base_env():
    """Only the variables a subprocess needs, so the caller's GIT_*, tokens and the like stay out."""
    return {k: os.environ[k] for k in PASS_ENV if k in os.environ}


def git(repo, *args, check=True, binary=False):
    env = dict(base_env(), **GIT_ENV)
    # gc.auto=0: parallel jobs merge in worktrees of one repository; a background gc must not race them.
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "gc.auto=0", *args]
    r = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=not binary)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def git_version(text):
    """(major, minor) from `git --version` output, (0, 0) if it has none."""
    m = re.search(r"(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def check_git():
    try:
        found = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    except OSError:
        found = ""
    if git_version(found) < MIN_GIT:
        sys.exit(f"git {MIN_GIT[0]}.{MIN_GIT[1]} or later is needed (for `git merge-tree --write-tree`); "
                 f"found: {found or 'no git on PATH'}")


def check_probes(cfg, snap):
    """Stop before any test runs if a probe names a missing file or a target not in the snapshot."""
    numbers = {p["number"] for p in snap["prs"]}
    for probe in cfg["probes"]:
        if not (cfg["_dir"] / probe["file"]).is_file():
            sys.exit(f"probe {probe['name']!r}: file {probe['file']} not found next to the config")
        for target in probe.get("targets", []):
            if target != "base" and target not in numbers:
                sys.exit(f"probe {probe['name']!r}: target {target!r} is neither \"base\" nor an open PR in the snapshot")


# ---------------------------------------------------------------- snapshot


def api(cfg, path):
    req = urllib.request.Request(cfg["api"].rstrip("/") + path)
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        # Unredirected, so the token is never sent on to another host.
        req.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
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
    except urllib.error.URLError as exc:
        sys.exit(f"{req.full_url}: {exc.reason}")


def required_status(cfg, sha):
    name = cfg.get("required_status")
    if not name:
        return "SUCCESS"
    combined = api(cfg, f"/repos/{cfg['repo']}/commits/{sha}/status?per_page=100")
    for s in combined.get("statuses", []):
        if s["context"] == name:
            return s["state"].upper()
    runs = api(cfg, f"/repos/{cfg['repo']}/commits/{sha}/check-runs?check_name={urllib.parse.quote(name, safe='')}")
    for run in runs.get("check_runs", []):
        return (run.get("conclusion") or run.get("status") or "").upper()
    return "MISSING"


def cmd_snapshot(cfg, args):
    check_git()
    date = args.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pulls, page = [], 1
    while True:
        batch = api(cfg, f"/repos/{cfg['repo']}/pulls?state=open&per_page=100&page={page}")
        pulls += [p for p in batch if p["base"]["ref"] == cfg["base"]]
        if len(batch) < 100:
            break
        page += 1
    cache = Path(args.work) / "cache.git"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        git(".", "init", "-q", "--bare", str(cache))
    stale = git(cache, "for-each-ref", "--format=delete %(refname)", "refs/audit").stdout
    subprocess.run(["git", "update-ref", "--stdin"], cwd=cache, input=stale, text=True, check=True,
                   env=dict(base_env(), **GIT_ENV))
    specs = [f"+refs/heads/{cfg['base']}:refs/audit/base"]
    specs += [f"+refs/pull/{p['number']}/head:refs/audit/pr/{p['number']}" for p in pulls]
    git(cache, "fetch", "-q", "--no-tags", cfg["remote"], *specs)
    base_sha = git(cache, "rev-parse", "refs/audit/base").stdout.strip()
    prs = []
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
            "linked": sorted({int(x) for x in LINK_RE.findall(p.get("body") or "")}),
            "files": sorted(files),
        })
    snap = {"date": date, "repo": cfg["repo"], "base_ref": cfg["base"], "base_sha": base_sha, "prs": prs}
    (out / f"{date}.json").write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    git(cache, "bundle", "create", "-q", str((out / f"{date}.bundle").resolve()), "--glob=refs/audit")
    print(f"{out / date}.json: {len(prs)} open PRs against {cfg['base']} {base_sha[:7]}")


# ---------------------------------------------------------------- build


def names(z):
    return [x for x in z.split("\0") if x]


def safe_member(root, m):
    """Whether a tar member from `git archive` can be written under `root` without escaping it."""
    parts = Path(m.name).parts
    if os.path.isabs(m.name) or ".." in parts or not (m.isfile() or m.isdir() or m.issym()):
        return False
    if m.issym():
        target = os.path.normpath(os.path.join(os.path.dirname(m.name), m.linkname))
        if os.path.isabs(m.linkname) or target == ".." or target.startswith(".." + os.sep):
            return False
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
        m = re.search(cfg[key], out, re.M) if cfg.get(key) else None
        return m.group(1).strip() if m else ""

    def number(key):
        m = re.search(r"\d+", first(key))
        return int(m.group()) if m else 0

    ran = number("count_pattern")
    res = {"outcome": "pass" if rc == 0 and (ran or not cfg.get("count_pattern")) else "fail"}
    if rc:
        found = [m.group(1) for m in re.finditer(cfg["failure_pattern"], out, re.M)] if cfg.get("failure_pattern") else []
        res["failures"] = sorted(set(found)) or [f"(exit status {rc})"]
        res["message"] = first("message_pattern")
    elif res["outcome"] == "fail":
        res.update(failures=["(no tests ran)"], message="")
    res["ran"] = ran
    res["skipped"] = number("skipped_pattern")
    return res


def setup_failure(rc, out):
    """Result for a run whose setup_cmd failed; the tests never ran."""
    lines = [x for x in out.splitlines() if x.strip()]
    detail = "timeout" if rc is None else f"exit status {rc}"
    return {"outcome": "fail", "failures": ["(setup failed)"],
            "message": f"setup_cmd {detail}" + (f": {lines[-1].strip()}" if lines else "")}


def read_capped(path):
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
    eligible, not_tried = [], []
    for p in snap["prs"]:
        n = p["number"]
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
        else:
            eligible.append(n)
            continue
        not_tried.append({"pr": n, "reason": reason, "detail": detail})
    rank = {n: i for i, n in enumerate(cfg["order"])}
    eligible.sort(key=lambda n: (rank.get(n, len(rank)), n))
    return eligible, not_tried


class Workspace:
    def __init__(self, cfg, snap, bundle, work):
        self.cfg, self.snap = cfg, snap
        self.work = Path(work).resolve()
        self.repo = self.work / "repo"
        self.lock = threading.Lock()
        self.local = threading.local()
        self.runs = {"sequence": 0, "swap": 0, "culprit": 0, "probe": 0}
        if self.work.exists() and any(self.work.iterdir()):
            if not (self.work / MARKER).is_file():
                sys.exit(f"refusing to clear {self.work}: no {MARKER} marker, so not a build directory")
            shutil.rmtree(self.work)
        self.repo.mkdir(parents=True)
        (self.work / MARKER).write_text("created by audit.py; deleted and recreated on each build\n")
        git(self.repo, "init", "-q")
        # Overrides each commit's own .gitattributes, so a PR cannot drop files from the test copy.
        info = self.repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* -export-ignore -export-subst\n")
        git(self.repo, "fetch", "-q", str(Path(bundle).resolve()), "+refs/audit/*:refs/audit/*")
        self.base = snap["base_sha"]
        self.heads = {p["number"]: p["head"] for p in snap["prs"]}

    def ref(self, n):
        return self.base if n == "base" else self.heads[n]

    def tree(self, name, commit):
        path = self.work / "wt" / name
        with self.lock:
            git(self.repo, "worktree", "add", "-q", "--detach", str(path), commit)
        return path

    def drop(self, path):
        with self.lock:
            git(self.repo, "worktree", "remove", "--force", str(path), check=False)

    def merge(self, wt, n):
        r = git(wt, "merge", "-q", "--no-ff", "--no-edit", "-m", f"merge #{n}", self.heads[n], check=False)
        if r.returncode:
            files = names(git(wt, "diff", "--name-only", "-z", "--diff-filter=U").stdout)
            git(wt, "merge", "--abort", check=False)
            return sorted(files) or ["(merge failed)"]
        return None

    def conflicts(self, a, b):
        r = git(self.repo, "merge-tree", "--write-tree", "--name-only", self.ref(a), self.ref(b), check=False)
        # A result (clean or conflicted) starts with the merged tree's id; anything else is an error,
        # including exit 1 for an unmergeable ref and 129 from a git without --write-tree.
        if r.returncode not in (0, 1) or not re.match(r"[0-9a-f]{40,64}$", r.stdout.split("\n", 1)[0]):
            raise RuntimeError(f"git merge-tree {a} {b} failed: {r.stderr.strip()}")
        return r.returncode == 1

    def incomplete_checkout(self, commit):
        """What test runs of `commit` would lack: `git archive` leaves out submodules, and LFS files stay pointers."""
        missing = []
        rows = [r for r in git(self.repo, "ls-tree", "-r", "-z", commit).stdout.split("\0") if r]
        if any(r.startswith("160000 ") for r in rows):
            missing.append("submodules")
        for r in rows:
            meta, _, path = r.partition("\t")
            if path.rsplit("/", 1)[-1] == ".gitattributes" and meta.split()[1] == "blob":
                if re.search(r"\bfilter=lfs\b", git(self.repo, "cat-file", "blob", meta.split()[2]).stdout):
                    missing.append("Git LFS files")
                    break
        return missing

    def phase(self, name):
        self.local.phase = name

    def count(self):
        with self.lock:
            self.runs[getattr(self.local, "phase", "sequence")] += 1

    def export(self, commit):
        """Copy of `commit`'s files, without .git, in a fresh temporary directory.

        PR code runs only here, so it cannot reach the shared repository's config, hooks or refs
        through the working tree.
        """
        scratch = Path(tempfile.mkdtemp(prefix="audit-run-"))
        root = scratch / "src"
        for d in (root, scratch / "home", scratch / "tmp"):
            d.mkdir()
        try:
            proc = subprocess.Popen(["git", "-c", "core.hooksPath=" + os.devnull, "archive", "--format=tar", commit],
                                    cwd=self.repo, env=dict(base_env(), **GIT_ENV), stdout=subprocess.PIPE)
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for m in tar:
                    if safe_member(root, m):
                        kw = {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
                        tar.extract(m, root, **kw)
            proc.stdout.close()
            if proc.wait():
                raise RuntimeError(f"git archive {commit} failed")
            prune_links(root)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return scratch, root

    def run_in(self, scratch, root, argv):
        """Run `argv` in `root` with a private HOME and TMPDIR; output is kept on disk and capped.

        Variables named in `pass_env` are copied from the caller and `env` is applied last, so a
        toolchain that lives outside PATH (rustup, Go, a virtualenv) can still be reached.
        """
        env = dict(base_env(), **TEST_ENV)
        env.update({k: os.environ[k] for k in self.cfg.get("pass_env", []) if k in os.environ})
        env.update(HOME=str(scratch / "home"), TMPDIR=str(scratch / "tmp"))
        env.update(self.cfg.get("env", {}))
        log = scratch / "output.log"
        posix = hasattr(os, "killpg")
        with open(log, "wb") as f:
            proc = subprocess.Popen(argv, cwd=root, env=env, stdout=f, stderr=subprocess.STDOUT,
                                    start_new_session=posix)
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
                else:
                    proc.kill()
                proc.wait()
        return rc, read_capped(log)

    def execute(self, scratch, root, argv):
        """Run `setup_cmd` (if configured), then `argv`, in the same copy.

        Returns (rc, output, setup_failed); a failed setup skips `argv`.
        """
        setup = self.cfg.get("setup_cmd")
        if setup:
            rc, out = self.run_in(scratch, root, fill(setup, python=self.cfg["python"]))
            if rc != 0:
                return rc, out, True
        rc, out = self.run_in(scratch, root, argv)
        return rc, out, False

    def test_once(self, wt, argv):
        self.count()
        scratch, root = self.export(git(wt, "rev-parse", "HEAD").stdout.strip())
        try:
            rc, out, setup_failed = self.execute(scratch, root, argv)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if setup_failed:
            return setup_failure(rc, out)
        if rc is None:
            return {"outcome": "fail", "failures": ["(timeout)"]}
        return scan(self.cfg, rc, out)

    def test(self, wt, argv=None):
        argv = argv or fill(self.cfg["test_cmd"], python=self.cfg["python"])
        first = self.test_once(wt, argv)
        if first["outcome"] == "pass":
            return first
        second = self.test_once(wt, argv)
        if second["outcome"] == "pass":
            return dict(second, outcome="flaky")
        return first


def run_sequence(ws, seq):
    """Merge `seq` in order onto the base, keeping only PRs that merge and pass the tests."""
    ws.phase("sequence")
    wt = ws.tree("sequence", ws.base)
    steps, merged, final = [], [], ws.test(wt)
    base_result = final
    if base_result["outcome"] not in ("pass", "flaky"):
        ws.drop(wt)
        return steps, merged, ws.base, base_result, final
    for n in seq:
        files = ws.merge(wt, n)
        if files:
            steps.append({"pr": n, "outcome": "conflict", "files": files})
            continue
        res = ws.test(wt)
        if res["outcome"] != "pass":
            git(wt, "reset", "-q", "--hard", "HEAD~1")
            steps.append(dict(res, pr=n))
            continue
        commit = git(wt, "rev-parse", "HEAD").stdout.strip()
        merged.append({"pr": n, "commit": commit})
        steps.append({"pr": n, "outcome": "merged"})
        final = res
    tip = git(wt, "rev-parse", "HEAD").stdout.strip()
    ws.drop(wt)
    return steps, merged, tip, base_result, final


def build_from(ws, name, prs):
    """Worktree with `prs` merged onto the base, skipping any that conflict."""
    wt = ws.tree(name, ws.base)
    used = [n for n in prs if ws.merge(wt, n) is None]
    return wt, used


def find_culprit(ws, n, seq, name):
    """First PR in `seq` whose addition makes PR `n` fail its tests, by bisection."""
    ws.phase("culprit")
    def fails_with(k):
        wt, used = build_from(ws, f"{name}-{k}", seq[:k])
        try:
            if ws.merge(wt, n):
                return "conflict"
            return ws.test(wt)["outcome"] not in ("pass", "flaky")
        finally:
            ws.drop(wt)

    on_base = fails_with(0)
    if on_base == "conflict":
        return {"on_base": "conflict"}
    if on_base:
        return {"on_base": "fail"}
    lo, hi = 0, len(seq)
    if not fails_with(hi):
        return {"on_base": "pass", "culprit": None}
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fails_with(mid):
            hi = mid
        else:
            lo = mid
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
    blockers = [m["pr"] for m in merged if ws.conflicts(n, m["pr"])]
    res = {"blockers": blockers}
    wt = ws.tree(f"swap-{n}", tip)
    try:
        for m in reversed(merged):
            if m["pr"] in blockers and git(wt, "revert", "--no-edit", "-m", "1", m["commit"], check=False).returncode:
                git(wt, "revert", "--abort", check=False)
                return dict(res, outcome="revert_conflict")
        if ws.merge(wt, n):
            return dict(res, outcome="conflict")
        res.update(ws.test(wt))
    finally:
        ws.drop(wt)
    if res["outcome"] not in ("pass", "flaky"):
        rest = [m["pr"] for m in merged if m["pr"] not in blockers]
        res["culprit"] = find_culprit(ws, n, rest, f"cul-swap-{n}")
    return res


def place_probe(root, rel, data):
    """Write the probe file under `root`, refusing symlinks anywhere on the way and existing files."""
    path = root
    for part in Path(rel).parts[:-1]:
        path = path / part
        if path.is_symlink():
            return f"{rel}: a directory on the path is a symlink"
        path.mkdir(exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root / rel, flags, 0o644)
    except OSError as exc:
        return f"{rel}: {exc.strerror}"
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return None


def run_probe(ws, probe, target):
    ws.phase("probe")
    rel = Path(probe.get("dest") or Path(ws.cfg["test_prefix"]) / ("audit_probe_" + Path(probe["file"]).name))
    module = str(rel.with_suffix("")).replace(os.sep, ".")
    argv = fill(probe["cmd"], python=ws.cfg["python"], dest=rel, module=module)
    row = {"probe": probe["name"], "target": target}
    scratch, root = ws.export(ws.ref(target))
    try:
        refused = place_probe(root, rel, (ws.cfg["_dir"] / probe["file"]).read_bytes())
        if refused:
            return dict(row, outcome="error", message=refused)
        ws.count()
        rc, out, setup_failed = ws.execute(scratch, root, argv)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if setup_failed:
        return dict(row, outcome="fail", message=setup_failure(rc, out)["message"])
    if rc is None:
        return dict(row, outcome="fail", message="(timeout)")
    res = scan(ws.cfg, rc, out)
    message = res.get("message") or ("(no tests ran)" if res.get("failures") == ["(no tests ran)"] else "")
    return dict(row, outcome=res["outcome"], message=message)


def tool_versions(cfg):
    gv = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip().split()[2]
    tools = {"git": gv, "os": platform.system()}
    cmds = [cfg["test_cmd"], cfg.get("setup_cmd") or []] + [p["cmd"] for p in cfg["probes"]]
    if any("{python}" in a for cmd in cmds for a in cmd):
        tools["python"] = subprocess.run([cfg["python"], "-c", "import platform;print(platform.python_version())"],
                                         capture_output=True, text=True).stdout.strip()
    return tools


def cmd_build(cfg, args):
    check_git()
    snap = json.loads(Path(args.snapshot).read_text())
    check_probes(cfg, snap)
    bundle = args.bundle or str(Path(args.snapshot).with_suffix(".bundle"))
    print("note: this runs code from every PR it tries; use a disposable machine without credentials",
          file=sys.stderr)
    overrides = []
    if args.ignore_status and cfg.get("required_status"):
        overrides.append(f"required status check `{cfg['required_status']}` ignored")
        cfg = dict(cfg, required_status="")
    ws = Workspace(cfg, snap, bundle, args.work)
    missing = " and ".join(ws.incomplete_checkout(ws.base))
    if missing and not cfg.get("allow_incomplete_checkout"):
        shutil.rmtree(ws.work, ignore_errors=True)
        sys.exit(f"the base {snap['base_sha'][:7]} uses {missing}, which test runs would not contain; "
                 f"set allow_incomplete_checkout to run anyway")
    if missing:
        overrides.append(f"test runs lack {missing} (allow_incomplete_checkout)")
    seq, not_tried = classify(cfg, snap)
    print(f"merging {len(seq)} PRs in order", flush=True)
    steps, merged, tip, base_result, final = run_sequence(ws, seq)
    if base_result["outcome"] not in ("pass", "flaky"):
        shutil.rmtree(ws.work, ignore_errors=True)
        detail = ", ".join(base_result.get("failures", [])) + (": " + base_result["message"] if base_result.get("message") else "")
        sys.exit(f"tests fail on the base {snap['base_sha'][:7]} before any PR is merged ({detail}); "
                 f"fix the test environment or test_cmd first")
    held = [s for s in steps if s["outcome"] != "merged"]
    print(f"{len(merged)} merged, {len(held)} held; running follow-ups", flush=True)
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
        for fut in concurrent.futures.as_completed(jobs):
            kind, n = jobs[fut]
            try:
                result = fut.result()
            except Exception as exc:  # one broken job must not discard hours of other results
                error = f"{type(exc).__name__}: {exc}"
                result = {"swap": {"blockers": [], "outcome": "error", "message": error},
                          "culprit": {"error": error},
                          "probe": {"probe": n[0], "target": n[1], "outcome": "error", "message": error}}[kind]
            if kind == "swap":
                swaps[str(n)] = result
            elif kind == "culprit":
                culprits[str(n)] = result
            else:
                probes.append(result)
    order = {(p["name"], str(t)): i for p in cfg["probes"] for i, t in enumerate(p["targets"])}
    probes.sort(key=lambda r: ([p["name"] for p in cfg["probes"]].index(r["probe"]), order[(r["probe"], str(r["target"]))]))
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
    }
    out = Path(args.out or f"results/{snap['date']}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
    shutil.rmtree(ws.work, ignore_errors=True)
    print(f"wrote {out}")


# ---------------------------------------------------------------- report


def clean(text, table=False):
    """Text from test output, made safe inside a Markdown code span (and a table cell)."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).replace("`", "'")
    if len(text) > 200:
        text = text[:200] + "..."
    return text.replace("|", "\\|") if table else text


def refs(ns):
    return ", ".join(f"#{n}" for n in ns)


def code(names, table=False):
    return ", ".join(f"`{clean(x, table)}`" for x in names)


def render(cfg, res):
    snap = res["snapshot"]
    base = f"`{snap['base_ref']}` ({snap['base_sha'][:7]})"
    tools = res["tools"]
    steps = {s["pr"]: s for s in res["steps"]}
    swaps = {int(k): v for k, v in res["swaps"].items()}
    culprits = {int(k): v for k, v in res["culprits"].items()}
    tried = len(res["order"])
    L = []
    w = L.append
    w(f"**Merge test of the open PRs against {base}**")
    w("")
    rules = res.get("rules", {})
    for o in rules.get("overrides", []):
        w(f"**Override:** {o}.")
        w("")
    w("<details>")
    w("<summary><b>Procedure</b></summary>")
    w("")
    w(f"- **Base:** `{snap['base_ref']}` at `{snap['base_sha'][:7]}`, with PR heads fetched on {snap['date']}.")
    py = f"Python {tools['python']}, " if tools.get("python") else ""
    w(f"- **Environment:** {py}git {tools['git']}, {tools['os']}.")
    skipped = res["final_tests"].get("skipped", 0)
    note = f" {skipped} tests were skipped in the final run." if skipped else ""
    w(f"- **Test run:** `{res['test_cmd']}` after every merge.{note}")
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
    if res["base_tests"]["outcome"] == "flaky":
        w("**Warning:** the tests on the base are flaky (failed, then passed on re-run).")
        w("")
    elif res["base_tests"]["outcome"] != "pass":
        w(f"**Warning:** the tests do not pass on the base itself ({code(res['base_tests'].get('failures', []))}).")
        w("")
    merged = res["merged"]
    if tried:
        w(f"### Result: {len(merged)} of {tried} PRs merged together with every test passing")
        w("")
        w(refs(merged) or "None of them merged.")
    else:
        w("### Result: no PRs were eligible to try")
    w("")

    ok_swaps = [n for n in res["order"] if n in swaps and swaps[n].get("outcome") == "pass"]
    bad_swaps = [n for n in res["order"] if n in swaps and swaps[n].get("outcome") != "pass"]
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

    pairs = []
    if bad_swaps:
        w(f"### Tests fail even after swapping ({len(bad_swaps)})")
        w("")
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

    for n in res["order"]:
        c = culprits.get(n)
        if c and c.get("culprit"):
            pairs.append((c["culprit"], n, c["pair"]))
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

    other = []
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
    if other:
        w("### Other")
        w("")
        L.extend(other)
        w("")

    if res["probes"]:
        w("### Probes")
        w("")
        w("| Probe | Target | Result | Message |")
        w("|---|---|---|---|")
        for p in res["probes"]:
            t = f"`{snap['base_ref']}`" if p["target"] == "base" else f"#{p['target']}"
            w(f"| {p['probe']} | {t} | {p['outcome']} | {('`' + clean(p['message'], table=True) + '`') if p['message'] else ''} |")
        w("")

    labels = [
        ("excluded", "Excluded"),
        ("status", f"Required status check not successful"),
        ("draft", "Draft"),
        ("duplicate", "Same change as another open PR"),
        ("feature", "Feature, left for separate review"),
        ("no_tests", "Changes code without tests"),
    ]
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
    runs = res.get("test_runs")
    if runs:
        total = runs["sequence"] + runs["swap"] + runs["culprit"] + runs["probe"]
        w(f"**Test-suite runs:** {total} in total: {runs['sequence']} for the sequential build, "
          f"{runs['swap']} for swap tests, {runs['culprit']} for culprit searches and {runs['probe']} "
          f"for probes. The sequential build runs one at a time; the rest ran on {runs['workers']} "
          f"parallel workers. Running time scales with the time one test-suite run takes on the "
          f"machine used.")
        w("")
    w("Tests passing only shows that the PRs combine. It doesn't show each fix is correct.")
    return "\n".join(L) + "\n"


def cmd_report(cfg, args):
    res = json.loads(Path(args.results).read_text())
    text = render(cfg, res)
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)


def cmd_verify(cfg, args):
    res = json.loads(Path(args.results).read_text())
    a, b = render(cfg, res), render(cfg, json.loads(Path(args.results).read_text()))
    if a != b:
        sys.exit("FAIL: two renders of the same results differ")
    if args.golden:
        want = Path(args.golden).read_text()
        if a != want:
            import difflib
            sys.stdout.writelines(difflib.unified_diff(want.splitlines(True), a.splitlines(True), "golden", "rendered"))
            sys.exit("FAIL: report differs from the golden copy")
    print("PASS: report is stable" + (" and matches the golden copy" if args.golden else ""))


def cmd_estimate(cfg, args):
    """Time one test-suite run here and scale it by the run counts of a reference build."""
    snap = json.loads(Path(args.snapshot).read_text())
    ref = json.loads(Path(args.reference).read_text())
    runs = ref.get("test_runs")
    if not runs:
        sys.exit(f"{args.reference} has no test_runs counts; use results from a newer build")
    check_git()
    bundle = args.bundle or str(Path(args.snapshot).with_suffix(".bundle"))
    ws = Workspace(cfg, snap, bundle, args.work)
    wt = ws.tree("estimate", ws.base)
    argv = fill(cfg["test_cmd"], python=cfg["python"])
    start = time.monotonic()
    ws.test_once(wt, argv)
    one = time.monotonic() - start
    ws.drop(wt)
    shutil.rmtree(ws.work, ignore_errors=True)
    scale = len(classify(cfg, snap)[0]) / max(1, len(ref["order"]))
    seq = runs["sequence"] * scale * one
    rest = (runs["swap"] + runs["culprit"] + runs["probe"]) * scale * one / max(1, cfg["workers"])
    print(f"one test-suite run here: {one:.0f} s")
    print(f"scaled from the reference build ({len(ref['order'])} PRs tried; {scale:.2f}x for this snapshot):")
    print(f"  sequential build  ~{seq / 60:.0f} min")
    print(f"  follow-up jobs    ~{rest / 60:.0f} min on {cfg['workers']} workers")
    print(f"  total             ~{(seq + rest) / 60:.0f} min (merges and bisection rebuilds add some more)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--date")
    s.add_argument("--out", default="snapshots")
    s.add_argument("--work", default="work")
    b = sub.add_parser("build")
    b.add_argument("--snapshot", required=True)
    b.add_argument("--bundle")
    b.add_argument("--work", default="work/build")
    b.add_argument("--out")
    b.add_argument("--ignore-status", action="store_true",
                   help="try PRs whatever their required status check says (shown in the report)")
    r = sub.add_parser("report")
    r.add_argument("--results", required=True)
    r.add_argument("--out")
    v = sub.add_parser("verify")
    v.add_argument("--results", required=True)
    v.add_argument("--golden")
    e = sub.add_parser("estimate")
    e.add_argument("--snapshot", required=True)
    e.add_argument("--bundle")
    e.add_argument("--reference", required=True, help="results file of an earlier build")
    e.add_argument("--work", default="work/estimate")
    args = ap.parse_args()
    # report and verify read only a results file, so they need no project config.
    cfg = load_config(args.config) if args.cmd in ("snapshot", "build", "estimate") else {}
    {"snapshot": cmd_snapshot, "build": cmd_build, "report": cmd_report, "verify": cmd_verify, "estimate": cmd_estimate}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
