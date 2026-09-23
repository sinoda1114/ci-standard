#!/usr/bin/env python3
"""triage.py の回帰テスト（unittest、外部依存なし）。実行: python3 -m unittest discover -s scripts/pr-triage"""
import json, os, subprocess, sys, tempfile, unittest, urllib.error
from unittest import mock
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
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", json.dumps(r))   # 結果 JSON にも残さない

    def test_circuit_breaker_stops_after_first_error(self):
        saved = triage.ROUTES, triage.TIMEOUT
        try:
            triage.ROUTES = [("TYPESAFE_API_KEY", "http://127.0.0.1:9/systemone", "jev-latest")]; triage.TIMEOUT = 1
            r = self._run_with_call(triage.call, [th(i, "a.py", i) for i in range(6)])   # 本物の call（接続拒否）
            self.assertEqual(r["calls"], 1); self.assertEqual(r["jev"], "partial")
        finally:
            triage.ROUTES, triage.TIMEOUT = saved

    def test_budget_recorded_in_pairing_phase(self):
        ok = lambda route, state, q: ({"category": {"choice": "bug", "confidence": 0.9}, "is_security": {"noul": 0.1},
                                       "same_issue": {"noul": 0.2}}, None)
        r = self._run_with_call(ok, [th(i, "a.py", i) for i in range(6)], MAX_CALLS=8)   # 6 分類 + 15 ペア > 8
        self.assertEqual(r["calls"], 8); self.assertEqual(r["jev"], "partial"); self.assertIn("budget", r["errors"])

    def _run_with_call(self, fake_call, threads, **over):
        saved = triage.call, triage.MAX_CALLS, triage.BUDGET_SECONDS
        saved_env = {k: os.environ.get(k) for k in ("TYPESAFE_API_KEY", "HOME")}
        try:
            triage.call = fake_call
            for k, v in over.items(): setattr(triage, k, v)
            os.environ["TYPESAFE_API_KEY"] = "dummy"; os.environ["HOME"] = "/nonexistent"
            with tempfile.TemporaryDirectory() as d:
                src = Path(d, "t.json"); out = Path(d, "o.json"); md = Path(d, "o.md")
                src.write_text(json.dumps(threads))
                sys.argv = ["triage.py", "--threads", str(src), "--out", str(out), "--md", str(md)]; triage.main()
                return json.loads(out.read_text())
        finally:
            triage.call, triage.MAX_CALLS, triage.BUDGET_SECONDS = saved
            for k, v in saved_env.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v

    def test_non_numeric_and_malformed_answers_do_not_crash(self):
        answers = iter([({"category": {"choice": "bug"}, "is_security": {"noul": "high"}}, None),   # 数値以外
                        ([{"category": "bug"}], None),                                             # dict でない
                        ])
        r = self._run_with_call(lambda *a: next(answers), [th(1, "a.py", 1), th(2, "b.py", 2)])
        self.assertIsNone(r["threads"][0]["is_security"]); self.assertEqual(r["threads"][0]["category"], "unknown")
        self.assertEqual(r["calls"], 1)                     # 最初の不正応答で遮断（2 件目は呼ばれない）
        self.assertIn("bad-response", r["errors"]); self.assertEqual(r["jev"], "partial")

    def test_unknown_choice_does_not_trip_breaker(self):
        answers = iter([({"category": {"choice": "Bug"}, "is_security": {"noul": 0.1}}, None),      # ラベル揺れ（形は正しい）
                        ({"category": {"choice": "docs"}, "is_security": {"noul": 0.1}}, None)])
        r = self._run_with_call(lambda *a: next(answers), [th(1, "a.py", 1), th(2, "b.py", 2)])
        self.assertEqual(r["calls"], 2)                                  # 2 件目も分類される
        self.assertEqual([t["category"] for t in r["threads"]], ["unknown", "docs"])
        self.assertIn("unknown-choice", r["errors"]); self.assertEqual(r["jev"], "partial")

    def test_bool_is_not_a_probability(self):
        bad = ({"category": {"choice": "bug"}, "is_security": {"noul": True}}, None)
        r = self._run_with_call(lambda *a: bad, [th(1, "a.py", 1)])
        self.assertIsNone(r["threads"][0]["is_security"]); self.assertIn("bad-response", r["errors"])

    def test_count_only(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "t.json"); src.write_text(json.dumps([th(1, "a.py", 1), th(2, "a.py", 2, author="x", atype="User"), th(3, "a.py", 3, resolved=True)]))
            p = subprocess.run([sys.executable, str(HERE / "triage.py"), "--threads", str(src), "--out", "/dev/null", "--count-only"],
                               capture_output=True, text=True, env={**os.environ, "TYPESAFE_API_KEY": "dummy", "HOME": "/nonexistent"})
            self.assertEqual(p.stdout.strip(), "1")

    def test_malformed_pairing_answers_fall_back_to_line_proximity(self):
        # 同一ファイル 3 件 → 分類 3 回のあとペア 3 回。same_issue が数値・null・辞書でない形で返っても落ちず、行近接（±5）で束ねる
        ok = ({"category": {"choice": "bug", "confidence": 0.9}, "is_security": {"noul": 0.1}}, None)
        answers = iter([ok, ok, ok, ({"same_issue": 0.9}, None), ({"same_issue": None}, None), ({"same_issue": "yes"}, None)])
        r = self._run_with_call(lambda *a: next(answers), [th(1, "a.py", 10), th(2, "a.py", 12), th(3, "a.py", 40)])
        self.assertEqual(r["calls"], 4)                     # 最初の不正応答で遮断
        self.assertIn("bad-response", r["errors"]); self.assertEqual(r["jev"], "partial")
        self.assertEqual(sorted(sorted(g["members"]) for g in r["groups"]), [[0, 1], [2]])
        self.assertTrue(all(p["by"] == "line" for p in r["pairs"]))

    def test_deadline_stops_calls(self):
        import time as _t
        def slow(*a):
            _t.sleep(0.05); return ({"category": {"choice": "bug", "confidence": 0.9}, "is_security": {"noul": 0.1}, "same_issue": {"noul": 0.2}}, None)
        r = self._run_with_call(slow, [th(i, "a.py", i) for i in range(8)], BUDGET_SECONDS=0.12)   # 8 分類 + 28 ペア
        self.assertIn("deadline", r["errors"]); self.assertLess(r["calls"], 36); self.assertEqual(r["jev"], "partial")

    def test_mock_mode_and_location_display(self):
        r, md = run([th(1, "src/a/util.py", 10), th(2, None, None), th(3, None, 7)], env={"PR_TRIAGE_MOCK": "1"})
        self.assertEqual(r["jev"], "ok"); self.assertIn("a/util.py:10", md); self.assertIn("| (場所不明) |", md); self.assertIn("(場所不明):7", md)

    def test_rejects_non_array(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "t.json"); src.write_text("{}")
            p = subprocess.run([sys.executable, str(HERE / "triage.py"), "--threads", str(src), "--out", str(Path(d, "o.json"))],
                               capture_output=True)
            self.assertEqual(p.returncode, 2)


