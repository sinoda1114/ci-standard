"""leak_metrics.py の単体テスト（ネットワークに出ない）。

マージ後 14 日以内の fix・revert・Issue を「漏れの候補」として集め、AI で 2 段に判定し、台帳に残す処理のうち、
判断を含む部分（窓・結び付け・判定の読み取り・2 段目に進む条件・台帳の読み書き）を確かめる。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leak_metrics as lm  # noqa: E402


def pr(n, merged, files, title="feat: x"):
    return {"number": n, "merged_at": merged, "files": files, "title": title}


def commit(sha, date, message, files, prs=()):
    return {"sha": sha, "date": date, "message": message, "files": files, "prs": list(prs)}


class FixMessage(unittest.TestCase):
    def test_fix_and_revert_are_candidates(self):
        for m in ["fix: 空配列で落ちる", "fix(api)!: x", "hotfix: y", 'Revert "feat: z"', "revert: z"]:
            self.assertTrue(lm.is_fix_message(m), m)

    def test_other_types_are_not(self):
        for m in ["feat: fix という語を含む機能", "chore: fix typo の準備", "docs: x", "prefix: y"]:
            self.assertFalse(lm.is_fix_message(m), m)

    def test_revert_target(self):
        msg = 'Revert "feat: x"\n\nThis reverts commit 0123456789abcdef0123456789abcdef01234567.'
        self.assertEqual(lm.revert_target(msg), "0123456789abcdef0123456789abcdef01234567")
        self.assertIsNone(lm.revert_target("fix: x"))


class Window(unittest.TestCase):
    PRS = [pr(10, "2026-10-01T00:00:00Z", ["src/a.js", "src/b.js"]),
           pr(11, "2026-10-01T00:00:00Z", ["docs/readme.md"]),
           pr(12, "2026-10-15T00:00:00Z", ["src/a.js"], title="fix: a を直す")]

    def test_suspects_are_prs_within_14_days_touching_same_files(self):
        c = commit("f1", "2026-10-14T23:00:00Z", "fix: a を直す", ["src/a.js"])
        cands = lm.fix_candidates("o/r", [c], self.PRS)
        self.assertEqual([s["number"] for s in cands[0]["suspects"]], [10])

    def test_older_than_14_days_is_not_suspect(self):
        c = commit("f2", "2026-10-16T00:00:01Z", "fix: a", ["src/a.js"], prs=[13])
        cands = lm.fix_candidates("o/r", [c], self.PRS)
        self.assertEqual([s["number"] for s in cands[0]["suspects"]], [12])

    def test_own_pr_is_not_suspect(self):
        c = commit("f3", "2026-10-15T00:00:00Z", "fix: a を直す", ["src/a.js"], prs=[12])
        cands = lm.fix_candidates("o/r", [c], self.PRS)
        self.assertNotIn(12, [s["number"] for s in cands[0]["suspects"]])

    def test_no_overlap_means_no_suspect(self):
        c = commit("f4", "2026-10-05T00:00:00Z", "fix: c", ["src/c.js"])
        cands = lm.fix_candidates("o/r", [c], self.PRS)
        self.assertEqual(cands[0]["suspects"], [])

    def test_non_fix_commits_are_ignored(self):
        c = commit("f5", "2026-10-05T00:00:00Z", "feat: d", ["src/a.js"])
        self.assertEqual(lm.fix_candidates("o/r", [c], self.PRS), [])

    def test_revert_points_to_original_pr(self):
        c = commit("r1", "2026-10-05T00:00:00Z", 'Revert "feat: x"\n\nThis reverts commit abc.', ["src/a.js"])
        cands = lm.fix_candidates("o/r", [c], self.PRS, revert_pr=lambda sha: 10 if sha == "abc" else None)
        self.assertEqual(cands[0]["kind"], "revert")
        self.assertEqual([s["number"] for s in cands[0]["suspects"]], [10])


class Judgement(unittest.TestCase):
    def test_reads_json_inside_prose(self):
        j = lm.parse_judgement('結果です。\n{"is_leak": "yes", "caused_by_pr": 10, "severity": "high", "security": true, "rationale": "x"}\n以上')
        self.assertEqual(j["severity"], "high")
        self.assertTrue(j["security"])

    def test_broken_output_is_none(self):
        self.assertIsNone(lm.parse_judgement("not json"))
        self.assertIsNone(lm.parse_judgement('{"is_leak": "yes"}'))
        self.assertIsNone(lm.parse_judgement('{"is_leak": "maybe", "caused_by_pr": null, "severity": "high", "security": false, "rationale": ""}'))

    def test_stage2_only_for_serious_or_unclear(self):
        base = {"is_leak": "yes", "caused_by_pr": 1, "severity": "other", "security": False, "rationale": ""}
        self.assertFalse(lm.needs_stage2(base))
        self.assertTrue(lm.needs_stage2(dict(base, severity="high")))
        self.assertTrue(lm.needs_stage2(dict(base, security=True)))
        self.assertTrue(lm.needs_stage2(dict(base, is_leak="unclear")))
        self.assertFalse(lm.needs_stage2(dict(base, is_leak="no")))


class TwoStage(unittest.TestCase):
    CAND = {"key": "o/r@f1", "repo": "o/r", "kind": "fix", "sha": "f1", "message": "fix: x", "diff": "-a\n+b",
            "suspects": [{"number": 10, "title": "feat: x", "diff": "+a"}]}

    def run_with(self, replies, key="k"):
        calls = []

        def api(model, prompt):
            calls.append(model)
            r = replies[len(calls) - 1]
            if isinstance(r, Exception):
                raise r
            return r
        return lm.judge(self.CAND, api=api, has_key=bool(key)), calls

    def test_minor_stops_at_stage1(self):
        out, calls = self.run_with(['{"is_leak":"yes","caused_by_pr":10,"severity":"other","security":false,"rationale":"軽微"}'])
        self.assertEqual(calls, [lm.STAGE1_MODEL])
        self.assertEqual(out["verdict"], "judged")
        self.assertFalse(out["serious"])

    def test_serious_goes_to_stage2_and_stage2_decides(self):
        out, calls = self.run_with([
            '{"is_leak":"yes","caused_by_pr":10,"severity":"high","security":true,"rationale":"注入"}',
            '{"is_leak":"yes","caused_by_pr":10,"severity":"critical","security":true,"rationale":"注入を確認"}'])
        self.assertEqual(calls, [lm.STAGE1_MODEL, lm.STAGE2_MODEL])
        self.assertTrue(out["serious"])
        self.assertEqual(out["severity"], "critical")

    def test_stage2_can_downgrade(self):
        out, _ = self.run_with([
            '{"is_leak":"yes","caused_by_pr":10,"severity":"high","security":false,"rationale":""}',
            '{"is_leak":"no","caused_by_pr":null,"severity":"other","security":false,"rationale":"仕様変更"}'])
        self.assertFalse(out["serious"])

    def test_broken_or_failed_output_is_unjudged_not_safe(self):
        out, _ = self.run_with(["not json"])
        self.assertEqual(out["verdict"], "unjudged")
        out, _ = self.run_with([RuntimeError("429")])
        self.assertEqual(out["verdict"], "unjudged")

    def test_no_key_is_unjudged_without_calls(self):
        out, calls = self.run_with([], key="")
        self.assertEqual(out["verdict"], "unjudged")
        self.assertEqual(calls, [])

    def test_no_suspect_skips_ai(self):
        out = lm.judge(dict(self.CAND, suspects=[]), api=lambda m, p: self.fail("呼ばれた"), has_key=True)
        self.assertEqual(out["verdict"], "no-suspect")
        self.assertFalse(out["serious"])

    def test_prompt_is_neutral_and_bounded(self):
        big = dict(self.CAND, diff="+x\n" * 100000)
        p = lm.build_prompt(big)
        self.assertLessEqual(len(p), lm.MAX_PROMPT_CHARS + 2000)
        self.assertIn("JSON", p)


class Ledger(unittest.TestCase):
    def test_state_roundtrip(self):
        recs = [{"key": "o/r@f1", "verdict": "judged", "serious": False, "severity": "other"},
                {"key": "o/r#5", "verdict": "unjudged", "serious": False, "severity": None}]
        body = lm.render_month(recs, "2026-10")
        self.assertEqual(lm.parse_month(body), recs)
        self.assertIn("2026-10", body)

    def test_parse_empty_or_foreign_body(self):
        self.assertEqual(lm.parse_month(""), [])
        self.assertEqual(lm.parse_month("人が書いた本文"), [])

    def test_unjudged_is_retried_judged_is_skipped(self):
        known = [{"key": "a", "verdict": "judged"}, {"key": "b", "verdict": "unjudged"}, {"key": "c", "verdict": "no-suspect"}]
        todo = lm.pending([{"key": "a"}, {"key": "b"}, {"key": "c"}, {"key": "d"}], known)
        self.assertEqual([c["key"] for c in todo], ["b", "d"])

    def test_merge_replaces_by_key(self):
        merged = lm.merge([{"key": "b", "verdict": "unjudged"}], [{"key": "b", "verdict": "judged"}])
        self.assertEqual(merged, [{"key": "b", "verdict": "judged"}])

    def test_serious_issue_text(self):
        rec = {"key": "o/r@f1", "repo": "o/r", "sha": "f1", "message": "fix: 注入を直す", "kind": "fix",
               "verdict": "judged", "serious": True, "severity": "critical", "security": True,
               "caused_by_pr": 10, "rationale": "入力が SQL に届く"}
        title, body = lm.serious_issue(rec)
        self.assertTrue(title.startswith("[漏れ]"))
        self.assertIn("o/r", title)
        self.assertIn("#10", body)
        self.assertIn("ai-review-leak:o/r@f1", body)

    def test_cap_limits_judgements_per_run(self):
        todo = [{"key": str(i)} for i in range(100)]
        self.assertEqual(len(lm.cap(todo)), lm.MAX_PER_RUN)


class LedgerSafety(unittest.TestCase):
    EVIL = "x leak-metrics-state --> <!-- leak-metrics-state [] leak-metrics-state --> & @someone [l](http://e)"

    def test_delimiters_in_any_value_survive_roundtrip(self):
        recs = [{"key": "o/r@f1", "message": self.EVIL, "rationale": self.EVIL, "verdict": "judged",
                 "serious": True, "severity": "high"},
                {"key": "o/r@f2", "message": "ok", "verdict": "unjudged", "serious": False}]
        body = lm.render_month(recs, "2026-10")
        self.assertEqual(lm.parse_month(body), recs)
        self.assertEqual(body.rsplit(lm.STATE_BEGIN, 1)[1].count("-->"), 1)  # 終わりの区切りだけ

    def test_markdown_list_does_not_render_outside_values(self):
        body = lm.render_month([{"key": "o/r@f1", "message": self.EVIL, "verdict": "judged", "serious": True,
                                 "severity": "high"}], "2026-10")
        head = body.split(lm.STATE_BEGIN, 1)[0]
        self.assertNotIn("<!--", head)
        self.assertNotIn("-->", head)

    def test_broken_state_stops_instead_of_overwriting(self):
        good = lm.render_month([{"key": "a", "verdict": "judged"}], "2026-10")
        for broken in [good.replace('"verdict"', '"verdict'), good.replace(lm.STATE_END, ""),
                       good.replace('[{"key": "a", "verdict": "judged"}]', '{"x": 1}')]:
            with self.assertRaises(lm.LedgerError):
                lm.parse_month(broken)

    def test_body_without_state_is_empty(self):
        self.assertEqual(lm.parse_month("人が書いた本文"), [])

    def test_caused_by_pr_is_normalized_and_limited_to_suspects(self):
        for v, want in [(10, 10), ("10", 10), ("#10", 10), ("PR 10", None), (True, None), (-1, None), (None, None)]:
            self.assertEqual(lm.pr_number(v), want, v)
        cand = {"key": "o/r@f1", "repo": "o/r", "kind": "fix", "sha": "f1", "message": "fix: x", "diff": "",
                "suspects": [{"number": 10, "title": "t", "diff": ""}]}
        reply = '{"is_leak":"yes","caused_by_pr":"%s","severity":"other","security":false,"rationale":"r"}'
        self.assertEqual(lm.judge(cand, api=lambda m, p: reply % "#10", has_key=True)["caused_by_pr"], 10)
        out = lm.judge(cand, api=lambda m, p: reply % "99", has_key=True)
        self.assertEqual(out["verdict"], "judged")
        self.assertIsNone(out["caused_by_pr"])

    def test_serious_issue_title_is_bounded_and_rationale_is_quoted(self):
        rec = {"key": "o/r@f1", "repo": "o/r", "sha": "f1", "message": "fix: " + "あ" * 400, "kind": "fix",
               "serious": True, "severity": "high", "security": False, "caused_by_pr": 10,
               "rationale": "```\n@someone <!-- x -->"}
        title, body = lm.serious_issue(rec)
        self.assertLessEqual(len(title), 200)
        self.assertIn("```text\n'''\n@someone <!-- x -->\n```", body)


if __name__ == "__main__":
    unittest.main()
