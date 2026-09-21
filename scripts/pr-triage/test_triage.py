#!/usr/bin/env python3
"""triage.py の回帰テスト（unittest、外部依存なし）。実行: python3 -m unittest discover -s scripts/pr-triage"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import triage  # noqa: E402


def th(i, path, line, author="cursor", body=None, resolved=False, atype="Bot"):
    return {"id": f"T{i}", "path": path, "line": line, "author": author, "authorType": atype,
            "isResolved": resolved, "body": body or f"finding {i}"}


def run(threads, env=None, args=()):
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "t.json"); out = Path(d, "o.json"); md = Path(d, "o.md")
        src.write_text(json.dumps(threads))
        e = {**os.environ, "HOME": "/nonexistent", "TYPESAFE_API_KEY": "", "AI_GATEWAY_API_KEY": "", **(env or {})}
        subprocess.run([sys.executable, str(HERE / "triage.py"), "--threads", str(src), "--out", str(out), "--md", str(md), *args],
                       check=True, env=e, capture_output=True)
        return json.loads(out.read_text()), md.read_text()


class TriageTests(unittest.TestCase):
    def test_bot_only_and_unresolved_only(self):
        r, _ = run([th(1, "a.py", 1), th(2, "a.py", 2, author="sinoda1114", atype="User"), th(3, "a.py", 3, resolved=True)])
        self.assertEqual(len(r["threads"]), 1); self.assertEqual(r["dropped"], {"human": 1, "resolved": 1})

    def test_include_flags(self):
        r, _ = run([th(1, "a.py", 1), th(2, "a.py", 2, author="x", atype="User"), th(3, "a.py", 3, resolved=True)],
                   args=("--include-humans", "--include-resolved"))
        self.assertEqual(len(r["threads"]), 3)

    def test_full_path_grouping(self):
        r, _ = run([th(1, "src/index.ts", 10), th(2, "test/index.ts", 12), th(3, "src/index.ts", 11)])
        self.assertEqual(sorted(sorted(g["members"]) for g in r["groups"]), [[0, 2], [1]])
        self.assertEqual(r["jev"], "unavailable")

    def test_secret_regex(self):
        yes = ["github_pat_11ABCDEFG0123456789abcdefghij", "PASSWORD=Sup3rS3cretValue123456", "token = 'abcdefghijklmnop1234'",
               "password: 'p@ss w0rd!'", "xoxb-123456789012-abcdefghijkl", "AKIAABCDEFGHIJKLMNOP"]
        no = ["token = getToken()", "read from process.env.API_KEY", "secret: string", "password field validation"]
        for s in yes: self.assertTrue(triage.SECRET_RE.search(s), s)
        for s in no: self.assertFalse(triage.SECRET_RE.search(s), s)

    def test_secret_thread_not_sent_and_masked(self):
        r, md = run([th(1, "a.py", 1, body="token = 'AKIAIOSFODNN7EXAMPLE' leaked")], env={"TYPESAFE_API_KEY": "dummy"})
        self.assertEqual(r["calls"], 0); self.assertEqual(r["jev"], "unavailable")
        self.assertIn("秘密情報らしき値を含むため要旨を省略", md); self.assertNotIn("AKIAIOSFODNN7EXAMPLE", md)

    def test_circuit_breaker_stops_after_first_error(self):
        saved = triage.ROUTES, triage.TIMEOUT
        try:
            triage.ROUTES = [("TYPESAFE_API_KEY", "http://127.0.0.1:9/systemone", "jev-latest")]; triage.TIMEOUT = 1
            os.environ["TYPESAFE_API_KEY"] = "dummy"; os.environ["HOME"] = "/nonexistent"
            with tempfile.TemporaryDirectory() as d:
                src = Path(d, "t.json"); out = Path(d, "o.json")
                src.write_text(json.dumps([th(i, "a.py", i) for i in range(6)]))
                sys.argv = ["triage.py", "--threads", str(src), "--out", str(out)]; triage.main()
                r = json.loads(out.read_text())
            self.assertEqual(r["calls"], 1); self.assertEqual(r["jev"], "partial")
        finally:
            triage.ROUTES, triage.TIMEOUT = saved

    def test_budget_recorded_in_pairing_phase(self):
        saved = triage.call, triage.MAX_CALLS
        try:
            triage.call = lambda route, state, q: ({"category": {"choice": "bug", "confidence": 0.9}, "is_security": {"noul": 0.1},
                                                    "same_issue": {"noul": 0.2}}, None)
            triage.MAX_CALLS = 8
            os.environ["TYPESAFE_API_KEY"] = "dummy"; os.environ["HOME"] = "/nonexistent"
            with tempfile.TemporaryDirectory() as d:
                src = Path(d, "t.json"); out = Path(d, "o.json")
                src.write_text(json.dumps([th(i, "a.py", i) for i in range(6)]))   # 6 分類 + 15 ペア > 8
                sys.argv = ["triage.py", "--threads", str(src), "--out", str(out)]; triage.main()
                r = json.loads(out.read_text())
            self.assertEqual(r["calls"], 8); self.assertEqual(r["jev"], "partial"); self.assertIn("budget", r["errors"])
        finally:
            triage.call, triage.MAX_CALLS = saved

    def test_mock_mode_and_location_display(self):
        r, md = run([th(1, "src/a/util.py", 10), th(2, None, None)], env={"PR_TRIAGE_MOCK": "1"})
        self.assertEqual(r["jev"], "ok"); self.assertIn("a/util.py:10", md); self.assertIn("(場所不明)", md)

    def test_rejects_non_array(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "t.json"); src.write_text("{}")
            p = subprocess.run([sys.executable, str(HERE / "triage.py"), "--threads", str(src), "--out", str(Path(d, "o.json"))],
                               capture_output=True)
            self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