class CallHttpTests(unittest.TestCase):
    KEY = "apikey_" + "A" * 40
    ROUTE = (KEY, "https://jev.invalid/v1/systemone", "jev-latest")

    def http_error(self, read):
        fp = mock.Mock(); fp.read.side_effect = read
        return urllib.error.HTTPError(self.ROUTE[1], 403, "Forbidden", {}, fp)

    def test_sends_explicit_user_agent(self):
        resp = mock.MagicMock(); resp.__enter__.return_value.read.return_value = b'{"answers": {}}'
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            self.assertEqual(triage.call(self.ROUTE, "s", {}), ({}, None))
        ua = urlopen.call_args.args[0].get_header("User-agent")
        self.assertEqual(ua, triage.USER_AGENT)
        self.assertFalse(ua.startswith("Python-urllib"))

    def test_http_error_keeps_body_head(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(lambda *a: b"error code: 1010\n")):
            self.assertEqual(triage.call(self.ROUTE, "s", {}), (None, "HTTP 403: error code: 1010"))

    def test_key_is_masked_before_truncation(self):
        body = ("x" * 110 + " " + self.KEY + " tail").encode()
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(lambda *a: body)):
            _, err = triage.call(self.ROUTE, "s", {})
        self.assertTrue(err.startswith("HTTP 403: "))
        self.assertNotIn("apikey", err)

    def test_other_secrets_in_body_are_redacted(self):
        body = b"upstream echoed ghp_" + b"B" * 36 + b" in its error"
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(lambda *a: body)):
            _, err = triage.call(self.ROUTE, "s", {})
        self.assertNotIn("ghp_", err)
        self.assertIn("<redacted>", err)

    def test_error_body_read_is_bounded(self):
        fp = mock.Mock(); fp.read.return_value = b"x"
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(self.ROUTE[1], 500, "E", {}, fp)):
            triage.call(self.ROUTE, "s", {})
        (limit,), _ = fp.read.call_args
        self.assertTrue(0 < limit <= 65536)

    def test_unreadable_error_body_falls_back_to_status(self):
        def stalled(*a):
            raise TimeoutError("timed out")
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(stalled)):
            self.assertEqual(triage.call(self.ROUTE, "s", {}), (None, "HTTP 403"))


if __name__ == "__main__":
    unittest.main()
