"""Tests for audit.py. Real git repositories in temporary directories; nothing is mocked."""

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import audit  # noqa: E402

GOLDEN = ROOT / "tests" / "golden"


def sh(cwd, *argv):
    env = dict(os.environ, **audit.GIT_ENV)
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, message, remove=()):
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
    sh(repo, "git", "checkout", "-q", "-b", name, "main")
    head = commit(repo, files, name, remove)
    sh(repo, "git", "checkout", "-q", "main")
    return head


# How the fixtures' tests are run and read; the tool itself assumes no test runner.
UNITTEST = {
    "test_cmd": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
    "failure_pattern": r"^(?:FAIL|ERROR): (\S+) \(",
    "count_pattern": r"^Ran (\d+) test",
    "skipped_pattern": r"skipped=(\d+)",
    "message_pattern": r"^\w*Error: (.+)$",
}
PROBE_CMD = ["{python}", "-m", "unittest", "{module}"]

TEST_ADD = "import unittest, app\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(app.add(1, 2), 3)\n"


def make_project(tmp):
    """A small repository with PRs covering each outcome, plus its snapshot, bundle and config."""
    repo = tmp / "src"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    base = commit(repo, {
        "app.py": "VERSION = 1\n\ndef add(a, b):\n    return a + b\n",
        "util.py": "def helper():\n    return 42\n",
        "tests/test_app.py": TEST_ADD,
    }, "base")
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
        6: branch(repo, "p6", {"tests/test_p6.py": t.format("p6")}),
        7: branch(repo, "p7", {"tests/test_p7.py": t.format("p7")}),
        8: branch(repo, "p8", {"app.py": "VERSION = 1\n\ndef add(a, b):\n    return b + a\n"}),
        # 9 breaks a test on the base by itself.
        9: branch(repo, "p9", {"app.py": "VERSION = 1\n\ndef add(a, b):\n    return a - b\n", "tests/test_p9.py": t.format("p9")}),
    }
    sh(repo, "git", "update-ref", "refs/audit/base", base)
    for n, head in heads.items():
        sh(repo, "git", "update-ref", f"refs/audit/pr/{n}", head)
    bundle = tmp / "snap.bundle"
    sh(repo, "git", "bundle", "create", "-q", str(bundle), "--glob=refs/audit")
    prs = []
    for n, head in heads.items():
        files = audit.names(sh(repo, "git", "diff", "--name-only", "-z", f"{base}...{head}") + "\0")
        prs.append({"number": n, "title": f"p{n}", "head": head, "draft": False,
                    "status": "FAILURE" if n == 7 else "SUCCESS", "linked": [], "files": sorted(files)})
    snap = tmp / "snap.json"
    snap.write_text(json.dumps({"date": "test", "repo": "local/test", "base_ref": "main",
                                "base_sha": base, "prs": prs}))
    (tmp / "probe.py").write_text(
        "import unittest, util\nclass P(unittest.TestCase):\n"
        "    def test_helper_exists(self): self.assertTrue(hasattr(util, 'helper'))\n")
    config = tmp / "config.json"
    config.write_text(json.dumps(dict(
        UNITTEST, repo="local/test", base="main", required_status="ci", workers=2,
        order=[1, 2, 3, 4, 5, 9], exclude={"6": "excluded for the test"},
        probes=[{"name": "helper exists", "file": "probe.py", "cmd": PROBE_CMD, "targets": ["base", 3]}],
    )))
    return audit.load_config(str(config)), snap, bundle


def build(cfg, snap, bundle, tmp, name, ignore_status=False):
    out = tmp / f"{name}.json"
    args = argparse.Namespace(snapshot=str(snap), bundle=str(bundle), work=str(tmp / f"work-{name}"), out=str(out),
                              ignore_status=ignore_status)
    audit.cmd_build(cfg, args)
    return json.loads(out.read_text())


class BuildEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.cfg, snap, bundle = make_project(tmp)
        cls.res = build(cls.cfg, snap, bundle, tmp, "a")
        cls.again = build(cls.cfg, snap, bundle, tmp, "b")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_tests_pass_on_base(self):
        self.assertEqual(self.res["base_tests"]["outcome"], "pass")

    def test_merged_set(self):
        self.assertEqual(self.res["merged"], [1, 3, 5])

    def test_tests_cannot_see_git(self):
        self.assertIn(5, self.res["merged"])

    def test_text_conflict_is_swapped_in(self):
        swap = self.res["swaps"]["2"]
        self.assertEqual((swap["blockers"], swap["outcome"]), ([1], "pass"))

    def test_culprit_found_and_pair_confirmed(self):
        c = self.res["culprits"]["4"]
        self.assertEqual(c["culprit"], 3)
        self.assertEqual((c["pair"]["outcome"], c["pair"]["failures"]), ("fail", ["test_helper"]))

    def test_failure_on_base_alone_is_not_blamed_on_others(self):
        self.assertEqual(self.res["culprits"]["9"], {"on_base": "fail"})

    def test_not_tried_reasons(self):
        got = {r["pr"]: r["reason"] for r in self.res["not_tried"]}
        self.assertEqual(got, {6: "excluded", 7: "status", 8: "no_tests"})

    def test_probe_results(self):
        got = [(p["target"], p["outcome"]) for p in self.res["probes"]]
        self.assertEqual(got, [("base", "pass"), (3, "fail")])

    def test_sequence_run_count(self):
        # base 1 + #1 1 + #2 conflict 0 + #3 1 + #4 failing twice 2 + #5 1 + #9 failing twice 2
        self.assertEqual(self.res["test_runs"]["sequence"], 8)

    def test_same_inputs_same_results(self):
        self.assertEqual(self.res, self.again)

    def test_report_is_deterministic(self):
        self.assertEqual(audit.render(self.cfg, self.res), audit.render(self.cfg, self.again))


def small_project(tmp, base_test, status):
    """One PR on top of a base whose test is `base_test`; the PR's status check is `status`."""
    repo = tmp / "src"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    base = commit(repo, {"app.py": "X = 1\n", "tests/test_base.py": base_test}, "base")
    head = branch(repo, "p1", {"tests/test_p1.py": "import unittest\nclass T(unittest.TestCase):\n    def test_p1(self): pass\n"})
    sh(repo, "git", "update-ref", "refs/audit/base", base)
    sh(repo, "git", "update-ref", "refs/audit/pr/1", head)
    bundle = tmp / "snap.bundle"
    sh(repo, "git", "bundle", "create", "-q", str(bundle), "--glob=refs/audit")
    snap = tmp / "snap.json"
    snap.write_text(json.dumps({"date": "test", "repo": "local/test", "base_ref": "main", "base_sha": base,
                                "prs": [{"number": 1, "title": "p1", "head": head, "draft": False, "status": status,
                                         "linked": [], "files": ["tests/test_p1.py"]}]}))
    config = tmp / "config.json"
    config.write_text(json.dumps(dict(UNITTEST, repo="local/test", base="main", required_status="ci", workers=1)))
    return audit.load_config(str(config)), snap, bundle


PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"


