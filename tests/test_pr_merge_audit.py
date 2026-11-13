# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The pr-merge-audit Authors
#
# Test suite for pr_merge_audit.py. Run with: python3 -m unittest discover -s tests

"""Tests for pr_merge_audit.py. Real git repositories in temporary directories; nothing is mocked.

Fixtures build small repositories with known PRs, bundle them like a real snapshot, and run the
actual build, export and report code paths, so each test exercises the behaviour users depend on.
"""

import argparse
import http.server
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import shutil
import threading
import time
import unittest
from pathlib import Path

# Import the module from the repository root, so the tests run against the working copy.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pr_merge_audit as audit  # noqa: E402

# Reference results and report for the end-to-end fixture; see `verify`.
GOLDEN = ROOT / "tests" / "golden"


def sh(cwd, *argv):
    """Run a command in `cwd` with the tool's fixed git identity; return stripped stdout."""
    env = dict(os.environ, **audit.GIT_ENV)
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, message, remove=()):
    """Write `files`, remove paths in `remove`, commit everything and return the commit id."""
    for rel in remove:
        sh(repo, "git", "rm", "-rq", rel)
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sh(repo, "git", "add", rel)
    sh(repo, "git", "commit", "-qm", message)
    return sh(repo, "git", "rev-parse", "HEAD")


def branch(repo, name, files, remove=()):
    """Commit `files` on a new branch `name` off main, return to main and return the head commit."""
    sh(repo, "git", "checkout", "-q", "-b", name, "main")
    head = commit(repo, files, name, remove)
    sh(repo, "git", "checkout", "-q", "main")
    return head


# How the fixtures' tests are run and read; the tool itself assumes no test runner.
# Output patterns for Python unittest, used by every fixture; any runner works with its own patterns.
UNITTEST = {
    "test_cmd": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
    "failure_pattern": r"^(?:FAIL|ERROR): (\S+) \(",
    "count_pattern": r"^Ran (\d+) test",
    "skipped_pattern": r"skipped=(\d+)",
    "message_pattern": r"^\w*Error: (.+)$",
}
# Runs a single probe module with unittest.
PROBE_CMD = ["{python}", "-m", "unittest", "{module}"]

# The base test: app.add must add.
TEST_ADD = "import unittest, app\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(app.add(1, 2), 3)\n"


def make_project(tmp):
    """A small repository with PRs covering each outcome, plus its snapshot, bundle and config."""
    # Base: an app, a helper module and one passing test.
    repo = tmp / "src"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    base = commit(repo, {
        "app.py": "VERSION = 1\n\ndef add(a, b):\n    return a + b\n",
        "util.py": "def helper():\n    return 42\n",
        "tests/test_app.py": TEST_ADD,
    }, "base")
    # Template for a trivially passing test file per PR.
    t = "import unittest\nclass T(unittest.TestCase):\n    def test_{0}(self): pass\n"
    heads = {
        # 1 and 2 change the same line: a text conflict.
        1: branch(repo, "p1", {"app.py": "VERSION = 2\n\ndef add(a, b):\n    return a + b\n", "tests/test_p1.py": t.format("p1")}),
        2: branch(repo, "p2", {"app.py": "VERSION = 3\n\ndef add(a, b):\n    return a + b\n", "tests/test_p2.py": t.format("p2")}),
        # 3 removes util.helper and 4 starts testing it: each passes alone, together they fail.
        3: branch(repo, "p3", {"util.py": "", "tests/test_p3.py": t.format("p3")}),
        4: branch(repo, "p4", {"tests/test_helper.py":
                               "import unittest, util\nclass T(unittest.TestCase):\n"
                               "    def test_helper(self): self.assertEqual(util.helper(), 42)\n"}),
        # 5 fails if its tests can see any .git directory or file above them.
        5: branch(repo, "p5", {"tests/test_isolated.py":
                               "import os, unittest\nfrom pathlib import Path\n"
                               "class T(unittest.TestCase):\n"
                               "    def test_no_git(self):\n"
                               "        here = Path(os.getcwd())\n"
                               "        self.assertEqual([p for p in [here, *here.parents] if (p / '.git').exists()], [])\n"}),
        # 6 is excluded by configuration; 7 fails its required check; 8 changes code without tests.
        6: branch(repo, "p6", {"tests/test_p6.py": t.format("p6")}),
        7: branch(repo, "p7", {"tests/test_p7.py": t.format("p7")}),
        8: branch(repo, "p8", {"app.py": "VERSION = 1\n\ndef add(a, b):\n    return b + a\n"}),
        # 9 breaks a test on the base by itself.
        9: branch(repo, "p9", {"app.py": "VERSION = 1\n\ndef add(a, b):\n    return a - b\n", "tests/test_p9.py": t.format("p9")}),
    }
    # Publish the commits under the snapshot ref names and bundle them, as `snapshot` would.
    sh(repo, "git", "update-ref", "refs/audit/base", base)
    for n, head in heads.items():
        sh(repo, "git", "update-ref", f"refs/audit/pr/{n}", head)
    bundle = tmp / "snap.bundle"
    sh(repo, "git", "bundle", "create", "-q", str(bundle), "--glob=refs/audit")
    # Snapshot metadata for each PR, with changed files computed by git.
    prs = []
    for n, head in heads.items():
        files = audit.names(sh(repo, "git", "diff", "--name-only", "-z", f"{base}...{head}") + "\0")
        prs.append({"number": n, "title": f"p{n}", "head": head, "draft": False,
                    "status": "FAILURE" if n == 7 else "SUCCESS", "files": sorted(files)})
    snap = tmp / "snap.json"
    snap.write_text(json.dumps({"date": "test", "repo": "local/test", "base_ref": "main",
                                "base_sha": base, "prs": prs}))
    # A probe that checks util.helper still exists.
    (tmp / "probe.py").write_text(
        "import unittest, util\nclass P(unittest.TestCase):\n"
        "    def test_helper_exists(self): self.assertTrue(hasattr(util, 'helper'))\n")
    # Configuration: fixed order, one exclusion, one probe on the base and on PR 3.
    config = tmp / "config.json"
    config.write_text(json.dumps(dict(
        UNITTEST, repo="local/test", base="main", required_status="ci", workers=2,
        order=[1, 2, 3, 4, 5, 9], exclude={"6": "excluded for the test"},
        probes=[{"name": "helper exists", "file": "probe.py", "cmd": PROBE_CMD, "targets": ["base", 3]}],
    )))
    return audit.load_config(str(config)), snap, bundle


def build(cfg, snap, bundle, tmp, name, ignore_status=False, emit_branch=None, emit_repo=None):
    """Run the real `build` command on a fixture and return the parsed results file."""
    out = tmp / f"{name}.json"
    args = argparse.Namespace(snapshot=str(snap), bundle=str(bundle), work=str(tmp / f"work-{name}"), out=str(out),
                              ignore_status=ignore_status, emit_branch=emit_branch,
                              emit_repo=str(emit_repo) if emit_repo else None)
    audit.cmd_build(cfg, args)
    return json.loads(out.read_text())


