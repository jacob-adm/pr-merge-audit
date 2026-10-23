"""Tests for audit.py. Real git repositories in temporary directories; nothing is mocked."""

import argparse
import http.server
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
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
    config.write_text(json.dumps({
        "repo": "local/test", "base": "main", "required_status": "ci", "workers": 2,
        "order": [1, 2, 3, 4, 5, 9], "exclude": {"6": "excluded for the test"},
        "probes": [{"name": "helper exists", "file": "probe.py", "targets": ["base", 3]}],
    }))
    return audit.load_config(str(config)), snap, bundle


def build(cfg, snap, bundle, tmp, name):
    out = tmp / f"{name}.json"
    args = argparse.Namespace(snapshot=str(snap), bundle=str(bundle), work=str(tmp / f"work-{name}"), out=str(out))
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
    def test_golden_report(self):
        cfg = audit.load_config(str(ROOT / "config.json"))
        res = json.loads((GOLDEN / "results.json").read_text())
        self.assertEqual(audit.render(cfg, res), (GOLDEN / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