class SmallBuilds(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failing_base_stops_the_build(self):
        failing = "import unittest\nclass T(unittest.TestCase):\n    def test_base(self): self.fail('broken')\n"
        cfg, snap, bundle = small_project(self.tmp, failing, "SUCCESS")
        with self.assertRaises(SystemExit) as stop:
            build(cfg, snap, bundle, self.tmp, "a")
        self.assertIn("test_base", str(stop.exception.code))
        self.assertFalse((self.tmp / "a.json").exists())
        self.assertFalse((self.tmp / "work-a").exists())

    def test_nothing_eligible_renders_compactly(self):
        cfg, snap, bundle = small_project(self.tmp, PASSING, "FAILURE")
        text = audit.render(cfg, build(cfg, snap, bundle, self.tmp, "a"))
        self.assertIn("### Result: no PRs were eligible to try", text)
        self.assertIn("#1 (failure)", text)
        self.assertNotIn("(0)", text)
        self.assertNotIn("\n\n\n", text)

    def test_ignore_status_is_applied_and_shown(self):
        cfg, snap, bundle = small_project(self.tmp, PASSING, "FAILURE")
        res = build(cfg, snap, bundle, self.tmp, "a", ignore_status=True)
        self.assertEqual(res["merged"], [1])
        self.assertIn("**Override:** required status check `ci` ignored.", audit.render(cfg, res))


class Classify(unittest.TestCase):
    def cfg(self, **kw):
        cfg = {"test_prefix": "tests/", "order": [], "exclude": {}, "duplicates": {}, "features": {},
               "no_tests_ok": set(), "required_status": "ci"}
        cfg.update(kw)
        cfg.setdefault("test_files", "^tests/")
        return cfg

    def snap(self, *prs):
        return {"prs": [dict({"draft": False, "status": "SUCCESS", "files": ["tests/t.py"]}, number=n, **kw)
                        for n, kw in prs]}

    def test_order_then_number(self):
        eligible, _ = audit.classify(self.cfg(order=[3]), self.snap((1, {}), (2, {}), (3, {})))
        self.assertEqual(eligible, [3, 1, 2])

    def test_first_matching_reason_wins(self):
        _, skipped = audit.classify(self.cfg(exclude={1: "x"}), self.snap((1, {"status": "FAILURE", "draft": True})))
        self.assertEqual(skipped[0]["reason"], "excluded")

    def test_status_ignored_when_not_required(self):
        eligible, _ = audit.classify(self.cfg(required_status=""), self.snap((1, {"status": "FAILURE"})))
        self.assertEqual(eligible, [1])

    def test_test_files_pattern(self):
        cfg = self.cfg(test_files=r"_test\.py$")
        eligible, skipped = audit.classify(cfg, self.snap((1, {"files": ["pkg/a_test.py"]}),
                                                         (2, {"files": ["pkg/a.py"]})))
        self.assertEqual((eligible, skipped[0]["reason"]), ([1], "no_tests"))


class Helpers(unittest.TestCase):
    def test_names_keeps_spaces(self):
        self.assertEqual(audit.names("a b.py\0c.py\0"), ["a b.py", "c.py"])

    def test_clean_cannot_leave_code_span_or_cell(self):
        text = audit.clean("x` | @a ![i](http://e) \n### h" + "y" * 300, table=True)
        self.assertNotIn("`", text)
        self.assertNotIn("\n", text)
        self.assertNotRegex(text, r"(?<!\\)\|")
        self.assertLessEqual(len(text), 203 + text.count("\\"))

    def test_scan_without_patterns_uses_exit_status(self):
        self.assertEqual(audit.scan({}, 0, "anything")["outcome"], "pass")
        res = audit.scan({}, 3, "anything")
        self.assertEqual((res["outcome"], res["failures"]), ("fail", ["(exit status 3)"]))

    def test_scan_count_pattern_requires_a_test(self):
        cfg = {"count_pattern": r"^(\d+) passed", "failure_pattern": r"^FAILED (\S+)"}
        self.assertEqual(audit.scan(cfg, 0, "4 passed")["outcome"], "pass")
        self.assertEqual(audit.scan(cfg, 0, "0 passed")["failures"], ["(no tests ran)"])
        self.assertEqual(audit.scan(cfg, 1, "FAILED a::b\nFAILED a::c\n")["failures"], ["a::b", "a::c"])

    def test_read_capped(self):
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
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def member(self, name, kind=tarfile.REGTYPE, link=""):
        m = tarfile.TarInfo(name)
        m.type, m.linkname = kind, link
        return m

    def test_safe_member(self):
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
        self.assertIsNone(audit.place_probe(self.root, Path("tests/p.py"), b"x"))
        self.assertEqual((self.root / "tests" / "p.py").read_bytes(), b"x")

    def test_probe_refuses_symlinked_directory(self):
        (self.root / "tests").symlink_to(self.outside)
        self.assertIsNotNone(audit.place_probe(self.root, Path("tests/p.py"), b"x"))
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_probe_refuses_existing_file_or_link(self):
        (self.root / "tests").mkdir()
        target = self.outside / "victim"
        target.write_text("keep")
        (self.root / "tests" / "p.py").symlink_to(target)
        self.assertIsNotNone(audit.place_probe(self.root, Path("tests/p.py"), b"x"))
        self.assertEqual(target.read_text(), "keep")

    def test_work_directory_needs_marker(self):
        work = self.tmp / "work"
        work.mkdir()
        (work / "keep.txt").write_text("keep")
        with self.assertRaises(SystemExit):
            audit.Workspace({}, {"base_sha": "", "prs": []}, "unused", work)
        self.assertTrue((work / "keep.txt").exists())


def git_in(repo, *argv, data=None):
    env = dict(os.environ, **audit.GIT_ENV)
    return subprocess.run(["git", *argv], cwd=repo, env=env, input=data, check=True,
                          capture_output=True, text=True).stdout.strip()


def mktree(repo, entries):
    """Tree from (mode, name, blob text or subtree id) rows, so a test can hold any names and links."""
    rows = []
    for mode, name, value in entries:
        kind = "tree" if mode == "040000" else "blob"
        oid = value if kind == "tree" else git_in(repo, "hash-object", "-w", "--stdin", data=value)
        rows.append(f"{mode} {kind} {oid}\t{name}\n")
    return git_in(repo, "mktree", data="".join(rows))


class TreeFixture:
    """A one-commit repository built from raw trees, bundled and opened as a Workspace."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.src = self.tmp / "src"
        self.src.mkdir()
        git_in(self.src, "init", "-q")
        self.victim = self.tmp / "victim.txt"
        self.victim.write_text("keep")

    def tearDown(self):
        self._tmp.cleanup()

    def workspace(self, tree, **cfg):
        head = git_in(self.src, "commit-tree", tree, "-m", "pr")
        git_in(self.src, "update-ref", "refs/audit/base", head)
        bundle = self.tmp / "b.bundle"
        git_in(self.src, "bundle", "create", "-q", str(bundle), "--glob=refs/audit")
        cfg = dict(UNITTEST, test_timeout=60, python=sys.executable, **cfg)
        return audit.Workspace(cfg, {"base_sha": head, "prs": []}, bundle, self.tmp / "work"), head

    def pr_tree(self, files):
        return mktree(self.src, [("040000", "tests", mktree(self.src, [("100644", k, v) for k, v in files.items()]))])


class HostileTrees(TreeFixture, unittest.TestCase):
    """Commits a PR author could push, run through the real export and test steps."""

    def test_chained_link_is_pruned_and_never_written_through(self):
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
        tree = mktree(self.src, [("100644", ".gitattributes", "tests/test_hidden.py export-ignore\n"),
                                 ("040000", "tests", mktree(self.src, [("100644", "test_hidden.py", PASSING)]))])
        ws, head = self.workspace(tree)
        scratch, root = ws.export(head)
        try:
            self.assertTrue((root / "tests" / "test_hidden.py").is_file())
        finally:
            shutil.rmtree(scratch)

    def test_early_exit_is_not_a_pass(self):
        ws, head = self.workspace(self.pr_tree({"test_x.py": "import os\nos._exit(0)\n"}))
        wt = ws.tree("t", head)
        res = ws.test_once(wt, [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        self.assertEqual((res["outcome"], res["failures"]), ("fail", ["(no tests ran)"]))

    def test_background_processes_are_stopped(self):
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
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            self.fail("background process outlived the test run")


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
        os.environ.update(AUDIT_PASSED="from-caller", AUDIT_NOT_LISTED="secret")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AUDIT_PASSED", "AUDIT_NOT_LISTED")])
        ws, head = self.workspace(self.pr_tree({"test_env.py": ENV_TEST}),
                                  pass_env=["AUDIT_PASSED"], env={"AUDIT_FIXED": "from-config"})
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "pass")

    def test_unlisted_variables_stay_out(self):
        os.environ["AUDIT_PASSED"] = "from-caller"
        self.addCleanup(os.environ.pop, "AUDIT_PASSED", None)
        ws, head = self.workspace(self.pr_tree({"test_env.py": ENV_TEST}))
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "fail")

    def test_setup_cmd_runs_first_in_the_same_copy(self):
        setup = ["{python}", "-c", "open('tests/generated.py', 'w').write('X = 1\\n')"]
        ws, head = self.workspace(self.pr_tree({"test_setup.py": SETUP_TEST}), setup_cmd=setup)
        self.assertEqual(ws.test_once(ws.tree("t", head), UNITTEST_ARGV)["outcome"], "pass")

    def test_failed_setup_is_a_failure_and_tests_do_not_run(self):
        setup = ["{python}", "-c", "import sys; print('no toolchain'); sys.exit(3)"]
        ws, head = self.workspace(self.pr_tree({"test_ok.py": PASSING}), setup_cmd=setup)
        res = ws.test_once(ws.tree("t", head), UNITTEST_ARGV)
        self.assertEqual((res["outcome"], res["failures"]), ("fail", ["(setup failed)"]))
        self.assertEqual(res["message"], "setup_cmd exit status 3: no toolchain")

    def test_config_rejects_malformed_keys(self):
        for bad in ({"pass_env": "PATH"}, {"env": {"A": 1}}, {"setup_cmd": "make"}, {"setup_cmd": []}):
            path = self.tmp / "c.json"
            path.write_text(json.dumps(dict(UNITTEST, **bad)))
            with self.assertRaises(SystemExit, msg=bad):
                audit.load_config(path)


class TokenRedirect(unittest.TestCase):
    def test_token_not_sent_to_redirect_target(self):
        seen = {}

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"] = self.headers.get("Authorization")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            def do_GET(self):
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


class Golden(unittest.TestCase):
    """Results of a build of the fixture in make_project, and the report they render to."""

    def test_golden_report(self):
        res = json.loads((GOLDEN / "results.json").read_text())
        self.assertEqual(audit.render({}, res), (GOLDEN / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