class BuildEndToEnd(unittest.TestCase):
    """One full build of a fixture with merges, conflicts, failures and probes, checked end to end."""
    @classmethod
    def setUpClass(cls):
        """Build the fixture once, twice over, so determinism can be checked against a second run."""
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.cfg, snap, bundle = make_project(tmp)
        cls.res = build(cls.cfg, snap, bundle, tmp, "a")
        cls.again = build(cls.cfg, snap, bundle, tmp, "b")

    @classmethod
    def tearDownClass(cls):
        """Remove the fixture's temporary directory."""
        cls._tmp.cleanup()

    def test_tests_pass_on_base(self):
        """The fixture's base passes its own tests, so every later result is meaningful."""
        self.assertEqual(self.res["base_tests"]["outcome"], "pass")

    def test_merged_set(self):
        """Exactly the PRs expected to combine end up merged."""
        self.assertEqual(self.res["merged"], [1, 3, 5])

    def test_tests_cannot_see_git(self):
        """Test runs see no .git directory, so PR code cannot reach the repository."""
        self.assertIn(5, self.res["merged"])

    def test_text_conflict_is_swapped_in(self):
        """A PR that conflicts on text passes once its blocker is reverted."""
        swap = self.res["swaps"]["2"]
        self.assertEqual((swap["blockers"], swap["outcome"]), ([1], "pass"))

    def test_culprit_found_and_pair_confirmed(self):
        """Bisection names the PR that breaks another, and the pair test confirms it."""
        c = self.res["culprits"]["4"]
        self.assertEqual(c["culprit"], 3)
        self.assertEqual((c["pair"]["outcome"], c["pair"]["failures"]), ("fail", ["test_helper"]))

    def test_failure_on_base_alone_is_not_blamed_on_others(self):
        """A PR that fails on the base by itself gets no culprit."""
        self.assertEqual(self.res["culprits"]["9"], {"on_base": "fail"})

    def test_not_tried_reasons(self):
        """Excluded, failing-status and test-less PRs are reported with their reasons."""
        got = {r["pr"]: r["reason"] for r in self.res["not_tried"]}
        self.assertEqual(got, {6: "excluded", 7: "status", 8: "no_tests"})

    def test_probe_results(self):
        """Probes run on each target and report pass or fail."""
        got = [(p["target"], p["outcome"]) for p in self.res["probes"]]
        self.assertEqual(got, [("base", "pass"), (3, "fail")])

    def test_sequence_run_count(self):
        """The sequential build uses the expected number of test-suite runs."""
        # base 1 + #1 1 + #2 conflict 0 + #3 1 + #4 failing twice 2 + #5 1 + #9 failing twice 2
        self.assertEqual(self.res["test_runs"]["sequence"], 8)

    def test_same_inputs_same_results(self):
        """Two builds of the same snapshot give identical results, timings aside."""
        strip = lambda r: {k: v for k, v in r.items() if k != "timing"}
        self.assertEqual(strip(self.res), strip(self.again))

    def test_timing_matches_run_counts(self):
        """Recorded timings cover exactly the runs that were counted."""
        t = self.res["timing"]
        for phase in ("sequence", "swap", "culprit", "probe"):
            self.assertEqual(t["run_seconds"][phase]["count"], self.res["test_runs"][phase], phase)
        self.assertGreater(t["run_seconds"]["sequence"]["median"], 0)
        w = t["wall_seconds"]
        self.assertAlmostEqual(w["total"], w["sequence"] + w["follow_ups"], delta=0.2)

    def test_report_is_deterministic(self):
        """Rendering the two builds' results gives the same report."""
        self.assertEqual(audit.render(self.cfg, self.res), audit.render(self.cfg, self.again))


def refs_of(repo):
    """Every ref of `repo` with the commit it points at, for checking what a build changed."""
    return sh(repo, "git", "for-each-ref", "--format=%(refname) %(objectname)")


class EmitBranch(unittest.TestCase):
    """`build --emit-branch` keeps the merged, passing sequence as a new branch in a named repository."""
    @classmethod
    def setUpClass(cls):
        """Build PRs 1, 2 and 3 of the fixture (2 conflicts with 1) without the flag, then twice with it."""
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = cls.tmp = Path(cls._tmp.name)
        cls.cfg, cls.snap, cls.bundle = make_project(tmp)
        # Only PRs 1, 2 and 3 are tried; no probes, to keep the build short.
        cls.cfg.update(order=[1, 2, 3], probes=[], exclude={n: "not in this test" for n in (4, 5, 6, 9)})
        # The operator's clones: each has the fixture repository as its `origin`.
        cls.users = [tmp / "user-a", tmp / "user-b"]
        for user in cls.users:
            sh(tmp, "git", "clone", "-q", str(tmp / "src"), str(user))
        cls.origin_before = refs_of(tmp / "src")
        cls.user_before = refs_of(cls.users[0])
        cls.plain = build(cls.cfg, cls.snap, cls.bundle, tmp, "plain")
        cls.res = build(cls.cfg, cls.snap, cls.bundle, tmp, "a", emit_branch="refs/heads/integration",
                        emit_repo=cls.users[0])
        cls.again = build(cls.cfg, cls.snap, cls.bundle, tmp, "b", emit_branch="refs/heads/integration",
                          emit_repo=cls.users[1])

    @classmethod
    def tearDownClass(cls):
        """Remove the fixture's temporary directory."""
        cls._tmp.cleanup()

    def test_ref_points_at_the_final_passing_merge(self):
        """The branch is the merge of #3 on top of the merge of #1, and holds nothing of #2."""
        user, heads = self.users[0], {p["number"]: p["head"] for p in json.loads(self.snap.read_text())["prs"]}
        tip = sh(user, "git", "rev-parse", "refs/heads/integration")
        self.assertEqual(tip, self.res["emitted"]["commit"])
        self.assertEqual(sh(user, "git", "log", "--first-parent", "--format=%s", tip).splitlines(),
                         ["merge #3", "merge #1", "base"])
        for n, inside in ((1, True), (2, False), (3, True)):
            rc = subprocess.run(["git", "merge-base", "--is-ancestor", heads[n], tip], cwd=user).returncode
            self.assertEqual(rc == 0, inside, n)

    def test_manifest_lists_included_and_excluded_prs_with_reasons(self):
        """The results name the repository, ref and commit, and why each other PR is left out."""
        m = self.res["emitted"]
        self.assertEqual((m["ref"], m["included"]), ("refs/heads/integration", [1, 3]))
        self.assertEqual(Path(m["repo"]), (self.users[0] / ".git").resolve())
        self.assertEqual(m["excluded"], [{"pr": 2, "reason": "conflict"}]
                         + [{"pr": n, "reason": "not tried: excluded"} for n in (4, 5, 6)]
                         + [{"pr": 7, "reason": "not tried: status"}, {"pr": 8, "reason": "not tried: no_tests"},
                            {"pr": 9, "reason": "not tried: excluded"}])

    def test_only_the_new_branch_is_added_and_nothing_is_pushed(self):
        """The operator's clone gains exactly one ref, and its remote is untouched."""
        added = set(refs_of(self.users[0]).splitlines()) - set(self.user_before.splitlines())
        self.assertEqual(added, {"refs/heads/integration " + self.res["emitted"]["commit"]})
        self.assertEqual(refs_of(self.tmp / "src"), self.origin_before)
        self.assertFalse((self.users[0] / ".git" / "FETCH_HEAD").exists())

    def test_reflog_entry_has_the_callers_identity_and_time(self):
        """The new branch's reflog is not stamped with the fixed merge identity or its 2000-01-01 date."""
        log = (self.users[0] / ".git" / "logs" / "refs" / "heads" / "integration").read_text()
        self.assertIn("pr-merge-audit build", log)
        self.assertNotIn("audit@localhost", log)
        self.assertNotIn(" 946684800 ", log)

    def test_report_is_byte_identical_to_a_build_without_the_flag(self):
        """Emitting a branch changes nothing in the report."""
        self.assertNotIn("emitted", self.plain)
        self.assertEqual(audit.render(self.cfg, self.res), audit.render(self.cfg, self.plain))

    def test_emitted_commit_is_reproducible(self):
        """Fixed merge identity and dates give the same commit from two builds into two clones."""
        self.assertEqual(self.res["emitted"]["commit"], self.again["emitted"]["commit"])
        self.assertEqual(sh(self.users[1], "git", "rev-parse", "refs/heads/integration"), self.res["emitted"]["commit"])

    def test_existing_branch_stops_the_build_before_any_test(self):
        """An existing ref of that name is never overwritten: the build stops before it starts."""
        before = refs_of(self.users[0])
        with self.assertRaises(SystemExit) as stop:
            build(self.cfg, self.snap, self.bundle, self.tmp, "c", emit_branch="refs/heads/integration",
                  emit_repo=self.users[0])
        self.assertIn("already exists", str(stop.exception.code))
        self.assertFalse((self.tmp / "work-c").exists())
        self.assertFalse((self.tmp / "c.json").exists())
        self.assertEqual(refs_of(self.users[0]), before)

    def stops_early(self, ref, repo, message):
        """Assert that building with `ref` into `repo` stops before any test with `message`; return the message."""
        with self.assertRaises(SystemExit) as stop:
            build(self.cfg, self.snap, self.bundle, self.tmp, "d", emit_branch=ref, emit_repo=repo)
        self.assertIn(message, str(stop.exception.code))
        self.assertFalse((self.tmp / "work-d").exists())
        self.assertFalse((self.tmp / "d.json").exists())
        return str(stop.exception.code)

    def clone(self, name):
        """A fresh clone of the fixture repository, standing for another operator clone."""
        user = self.tmp / name
        sh(self.tmp, "git", "clone", "-q", str(self.tmp / "src"), str(user))
        return user

    def test_bad_arguments_stop_the_build_before_any_test(self):
        """Either flag without the other, a non-git directory or a ref that is not a full branch name stops early."""
        self.stops_early("refs/heads/x", None, "needs --emit-repo")
        self.stops_early(None, self.users[0], "needs --emit-branch")
        self.stops_early("refs/heads/x", self.tmp / "missing", "no such directory")
        self.stops_early("refs/heads/x", self.tmp, "fatal: not a git repository")  # git's own words
        self.stops_early("integration", self.users[0], "full branch name")
        self.stops_early("refs/heads/a..b", self.users[0], "full branch name")

    def test_ref_clashes_stop_the_build_before_any_test(self):
        """A ref git would refuse at the end (`x` beside `x/y`, or `main/z` under `main`) is refused up front."""
        user = self.clone("user-c")
        sh(user, "git", "branch", "x/y")
        self.assertIn("'refs/heads/x/y' exists", self.stops_early("refs/heads/x", user, "cannot be created"))
        self.assertIn("'refs/heads/main' exists", self.stops_early("refs/heads/main/z", user, "cannot be created"))

    def test_symbolic_ref_stops_the_build_before_any_test(self):
        """A dangling symbolic ref is refused, so git never creates the branch it points to instead."""
        user = self.clone("user-s")
        sh(user, "git", "symbolic-ref", "refs/heads/s", "refs/heads/elsewhere")
        self.stops_early("refs/heads/s", user, "symbolic ref to refs/heads/elsewhere")
        self.assertNotIn("refs/heads/elsewhere", refs_of(user))

    def test_other_object_format_stops_the_build_before_any_test(self):
        """A SHA-256 repository cannot hold the snapshot's SHA-1 commits; that is found before the build."""
        user = self.tmp / "user-256"
        sh(self.tmp, "git", "init", "-q", "--object-format=sha256", str(user))
        self.stops_early("refs/heads/integ", user, "uses sha256 object ids, but the snapshot uses sha1")

    def workspace(self):
        """A real build workspace loaded from the fixture's bundle, removed after the test."""
        ws = audit.Workspace(self.cfg, json.loads(self.snap.read_text()), self.bundle, self.tmp / "work-ws")
        self.addCleanup(audit.remove_tree, ws.work)
        return ws

    def test_ref_created_after_the_check_is_not_overwritten(self):
        """If the ref appears between the check and the write, git refuses and the ref keeps its commit."""
        user, ws = self.clone("user-o"), self.workspace()
        args = argparse.Namespace(emit_branch="refs/heads/late", emit_repo=str(user))
        gitdir = audit.check_emit(args, json.loads(self.snap.read_text()))
        sh(user, "git", "branch", "late", ws.base)
        with self.assertRaises(RuntimeError):
            audit.emit_branch(ws, gitdir, "refs/heads/late", ws.heads[1])
        self.assertEqual(sh(user, "git", "rev-parse", "refs/heads/late"), ws.base)

    def test_ref_turned_symbolic_after_the_check_is_refused(self):
        """If the ref becomes a dangling symbolic ref during the build, the write is refused and nothing created."""
        user, ws = self.clone("user-t"), self.workspace()
        args = argparse.Namespace(emit_branch="refs/heads/t", emit_repo=str(user))
        gitdir = audit.check_emit(args, json.loads(self.snap.read_text()))
        sh(user, "git", "symbolic-ref", "refs/heads/t", "refs/heads/elsewhere")
        with self.assertRaises(RuntimeError) as err:
            audit.emit_branch(ws, gitdir, "refs/heads/t", ws.heads[1])
        self.assertIn("symbolic ref", str(err.exception))
        self.assertNotIn("refs/heads/elsewhere", refs_of(user))

    def test_a_late_refusal_keeps_the_results(self):
        """A ref created during the build (here by the setup command) is refused; results are written without it."""
        user = self.clone("user-l")
        base = json.loads(self.snap.read_text())["base_sha"]
        make_ref = f"import subprocess; subprocess.run(['git', '-C', {str(user)!r}, 'update-ref', 'refs/heads/late', {base!r}])"
        cfg = dict(self.cfg, setup_cmd=["{python}", "-c", make_ref])
        with self.assertRaises(SystemExit) as stop:
            build(cfg, self.snap, self.bundle, self.tmp, "e", emit_branch="refs/heads/late", emit_repo=user)
        self.assertIn("was not created", str(stop.exception.code))
        self.assertEqual(json.loads((self.tmp / "e.json").read_text())["merged"], [1, 3])
        self.assertNotIn("emitted", json.loads((self.tmp / "e.json").read_text()))
        self.assertEqual(sh(user, "git", "rev-parse", "refs/heads/late"), base)


def small_project(tmp, base_test, status, base_files=None, pr_test=None, pr_files=None, **cfg):
    """One PR on top of a base whose test is `base_test`; the PR's status check is `status`."""
    repo = tmp / "src"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    base = commit(repo, dict({"app.py": "X = 1\n", "tests/test_base.py": base_test}, **(base_files or {})), "base")
    head = branch(repo, "p1", dict({"tests/test_p1.py": pr_test or "import unittest\nclass T(unittest.TestCase):\n    def test_p1(self): pass\n"}, **(pr_files or {})))
    sh(repo, "git", "update-ref", "refs/audit/base", base)
    sh(repo, "git", "update-ref", "refs/audit/pr/1", head)
    bundle = tmp / "snap.bundle"
    sh(repo, "git", "bundle", "create", "-q", str(bundle), "--glob=refs/audit")
    snap = tmp / "snap.json"
    snap.write_text(json.dumps({"date": "test", "repo": "local/test", "base_ref": "main", "base_sha": base,
                                "prs": [{"number": 1, "title": "p1", "head": head, "draft": False, "status": status,
                                         "files": ["tests/test_p1.py"]}]}))
    config = tmp / "config.json"
    config.write_text(json.dumps(dict(UNITTEST, repo="local/test", base="main", required_status="ci", workers=1, **cfg)))
    return audit.load_config(str(config)), snap, bundle


PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"


class SmallBuilds(unittest.TestCase):
    """Single-PR builds that exercise one rule or stop condition each."""
    def setUp(self):
        """Fresh temporary directory per test."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        """Remove the temporary directory."""
        self._tmp.cleanup()

    def test_failing_base_stops_the_build(self):
        """A base that fails its tests stops the build and leaves no files behind."""
        failing = "import unittest\nclass T(unittest.TestCase):\n    def test_base(self): self.fail('broken')\n"
        cfg, snap, bundle = small_project(self.tmp, failing, "SUCCESS")
        with self.assertRaises(SystemExit) as stop:
            build(cfg, snap, bundle, self.tmp, "a")
        self.assertIn("test_base", str(stop.exception.code))
        self.assertFalse((self.tmp / "a.json").exists())
        self.assertFalse((self.tmp / "work-a").exists())

    def test_nothing_eligible_renders_compactly(self):
        """A build with no eligible PRs still renders a short, clean report."""
        cfg, snap, bundle = small_project(self.tmp, PASSING, "FAILURE")
        text = audit.render(cfg, build(cfg, snap, bundle, self.tmp, "a"))
        self.assertIn("### Result: no PRs were eligible to try", text)
        self.assertIn("#1 (failure)", text)
        self.assertNotIn("(0)", text)
        self.assertNotIn("Serial landing", text)
        self.assertNotIn("\n\n\n", text)

    def test_ignore_status_is_applied_and_shown(self):
        """--ignore-status tries failing-status PRs and says so in the report."""
        cfg, snap, bundle = small_project(self.tmp, PASSING, "FAILURE")
        res = build(cfg, snap, bundle, self.tmp, "a", ignore_status=True)
        self.assertEqual(res["merged"], [1])
        self.assertIn("**Override:** required status check `ci` ignored.", audit.render(cfg, res))


    def test_lfs_base_stops_the_build_unless_allowed(self):
        """A base using Git LFS stops the build unless explicitly allowed."""
        lfs = {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"}
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS", lfs)
        with self.assertRaises(SystemExit) as stop:
            build(cfg, snap, bundle, self.tmp, "a")
        self.assertIn("Git LFS files", str(stop.exception.code))
        self.assertFalse((self.tmp / "work-a").exists())
        cfg["allow_incomplete_checkout"] = True
        res = build(cfg, snap, bundle, self.tmp, "b")
        self.assertEqual(res["merged"], [1])
        self.assertIn("**Override:** test runs lack Git LFS files", audit.render(cfg, res))

    def test_lfs_added_by_a_pr_stops_the_build_unless_allowed(self):
        """A PR that adds Git LFS stops the build unless explicitly allowed."""
        lfs = {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"}
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS", pr_files=lfs)
        with self.assertRaises(SystemExit) as stop:
            build(cfg, snap, bundle, self.tmp, "a")
        self.assertIn("#1 (Git LFS files)", str(stop.exception.code))
        self.assertFalse((self.tmp / "work-a").exists())
        cfg["allow_incomplete_checkout"] = True
        res = build(cfg, snap, bundle, self.tmp, "b")
        self.assertIn("**Override:** test runs of PRs #1 (Git LFS files) lack", audit.render(cfg, res))

    def test_probe_without_targets_stops_before_any_test(self):
        """A probe with no targets is rejected before any test runs."""
        (self.tmp / "probe.py").write_text(PASSING)
        probes = [{"name": "p", "file": "probe.py", "cmd": PROBE_CMD}]
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS", probes=probes)
        with self.assertRaises(SystemExit) as stop:
            build(cfg, snap, bundle, self.tmp, "a")
        self.assertIn("no targets", str(stop.exception.code))
        self.assertFalse((self.tmp / "work-a").exists())

    def test_bad_probe_target_stops_before_any_test(self):
        """A probe naming a PR not in the snapshot is rejected before any test runs."""
        (self.tmp / "probe.py").write_text(PASSING)
        probes = [{"name": "p", "file": "probe.py", "cmd": PROBE_CMD, "targets": ["base", 99]}]
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS", probes=probes)
        with self.assertRaises(SystemExit) as stop:
            build(cfg, snap, bundle, self.tmp, "a")
        self.assertIn("target 99", str(stop.exception.code))
        self.assertFalse((self.tmp / "work-a").exists())


    def test_flaky_pr_gets_no_culprit_search_and_one_label(self):
        """A flaky PR gets no culprit search and is listed once, as flaky."""
        marker = self.tmp / "ran-once"
        flaky = ("import os, unittest\nclass T(unittest.TestCase):\n    def test_p1(self):\n"
                 f"        if not os.path.exists({str(marker)!r}):\n"
                 f"            open({str(marker)!r}, 'w').close()\n            self.fail('first run')\n")
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS", pr_test=flaky)
        res = build(cfg, snap, bundle, self.tmp, "a")
        self.assertEqual((res["steps"][0]["outcome"], res["culprits"], res["test_runs"]["culprit"]), ("flaky", {}, 0))
        text = audit.render(cfg, res)
        self.assertIn("**Flaky (failed, then passed on re-run):** #1.", text)
        self.assertNotIn("no single culprit", text)

    def test_estimate_prints_serial_landing_for_the_snapshot_at_the_time_measured_here(self):
        """`estimate` uses N from the snapshot and the suite time it measured, not the reference build's."""
        import contextlib
        import io
        import re
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS")
        ref = build(cfg, snap, bundle, self.tmp, "ref")
        # A reference with other N and times: none of it may reach the serial-landing lines.
        ref["order"], ref["timing"] = [1, 2, 3, 4, 5], {"run_seconds": {"sequence": {"median": 999.0}}}
        (self.tmp / "ref.json").write_text(json.dumps(ref))
        args = argparse.Namespace(snapshot=str(snap), bundle=str(bundle), reference=str(self.tmp / "ref.json"),
                                  work=str(self.tmp / "work-estimate"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit.cmd_estimate(cfg, args)
        text = out.getvalue()
        one = float(re.search(r"N\(N\+1\)/2 = 1 full runs x ([0-9.]+) s", text).group(1))
        self.assertIn("\n".join(audit.serial_landing(1, one)) + "\n", text)
        self.assertIn("assuming one suite run at a time", text)
        self.assertNotIn("999", text)

    def test_environment_line_names_python_only_when_used(self):
        """The report names a Python version only if a command uses Python."""
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS")
        self.assertIn("python", audit.tool_versions(cfg))
        cfg = dict(cfg, test_cmd=[sys.executable, "-m", "unittest"])
        self.assertNotIn("python", audit.tool_versions(cfg))


    def test_a_failing_job_is_recorded_and_the_rest_kept(self):
        """A follow-up job that raises is recorded as an error; other results survive."""
        (self.tmp / "probe.py").write_text(PASSING)
        probes = [{"name": "p", "file": "probe.py", "cmd": PROBE_CMD, "targets": ["base", 2]}]
        cfg, snap, bundle = small_project(self.tmp, PASSING, "SUCCESS", probes=probes)
        data = json.loads(snap.read_text())
        data["prs"].append({"number": 2, "title": "gone", "head": "f" * 40, "draft": True, "status": "SUCCESS",
                            "files": []})  # head not in the bundle, so its export fails
        snap.write_text(json.dumps(data))
        res = build(cfg, snap, bundle, self.tmp, "a")
        self.assertEqual(res["merged"], [1])
        self.assertEqual([(p["target"], p["outcome"]) for p in res["probes"]], [("base", "pass"), (2, "error")])
        self.assertTrue(res["probes"][1]["message"])


class Packaging(unittest.TestCase):
    """Installation metadata stays consistent with the module."""
    def test_installed_module_and_console_script_use_the_distinctive_name(self):
        """A generic top-level module such as `audit` would collide with other installed packages."""
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(audit.__name__, "pr_merge_audit")
        self.assertIn('py-modules = ["pr_merge_audit"]', pyproject)
        self.assertIn('pr-merge-audit = "pr_merge_audit:main"', pyproject)

    def test_version_matches_the_package_metadata(self):
        """The module's __version__ matches pyproject.toml."""
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{audit.__version__}"', pyproject)

    def test_source_files_carry_a_license_header_and_documented_functions(self):
        """Every Python file starts with an SPDX header; every function and class has a docstring."""
        import ast
        for path in [ROOT / "pr_merge_audit.py", *sorted((ROOT / "tests").glob("*.py"))]:
            head = path.read_text(encoding="utf-8").splitlines()[:3]
            self.assertIn("# SPDX-License-Identifier: Apache-2.0", head, path.name)
        for path in (ROOT / "pr_merge_audit.py", ROOT / "tests" / "test_pr_merge_audit.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            undocumented = [n.name for n in ast.walk(tree)
                            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not ast.get_docstring(n)]
            self.assertEqual(undocumented, [], path.name)

    def test_tool_is_commented_densely(self):
        """At least one comment line per ten lines of the tool, so the code stays explained as it grows."""
        import io
        import tokenize
        src = (ROOT / "pr_merge_audit.py").read_text(encoding="utf-8")
        comments = sum(1 for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.COMMENT)
        self.assertGreaterEqual(comments * 10, len(src.splitlines()))


class Portability(unittest.TestCase):
    """Behaviour that must hold identically on Linux, macOS and Windows."""
    def test_archive_paths_absolute_on_any_platform_are_refused(self):
        """Absolute paths in any platform's syntax are recognised as absolute."""
        for name in ("/etc/x", "\\x", "C:x", "C:\\x", "\\\\host\\share\\x"):
            self.assertTrue(audit.absolute(name), name)
        self.assertFalse(audit.absolute("a/b"))

    def test_read_only_files_are_removed(self):
        """Directory trees holding read-only files can still be deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "t"
            (tree / "objects").mkdir(parents=True)
            (tree / "objects" / "pack").write_text("x")
            os.chmod(tree / "objects" / "pack", 0o444)
            audit.remove_tree(tree, strict=True)
            self.assertFalse(tree.exists())

    def test_written_files_are_utf8_with_lf_line_ends(self):
        """Output files are UTF-8 with LF line ends on every platform."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.md"
            audit.write_text(out, "caf\u00e9 \u2713\n")
            self.assertEqual(out.read_bytes(), "caf\u00e9 \u2713\n".encode("utf-8"))


class ProcessDocs(unittest.TestCase):
    """Claims in the contributor documentation stay true."""
    def test_contributing_claims_match_the_ci_workflow(self):
        """CONTRIBUTING.md says CI covers Linux, macOS and Windows on Python 3.8 and 3.13; keep that true."""
        doc = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        self.assertIn("Linux, macOS and Windows, Python 3.8 and 3.13", doc)
        for needle in ("ubuntu-", "macos-", "windows-", '"3.8"', '"3.13"'):
            self.assertIn(needle, ci)


class JobErrors(unittest.TestCase):
    """Failed follow-up jobs are recorded without losing other results; timing statistics are correct."""
    def test_every_kind_of_failed_job_is_recorded(self):
        """Swap, culprit and probe errors each produce the right result record."""
        exc = RuntimeError("boom")
        self.assertEqual(audit.job_error("swap", 5, exc), {"blockers": [], "outcome": "error", "message": "RuntimeError: boom"})
        self.assertEqual(audit.job_error("culprit", 5, exc), {"error": "RuntimeError: boom"})
        self.assertEqual(audit.job_error("probe", ("p", 2), exc)["target"], 2)

    def test_median_of_an_even_count_is_the_mean_of_the_middle_two(self):
        """Timing medians follow the standard definition."""
        self.assertEqual(audit.timing_summary({}, {"swap": [10, 100]})["run_seconds"]["swap"]["median"], 55)


class Classify(unittest.TestCase):
    """Which PRs are tried, in what order, and why the others are not."""
    def cfg(self, **kw):
        """Minimal configuration for classification, with overrides from `kw`."""
        cfg = {"test_prefix": "tests/", "order": [], "exclude": {}, "duplicates": {}, "features": {},
               "no_tests_ok": set(), "required_status": "ci"}
        cfg.update(kw)
        cfg.setdefault("test_files", "^tests/")
        return cfg

    def snap(self, *prs):
        """Snapshot of PRs given as (number, overrides) pairs; defaults to ready PRs that change tests."""
        return {"prs": [dict({"draft": False, "status": "SUCCESS", "files": ["tests/t.py"]}, number=n, **kw)
                        for n, kw in prs]}

    def test_order_then_number(self):
        """Configured order comes first; the rest follow by PR number."""
        eligible, _ = audit.classify(self.cfg(order=[3]), self.snap((1, {}), (2, {}), (3, {})))
        self.assertEqual(eligible, [3, 1, 2])

    def test_first_matching_reason_wins(self):
        """When several rules apply, the highest-priority reason is reported."""
        _, skipped = audit.classify(self.cfg(exclude={1: "x"}), self.snap((1, {"status": "FAILURE", "draft": True})))
        self.assertEqual(skipped[0]["reason"], "excluded")

    def test_status_ignored_when_not_required(self):
        """Without a required check, check state does not exclude PRs."""
        eligible, _ = audit.classify(self.cfg(required_status=""), self.snap((1, {"status": "FAILURE"})))
        self.assertEqual(eligible, [1])

    def test_test_files_pattern(self):
        """A custom test-file pattern decides which PRs count as tested."""
        cfg = self.cfg(test_files=r"_test\.py$")
        eligible, skipped = audit.classify(cfg, self.snap((1, {"files": ["pkg/a_test.py"]}),
                                                         (2, {"files": ["pkg/a.py"]})))
        self.assertEqual((eligible, skipped[0]["reason"]), ([1], "no_tests"))


class Helpers(unittest.TestCase):
    """Small pure functions: placeholder filling, output scanning, Markdown escaping."""
    def test_git_version(self):
        """git version strings are parsed, and unparseable ones give (0, 0)."""
        self.assertEqual(audit.git_version("git version 2.50.1 (Apple Git-155)"), (2, 50))
        self.assertEqual(audit.git_version("git version 2.45.1.windows.1"), (2, 45))
        self.assertLess(audit.git_version("git version 2.37.9"), audit.MIN_GIT)
        self.assertLess(audit.git_version(""), audit.MIN_GIT)

    def test_fill_leaves_other_braces(self):
        """Only named placeholders are replaced; other braces stay as written."""
        self.assertEqual(audit.fill(["{python}", "{1..3}", '{"a": 1}', "{dest}"], python="py", dest=Path("t/x.py")),
                         ["py", "{1..3}", '{"a": 1}', "t/x.py"])

    def test_scan_uses_group_one_and_ignores_non_numbers(self):
        """Output scanning reads capture group 1 and tolerates non-numeric counts."""
        cfg = {"failure_pattern": r"^(\S+) (FAILED|ERROR)", "count_pattern": r"^ran: (\S+)"}
        res = audit.scan(cfg, 1, "a.b FAILED\nc.d ERROR\nran: many\n")
        self.assertEqual((res["failures"], res["ran"]), (["a.b", "c.d"], 0))

    def test_job_errors_render(self):
        """Error results render as readable report lines."""
        res = json.loads((GOLDEN / "results.json").read_text())
        a, b = (str(x) for x in res["order"][-2:])
        res["swaps"][a] = {"blockers": [], "outcome": "error", "message": "RuntimeError: boom"}
        res["culprits"][b] = {"error": "RuntimeError: bang"}
        text = audit.render({}, res)
        self.assertIn(f"**#{a}:** the swap test could not run (`RuntimeError: boom`).", text)
        self.assertIn(f"**Culprit search could not run:** #{b} (`RuntimeError: bang`).", text)

    def test_serial_landing_in_the_report_is_counts_only(self):
        """The report gives N, N(N+1)/2 runs and this build's runs, and no times, so it stays deterministic."""
        res = json.loads((GOLDEN / "results.json").read_text())
        text = audit.render({}, res)
        self.assertIn("landing the N = 6 PRs tried here one at a time, re-testing every PR still open after each "
                      "landing, would take N(N+1)/2 = 21 full test-suite runs, against the 19 this build counted "
                      "(1.1 times as many).", text)
        # Measured times, however different, never reach the report.
        res["timing"] = {"run_seconds": {"sequence": {"count": 8, "median": 123.45}}, "wall_seconds": {"total": 9876.5}}
        self.assertEqual(audit.render({}, res), text)

    def test_serial_landing_ratio_only_when_serial_costs_more(self):
        """With as many or more runs counted than serial landing needs, the report gives no ratio."""
        res = json.loads((GOLDEN / "results.json").read_text())
        for sequence, counted in ((10, 21), (30, 41)):  # 21 is exactly the serial count
            res["test_runs"]["sequence"] = sequence
            text = audit.render({}, res)
            self.assertIn(f"N(N+1)/2 = 21 full test-suite runs, against the {counted} this build counted. ", text)
            self.assertNotIn("times as many", text)

    def test_serial_landing_hours(self):
        """N(N+1)/2 runs in machine-hours and N runs as hours to land, at the given suite time."""
        self.assertEqual(audit.serial_landing(6, 120.0), [
            "serial landing of the N = 6 PRs this snapshot would try, one at a time (estimate):",
            "  N(N+1)/2 = 21 full runs x 120.0 s  ~0.7 machine-hours",
            "  N x 120.0 s                 ~0.2 hours to land them, assuming one suite run at a time "
            "(each landing waits for its run)"])

    def test_empty_blockers_render_in_words(self):
        """A swap with no blockers is described in words, not as an empty list."""
        res = json.loads((GOLDEN / "results.json").read_text())
        n = str(res["order"][-1])
        res["swaps"][n] = {"blockers": [], "outcome": "conflict"}
        text = audit.render({}, res)
        self.assertIn(f"**#{n}:** conflicts with the combined build only; still conflicts", text)
        self.assertNotIn("with ;", text)

    def test_names_keeps_spaces(self):
        """NUL-separated path lists keep spaces inside names."""
        self.assertEqual(audit.names("a b.py\0c.py\0"), ["a b.py", "c.py"])

    def test_clean_cannot_leave_code_span_or_cell(self):
        """Escaped test output cannot break out of a code span or table cell."""
        text = audit.clean("x` | @a ![i](http://e) \n### h" + "y" * 300, table=True)
        self.assertNotIn("`", text)
        self.assertNotIn("\n", text)
        self.assertNotRegex(text, r"(?<!\\)\|")
        self.assertLessEqual(len(text), 203 + text.count("\\"))

    def test_scan_without_patterns_uses_exit_status(self):
        """Without output patterns, only the exit status decides the outcome."""
        self.assertEqual(audit.scan({}, 0, "anything")["outcome"], "pass")
        res = audit.scan({}, 3, "anything")
        self.assertEqual((res["outcome"], res["failures"]), ("fail", ["(exit status 3)"]))

    def test_scan_count_pattern_requires_a_test(self):
        """With a count pattern, a zero-test run is not a pass."""
        cfg = {"count_pattern": r"^(\d+) passed", "failure_pattern": r"^FAILED (\S+)"}
        self.assertEqual(audit.scan(cfg, 0, "4 passed")["outcome"], "pass")
        self.assertEqual(audit.scan(cfg, 0, "0 passed")["failures"], ["(no tests ran)"])
        self.assertEqual(audit.scan(cfg, 1, "FAILED a::b\nFAILED a::c\n")["failures"], ["a::b", "a::c"])

    def test_read_capped(self):
        """Oversized output keeps its head and tail and is marked as truncated."""
        with tempfile.TemporaryDirectory() as d:
            small, big = Path(d) / "s", Path(d) / "b"
            small.write_text("ok")
            big.write_bytes(b"a" * audit.OUTPUT_CAP + b"END")
            self.assertEqual(audit.read_capped(small), "ok")
            out = audit.read_capped(big)
            self.assertIn("[output truncated]", out)
            self.assertTrue(out.endswith("END"))
            self.assertLess(len(out), audit.OUTPUT_CAP + 100)


class Containment(unittest.TestCase):
    """Archive members that could write outside the export directory are refused."""
    def setUp(self):
        """An export directory and a directory outside it, per test."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def tearDown(self):
        """Remove both directories."""
        self._tmp.cleanup()

    def member(self, name, kind=tarfile.REGTYPE, link=""):
        """A tar member of the given type, as `git archive` would produce."""
        m = tarfile.TarInfo(name)
        m.type, m.linkname = kind, link
        return m

    def test_safe_member(self):
        """Each kind of tar member is accepted or refused as expected."""
        (self.root / "up").symlink_to(self.outside)
        cases = {
            ("a/b.py", tarfile.REGTYPE, ""): True,
            ("a/link", tarfile.SYMTYPE, "../b.py"): True,
            ("/etc/x", tarfile.REGTYPE, ""): False,
            ("../x", tarfile.REGTYPE, ""): False,
            ("a/link", tarfile.SYMTYPE, "../../x"): False,
            ("a/link", tarfile.SYMTYPE, "/etc"): False,
            ("up/x.py", tarfile.REGTYPE, ""): False,
            ("a/h", tarfile.LNKTYPE, "a/b.py"): False,
        }
        for (name, kind, link), want in cases.items():
            with self.subTest(name=name, link=link):
                self.assertIs(audit.safe_member(self.root, self.member(name, kind, link)), want)

    def test_probe_written_into_missing_directory(self):
        """A probe can be placed in a directory that does not exist yet."""
        self.assertIsNone(audit.place_probe(self.root, Path("tests/p.py"), b"x"))
        self.assertEqual((self.root / "tests" / "p.py").read_bytes(), b"x")

    def test_probe_refuses_symlinked_directory(self):
        """A probe is not written through a symlinked directory."""
        (self.root / "tests").symlink_to(self.outside)
        self.assertIsNotNone(audit.place_probe(self.root, Path("tests/p.py"), b"x"))
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_probe_refuses_existing_file_or_link(self):
        """A probe never overwrites a file or link the PR placed."""
        (self.root / "tests").mkdir()
        target = self.outside / "victim"
        target.write_text("keep")
        (self.root / "tests" / "p.py").symlink_to(target)
        self.assertIsNotNone(audit.place_probe(self.root, Path("tests/p.py"), b"x"))
        self.assertEqual(target.read_text(), "keep")

    def test_work_directory_needs_marker(self):
        """A non-empty work directory without the marker is never cleared."""
        work = self.tmp / "work"
        work.mkdir()
        (work / "keep.txt").write_text("keep")
        with self.assertRaises(SystemExit):
            audit.Workspace({}, {"base_sha": "", "prs": []}, "unused", work)
        self.assertTrue((work / "keep.txt").exists())


def git_in(repo, *argv, data=None):
    """Run git in `repo`, optionally feeding `data` on stdin; return stripped stdout."""
    env = dict(os.environ, **audit.GIT_ENV)
    # bytes, not text: in text mode Windows would turn each "\n" of `data` into "\r\n"
    return subprocess.run(["git", *argv], cwd=repo, env=env, input=None if data is None else data.encode(),
                          check=True, capture_output=True).stdout.decode().strip()


def mktree(repo, entries):
    """Tree from (mode, name, blob text or subtree id) rows, so a test can hold any names and links."""
    rows = []
    for mode, name, value in entries:
        kind = {"040000": "tree", "160000": "commit"}.get(mode, "blob")
        oid = value if kind != "blob" else git_in(repo, "hash-object", "-w", "--stdin", data=value)
        rows.append(f"{mode} {kind} {oid}\t{name}\n")
    return git_in(repo, "mktree", data="".join(rows))


class TreeFixture:
    """A one-commit repository built from raw trees, bundled and opened as a Workspace."""

    def setUp(self):
        """A scratch repository in which tests build arbitrary trees."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.src = self.tmp / "src"
        self.src.mkdir()
        git_in(self.src, "init", "-q")
        self.victim = self.tmp / "victim.txt"
        self.victim.write_text("keep")

    def tearDown(self):
        """Remove the scratch repository."""
        self._tmp.cleanup()

    def workspace(self, tree, **cfg):
        """Commit `tree`, bundle it as a snapshot base and open it as a Workspace."""
        head = git_in(self.src, "commit-tree", tree, "-m", "pr")
        git_in(self.src, "update-ref", "refs/audit/base", head)
        bundle = self.tmp / "b.bundle"
        git_in(self.src, "bundle", "create", "-q", str(bundle), "--glob=refs/audit")
        cfg = dict(UNITTEST, test_timeout=60, python=sys.executable, **cfg)
        return audit.Workspace(cfg, {"base_sha": head, "prs": []}, bundle, self.tmp / "work"), head

    def pr_tree(self, files):
        """Tree with `files` under `tests/`."""
        return mktree(self.src, [("040000", "tests", mktree(self.src, [("100644", k, v) for k, v in files.items()]))])


class HostileTrees(TreeFixture, unittest.TestCase):
    """Commits a PR author could push, run through the real export and test steps."""

    def test_chained_link_is_pruned_and_never_written_through(self):
        """A chain of links that escapes the export is removed and never followed."""
        # sub/s points at the copy's root, so the text of T cancels out while on disk it climbs to /.
        up = "/".join(["sub/s"] * 20 + [".."] * 40)
        sub = mktree(self.src, [("120000", "s", "..")])
        tree = mktree(self.src, [("120000", "T", up + str(self.victim)), ("040000", "sub", sub),
                                 ("100644", "t", "overwritten\n")])
        ws, head = self.workspace(tree)
        scratch, root = ws.export(head)
        try:
            self.assertEqual(self.victim.read_text(), "keep")
            self.assertFalse(os.path.lexists(root / "T") and os.path.realpath(root / "T") == str(self.victim))
            for d, dirs, files in os.walk(root):
                for name in dirs + files:
                    real = os.path.realpath(os.path.join(d, name))
                    self.assertTrue(real.startswith(os.path.realpath(root)), real)
        finally:
            shutil.rmtree(scratch)

    def test_export_ignore_cannot_hide_files(self):
        """A PR's .gitattributes cannot drop files from the test copy."""
        tree = mktree(self.src, [("100644", ".gitattributes", "tests/test_hidden.py export-ignore\n"),
                                 ("040000", "tests", mktree(self.src, [("100644", "test_hidden.py", PASSING)]))])
        ws, head = self.workspace(tree)
        scratch, root = ws.export(head)
        try:
            self.assertTrue((root / "tests" / "test_hidden.py").is_file())
        finally:
            shutil.rmtree(scratch)

    def test_early_exit_is_not_a_pass(self):
        """A suite that exits 0 before running tests is not reported as passing."""
        ws, head = self.workspace(self.pr_tree({"test_x.py": "import os\nos._exit(0)\n"}))
        wt = ws.tree("t", head)
        res = ws.test_once(wt, [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        self.assertEqual((res["outcome"], res["failures"]), ("fail", ["(no tests ran)"]))

    def test_home_and_temp_directories_are_private_on_every_platform(self):
        """Test runs see private home and temp directories, not the operator's."""
        real_home, real_tmp = os.path.expanduser("~"), tempfile.gettempdir()
        check = ("import os, tempfile, unittest\n"
                 "class T(unittest.TestCase):\n"
                 "    def test_private(self):\n"
                 f"        self.assertNotEqual(os.path.expanduser('~'), {real_home!r})\n"
                 f"        self.assertNotEqual(tempfile.gettempdir(), {real_tmp!r})\n")
        ws, head = self.workspace(self.pr_tree({"test_private.py": check}))
        wt = ws.tree("t", head)
        res = ws.test_once(wt, [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        self.assertEqual(res["outcome"], "pass", res.get("failures"))

    def test_background_processes_are_stopped(self):
        """Processes a test leaves running are stopped when the run ends."""
        pidfile = self.tmp / "pid"
        start = ("import subprocess, sys, unittest\n"
                 "class T(unittest.TestCase):\n"
                 "    def test_spawn(self):\n"
                 "        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                 f"        open({str(pidfile)!r}, 'w').write(str(p.pid))\n")
        ws, head = self.workspace(self.pr_tree({"test_spawn.py": start}))
        wt = ws.tree("t", head)
        self.assertEqual(ws.test_once(wt, [sys.executable, "-m", "unittest", "discover", "-s", "tests"])["outcome"],
                         "pass")
        pid = int(pidfile.read_text())
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            self.fail("background process outlived the test run")


def alive(pid):
    """Whether process `pid` still runs. On Windows os.kill(pid, 0) would terminate it, so ask tasklist."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Unsupported(TreeFixture, unittest.TestCase):
    """Setups the tool cannot test faithfully are detected, not silently mis-read."""

    def test_gitlink_and_nested_lfs_are_detected(self):
        """Submodules and nested LFS attributes are both detected."""
        lfs = mktree(self.src, [("100644", ".gitattributes", "*.bin filter=lfs -text\n")])
        tree = mktree(self.src, [("160000", "vendor", "1" * 40), ("040000", "assets", lfs)])
        ws, head = self.workspace(tree)
        self.assertEqual(ws.incomplete_checkout(head), ["submodules", "Git LFS files"])

    def test_plain_tree_is_complete(self):
        """An ordinary tree is reported as complete."""
        ws, head = self.workspace(self.pr_tree({"test_ok.py": PASSING}))
        self.assertEqual(ws.incomplete_checkout(head), [])

    def test_conflict_with_base_is_not_called_a_test_failure(self):
        """A PR that conflicts with the base is reported as a conflict, not a failure."""
        ws, head = self.workspace(self.pr_tree({"test_ok.py": PASSING}))
        ws.heads[5] = git_in(self.src, "commit-tree", self.pr_tree({"test_ok.py": PASSING + "# changed\n"}), "-m", "unrelated")
        self.assertEqual(audit.find_culprit(ws, 5, [], "c"), {"on_base": "conflict"})

    def test_merge_tree_error_is_not_read_as_no_conflict(self):
        """A git error during conflict detection raises instead of reading as clean."""
        ws, head = self.workspace(self.pr_tree({"test_ok.py": PASSING}))
        ws.heads[5] = "0" * 40
        with self.assertRaises(RuntimeError):
            ws.conflicts("base", 5)


ENV_TEST = ("import os, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_env(self):\n"
            "        self.assertEqual(os.environ.get('AUDIT_PASSED'), 'from-caller')\n"
            "        self.assertEqual(os.environ.get('AUDIT_FIXED'), 'from-config')\n"
            "        self.assertIsNone(os.environ.get('AUDIT_NOT_LISTED'))\n")
SETUP_TEST = "import unittest\nclass T(unittest.TestCase):\n    def test_setup(self): import generated\n"
UNITTEST_ARGV = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]


class Toolchain(TreeFixture, unittest.TestCase):
    """pass_env, env and setup_cmd reach the real test process."""

    def test_pass_env_and_env_reach_tests(self):
        """Variables from pass_env and env reach the test process."""
        os.environ.update(AUDIT_PASSED="from-caller", AUDIT_NOT_LISTED="secret")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AUDIT_PASSED", "AUDIT_NOT_LISTED")])
        ws, head = self.workspace(self.pr_tree({"test_env.py": ENV_TEST}),
                                  pass_env=["AUDIT_PASSED"], env={"AUDIT_FIXED": "from-config"})
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "pass")

    def test_unlisted_variables_stay_out(self):
        """Variables not listed are withheld from the test process."""
        os.environ["AUDIT_PASSED"] = "from-caller"
        self.addCleanup(os.environ.pop, "AUDIT_PASSED", None)
        ws, head = self.workspace(self.pr_tree({"test_env.py": ENV_TEST}))
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "fail")

    def test_setup_cmd_runs_first_in_the_same_copy(self):
        """setup_cmd runs before the tests, in the same copy."""
        setup = ["{python}", "-c", "open('tests/generated.py', 'w').write('X = 1\\n')"]
        ws, head = self.workspace(self.pr_tree({"test_setup.py": SETUP_TEST}), setup_cmd=setup)
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "pass")

    def test_braces_in_commands_are_kept(self):
        """Braces in commands that are not placeholders reach the process unchanged."""
        setup = ["{python}", "-c", "d = {'a': 1}; open('tests/generated.py', 'w').write('X = 1')"]
        ws, head = self.workspace(self.pr_tree({"test_setup.py": SETUP_TEST}), setup_cmd=setup)
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "pass")

    def test_failed_setup_is_a_failure_and_tests_do_not_run(self):
        """A failed setup is a failure, and the tests are skipped."""
        setup = ["{python}", "-c", "import sys; print('no toolchain'); sys.exit(3)"]
        ws, head = self.workspace(self.pr_tree({"test_ok.py": PASSING}), setup_cmd=setup)
        res = ws.test_once(ws.tree("t", head), UNITTEST_ARGV)
        self.assertEqual((res["outcome"], res["failures"]), ("fail", ["(setup failed)"]))
        self.assertEqual(res["message"], "setup_cmd exit status 3: no toolchain")

    def test_config_rejects_malformed_keys(self):
        """Malformed pass_env, env and setup_cmd values are rejected at load time."""
        for bad in ({"pass_env": "PATH"}, {"env": {"A": 1}}, {"setup_cmd": "make"}, {"setup_cmd": []}):
            path = self.tmp / "c.json"
            path.write_text(json.dumps(dict(UNITTEST, **bad)))
            with self.assertRaises(SystemExit, msg=bad):
                audit.load_config(path)


class TokenRedirect(unittest.TestCase):
    """The API token is never forwarded when a request is redirected to another host."""
    def test_token_not_sent_to_redirect_target(self):
        """The API token is dropped when the server redirects to another host."""
        seen = {}

        class Target(http.server.BaseHTTPRequestHandler):
            """Stand-in for the redirect target; records the Authorization header it receives."""
            def do_GET(self):
                """Answer with an empty JSON object."""
                seen["auth"] = self.headers.get("Authorization")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                """Keep test output free of request logs."""
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            """Stand-in for the API host; records the header, then redirects to the target."""
            def do_GET(self):
                """Answer with a redirect to the target host."""
                seen["first"] = self.headers.get("Authorization")
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
                self.end_headers()

        first = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, first):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        old = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "secret"
        try:
            audit.api({"api": f"http://127.0.0.1:{first.server_port}"}, "/y")
        finally:
            if old is None:
                del os.environ["GITHUB_TOKEN"]
            else:
                os.environ["GITHUB_TOKEN"] = old
        self.assertEqual((seen["first"], seen["auth"]), ("Bearer secret", None))


class RateLimit(unittest.TestCase):
    """API errors end the run with an actionable message, including rate-limit reset times."""
    def test_rate_limit_exits_with_a_hint(self):
        """A rate-limited API call exits with the reset time and a token hint."""
        class Limited(http.server.BaseHTTPRequestHandler):
            """Stand-in for an API that answers every request with a rate-limit error."""
            def do_GET(self):
                """Answer with HTTP 403 and exhausted rate-limit headers."""
                self.send_response(403, "rate limit exceeded")
                self.send_header("X-RateLimit-Remaining", "0")
                self.send_header("X-RateLimit-Reset", "0")
                self.end_headers()

            def log_message(self, *a):
                """Keep test output free of request logs."""
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Limited)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        saved = {k: os.environ.pop(k) for k in ("GITHUB_TOKEN", "GH_TOKEN") if k in os.environ}
        self.addCleanup(os.environ.update, saved)
        with self.assertRaises(SystemExit) as stop:
            audit.api({"api": f"http://127.0.0.1:{server.server_port}"}, "/y")
        msg = str(stop.exception.code)
        for part in ("HTTP 403", "rate limit reached", "1970-01-01 00:00 UTC", "GITHUB_TOKEN"):
            self.assertIn(part, msg)


class Golden(unittest.TestCase):
    """Results of a build of the fixture in make_project, and the report they render to."""

    def test_golden_report(self):
        """The stored results still render exactly as the reviewed golden report."""
        res = json.loads((GOLDEN / "results.json").read_text())
        self.assertEqual(audit.render({}, res), (GOLDEN / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
