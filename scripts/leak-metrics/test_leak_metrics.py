"""leak_metrics.py の単体テスト（ネットワークに出ない）。

マージ後 14 日以内の fix・revert・Issue を「漏れの候補」として集め、AI で 2 段に判定し、台帳に残す処理のうち、
判断を含む部分（窓・結び付け・判定の読み取り・2 段目に進む条件・台帳の読み書き）を確かめる。
"""
import json
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

    def test_legacy_body_with_delimiters_in_values_is_read_from_last_lines(self):
        # 修正前の形式（区切りを逃がさない）で保存された本文でも、値の中の区切りを状態と取り違えない
        import json
        recs = [{"key": "o/r@f1", "message": "<!-- leak-metrics-state [] leak-metrics-state -->", "verdict": "judged",
                 "serious": True, "severity": "high"}]
        legacy = "\n".join(["# 漏れの記録 2026-10", "", "- o/r@f1 %s（high）" % recs[0]["message"], "",
                             lm.STATE_BEGIN, json.dumps(recs, ensure_ascii=False), lm.STATE_END])
        self.assertEqual(lm.parse_month(legacy), recs)

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


class FakeGitHub:
    """台帳リポジトリの Issue と、取り直し用の API だけを持つ偽の gh"""
    def __init__(self, issues=(), fail_create=0):
        self.issues = {i["number"]: dict(i, labels=list(i.get("labels", []))) for i in issues}
        self.fail_create = fail_create
        self.calls = []
        self.api = {}

    def __call__(self, *args, input_text=None):
        self.calls.append(args)
        if args[:3] == ("api", "--paginate", "--slurp") and args[3].startswith("repos/o/ledger/issues?"):
            label = args[3].split("labels=")[1] if "labels=" in args[3] else None
            items = [{k: i[k] for k in ("number", "title", "body")} for i in self.issues.values()
                     if label is None or label in i["labels"]]
            return json.dumps([items[n:n + 100] for n in range(0, len(items), 100)] or [[]])
        if args[:2] == ("issue", "create"):
            if "--label" in args and self.fail_create:
                self.fail_create -= 1
                raise RuntimeError("gh issue create: 502")
            n = max(self.issues, default=0) + 1
            self.issues[n] = {"number": n, "title": args[args.index("--title") + 1], "body": input_text,
                              "labels": [args[args.index("--label") + 1]] if "--label" in args else []}
            return "https://github.com/o/ledger/issues/%d\n" % n
        if args[:2] == ("issue", "edit"):
            self.issues[int(args[2])]["body"] = input_text
            return ""
        if args[:2] == ("label", "create"):
            return ""
        if args[0] in ("api", "pr"):
            key = " ".join(args)
            if key not in self.api:
                raise RuntimeError("gh %s: HTTP 404" % key)
            return self.api[key]
        raise AssertionError(args)

    def ledgers(self):
        return {i["title"]: lm.parse_month(i["body"]) for i in self.issues.values() if i["title"].startswith("[漏れの記録]")}

    def leak_issues(self):
        return [i for i in self.issues.values() if "type:fix" in i["labels"]]


SERIOUS = '{"is_leak":"yes","caused_by_pr":10,"severity":"high","security":false,"rationale":"r"}'
MINOR = '{"is_leak":"no","caused_by_pr":null,"severity":"other","security":false,"rationale":"r"}'


def cand(key, date, suspects=(10,)):
    repo, sha = key.split("@")
    return {"key": key, "repo": repo, "kind": "fix", "sha": sha, "date": date, "message": "fix: x", "diff": "+x",
            "suspects": [{"number": n, "title": "t", "diff": "+y"} for n in suspects]}


class Run(unittest.TestCase):
    def setUp(self):
        self.saved = (lm.gh, lm.collect, lm.anthropic_api, lm.utcnow, os.environ.get("ANTHROPIC_API_KEY"))
        os.environ["ANTHROPIC_API_KEY"] = "k"
        self.now = lm.dt.datetime(2026, 10, 1, 3, 0, tzinfo=lm.dt.timezone.utc)
        lm.utcnow = lambda: self.now
        self.replies = []
        self.judged = []

        self.prompts = []

        def api(model, prompt):
            self.judged.append(model)
            self.prompts.append(prompt)
            return self.replies.pop(0) if self.replies else MINOR
        lm.anthropic_api = api

    def tearDown(self):
        lm.gh, lm.collect, lm.anthropic_api, lm.utcnow, key = self.saved
        if key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = key

    def go(self, fake, cands, errors=()):
        lm.gh = fake
        lm.collect = lambda owner, exclude: ([dict(c) for c in cands], list(errors))
        a = lm.argparse.Namespace(owner="o", ledger="o/ledger", exclude="", dry_run=False)
        return lm.run(a)

    def test_month_boundary_does_not_rejudge_or_duplicate(self):
        k = "o/r@abcdef1"
        sep = {"key": k, "repo": "o/r", "kind": "fix", "sha": "abcdef1", "date": "2026-09-29T00:00:00Z",
               "verdict": "judged", "serious": True, "notified": True, "severity": "high", "suspects": [10]}
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-09", "body": lm.render_month([sep], "2026-09")}])
        self.go(fake, [cand(k, "2026-09-29T00:00:00Z")])
        self.assertEqual(self.judged, [])
        self.assertEqual(fake.leak_issues(), [])
        self.assertEqual(fake.ledgers()["[漏れの記録] 2026-09"], [sep])
        self.assertNotIn("[漏れの記録] 2026-10", fake.ledgers())

    def test_failed_leak_issue_is_retried_next_run_without_duplicates(self):
        k = "o/r@abcdef2"
        fake = FakeGitHub(fail_create=1)
        self.replies = [SERIOUS, SERIOUS]
        self.go(fake, [cand(k, "2026-09-30T00:00:00Z")])
        self.assertEqual(fake.leak_issues(), [])
        self.assertFalse(fake.ledgers()["[漏れの記録] 2026-09"][0]["notified"])
        self.go(fake, [cand(k, "2026-09-30T00:00:00Z")])
        self.go(fake, [cand(k, "2026-09-30T00:00:00Z")])
        self.assertEqual(len(fake.leak_issues()), 1)
        self.assertTrue(fake.ledgers()["[漏れの記録] 2026-09"][0]["notified"])
        self.assertEqual(len(self.judged), 2)  # 判定は 1 回（2 段）だけ

    def test_created_but_unsaved_leak_issue_is_not_created_twice(self):
        k = "o/r@abcdef3"
        rec = {"key": k, "repo": "o/r", "kind": "fix", "sha": "abcdef3", "date": "2026-10-01T00:00:00Z",
               "verdict": "judged", "serious": True, "notified": False, "severity": "high", "suspects": [10]}
        title, body = lm.serious_issue(rec)
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-10", "body": lm.render_month([rec], "2026-10")},
                           {"number": 2, "title": title, "body": body, "labels": ["type:fix"]}])
        self.go(fake, [])
        self.assertEqual(len(fake.leak_issues()), 1)
        self.assertTrue(fake.ledgers()["[漏れの記録] 2026-10"][0]["notified"])

    def test_mark_in_middle_of_body_does_not_suppress(self):
        k = "o/r@abcdef4"
        rec = {"key": k, "repo": "o/r", "kind": "fix", "sha": "abcdef4", "date": "2026-10-01T00:00:00Z",
               "verdict": "judged", "serious": True, "notified": False, "severity": "high", "suspects": [10]}
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-10", "body": lm.render_month([rec], "2026-10")},
                           {"number": 2, "title": "[漏れ] other", "labels": ["type:fix"],
                            "body": "理由: %s\n\n<!-- ai-review-leak:o/r@other -->" % lm.leak_mark(k)}])
        self.go(fake, [])
        self.assertEqual(len(fake.leak_issues()), 2)

    def test_unjudged_outside_window_is_refetched_and_judged(self):
        old = {"key": "o/r@abcdef5", "repo": "o/r", "kind": "fix", "sha": "abcdef5", "date": "2026-08-01T00:00:00Z",
               "message": "fix: x", "verdict": "unjudged", "serious": False, "suspects": [10], "attempts": 1}
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-08", "body": lm.render_month([old], "2026-08")}])
        fake.api = {"api --paginate --slurp repos/o/r/commits/abcdef5?per_page=100": json.dumps([{"files": [{"patch": "+x"}]}]),
                    "api repos/o/r/pulls/10": json.dumps({"title": "feat: y"}), "pr diff 10 -R o/r": "+y",
                    "api --paginate --slurp repos/o/r/pulls/10/files?per_page=100": json.dumps([[{"filename": "a"}]])}
        self.go(fake, [])
        self.assertEqual(len(self.judged), 1)
        self.assertEqual(fake.ledgers()["[漏れの記録] 2026-08"][0]["verdict"], "judged")

    def test_tampered_record_is_not_refetched(self):
        bad = {"key": "evil/secret#5", "repo": "evil/secret", "kind": "issue", "sha": "", "date": "2026-08-01T00:00:00Z",
               "verdict": "unjudged", "serious": False, "suspects": [1]}
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-08", "body": lm.render_month([bad], "2026-08")}])
        self.go(fake, [])
        self.assertFalse([c for c in fake.calls if c[0] == "pr" or (c[0] == "api" and c[1] != "--paginate")])

    def test_over_cap_candidates_are_recorded_and_get_their_turn(self):
        cands = [cand("o/r@%07x" % i, "2026-09-%02dT00:00:00Z" % (1 + i % 28)) for i in range(lm.MAX_PER_RUN + 5)]
        fake = FakeGitHub()
        self.replies = ["not json"] * lm.MAX_PER_RUN  # 1 回目は全部読めない
        self.go(fake, cands)
        recs = {r["key"]: r for rs in fake.ledgers().values() for r in rs}
        self.assertEqual(len(recs), lm.MAX_PER_RUN + 5)
        tried = {k for k, r in recs.items() if r.get("attempts")}
        self.judged.clear()
        self.go(fake, cands)
        recs = {r["key"]: r for rs in fake.ledgers().values() for r in rs}
        untried = [c["key"] for c in cands if c["key"] not in tried]
        self.assertTrue(all(recs[k]["verdict"] == "judged" for k in untried))

    def test_failed_refetch_counts_as_a_try(self):
        olds = [{"key": "o/r@%07x" % i, "repo": "o/r", "kind": "fix", "sha": "%07x" % i, "date": "2026-08-01T00:00:00Z",
                 "message": "fix: x", "verdict": "unjudged", "serious": False, "suspects": [10]} for i in range(lm.MAX_PER_RUN + 3)]
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-08", "body": lm.render_month(olds, "2026-08")}])
        self.go(fake, [])  # 取り直しはすべて失敗する（偽の API に応答が無い）
        tried = {r["key"] for r in fake.ledgers()["[漏れの記録] 2026-08"] if r.get("attempts")}
        self.assertEqual(len(tried), lm.MAX_PER_RUN)
        fake.calls.clear()
        self.go(fake, [])
        asked = {c[-1].split("/commits/")[1].split("?")[0] for c in fake.calls if c[0] == "api" and "/commits/" in c[-1]}
        self.assertTrue({k.split("@")[1] for k in {r["key"] for r in olds} - tried} <= asked)

    def test_many_serious_records_stay_within_body_limit(self):
        recs = {"o/r@%07x" % i: {"key": "o/r@%07x" % i, "repo": "o/r", "kind": "fix", "sha": "%07x" % i,
                                  "date": "2026-10-01T00:00:00Z", "message": "fix: " + "あ" * 190, "verdict": "judged",
                                  "serious": True, "notified": True, "severity": "high", "rationale": "い" * 500,
                                  "suspects": [10]} for i in range(300)}
        fake = FakeGitHub()
        lm.gh = fake
        lm.save_months("o/ledger", recs, {}, {"2026-10"}, self.now)
        self.assertTrue(all(len(i["body"]) <= 65536 for i in fake.issues.values()))
        self.assertEqual(lm.load_ledger(list(fake.issues.values()))[0], recs)

    def test_ledger_listing_is_not_truncated(self):
        k = "o/r@abcdef7"
        sep = {"key": k, "repo": "o/r", "kind": "fix", "sha": "abcdef7", "date": "2026-09-29T00:00:00Z",
               "verdict": "judged", "serious": False, "suspects": [10]}
        noise = [{"number": n, "title": "other %d" % n, "body": ""} for n in range(1, 1200)]
        fake = FakeGitHub(noise + [{"number": 1200, "title": "[漏れの記録] 2026-09", "body": lm.render_month([sep], "2026-09")}])
        self.go(fake, [cand(k, "2026-09-29T00:00:00Z")])
        self.assertEqual(self.judged, [])

    def test_big_month_is_split_and_read_back(self):
        recs = {"o/r@%07x" % i: {"key": "o/r@%07x" % i, "repo": "o/r", "kind": "fix", "sha": "%07x" % i,
                                  "date": "2026-10-01T00:00:00Z", "message": "fix: " + "あ" * 190, "verdict": "judged",
                                  "serious": False, "rationale": "い" * 500, "suspects": [10]} for i in range(200)}
        fake = FakeGitHub()
        lm.gh = fake
        lm.save_months("o/ledger", recs, {}, {"2026-10"}, self.now)
        bodies = [i["body"] for i in fake.issues.values()]
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all(len(b) <= 65536 for b in bodies))
        got, slots = lm.load_ledger(list(fake.issues.values()))
        self.assertEqual(got, recs)
        lm.save_months("o/ledger", recs, slots, {"2026-10"}, self.now)  # 2 回目は同じ Issue を書き直す
        self.assertEqual(len(fake.issues), len(bodies))

    def test_future_dated_record_goes_to_current_month(self):
        self.assertEqual(lm.month_of({"date": "2099-01-01T00:00:00Z"}, self.now), "2026-10")
        self.assertEqual(lm.month_of({"date": "2026-09-30T00:00:00Z"}, self.now), "2026-09")

    def test_broken_ledger_stops_before_writing(self):
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-10", "body": "x\n" + lm.STATE_BEGIN + "\n[\n" + lm.STATE_END}])
        with self.assertRaises(lm.LedgerError):
            self.go(fake, [cand("o/r@abcdef6", "2026-10-01T00:00:00Z")])
        self.assertFalse([c for c in fake.calls if c[:2] in (("issue", "edit"), ("issue", "create"))])


class Robustness(unittest.TestCase):
    def test_huge_fix_diff_keeps_suspects_section(self):
        c = {"kind": "fix", "message": "fix: x", "diff": "+x\n" * 100000,
             "suspects": [{"number": n, "title": "t%d" % n, "diff": "+y\n" * 50000} for n in range(1, 6)]}
        p = lm.build_prompt(c)
        self.assertLessEqual(len(p), lm.MAX_PROMPT_CHARS)
        for n in range(1, 6):
            self.assertIn("### PR #%d" % n, p)

    def test_revert_lookup_failure_marks_only_that_candidate(self):
        prs = [pr(10, "2026-10-01T00:00:00Z", ["src/a.js"])]
        cs = [commit("r1", "2026-10-05T00:00:00Z", 'Revert "x"\n\nThis reverts commit abcdef1.', ["src/a.js"]),
              commit("f1", "2026-10-05T00:00:00Z", "fix: a", ["src/a.js"])]

        def boom(sha):
            raise RuntimeError("HTTP 502")
        out = lm.fix_candidates("o/r", cs, prs, revert_pr=boom)
        self.assertEqual(len(out), 2)
        self.assertIn("incomplete", out[0])
        self.assertNotIn("incomplete", out[1])
        rec = lm.judge(out[0], api=lambda m, p: self.fail("材料が欠けたまま判定した"), has_key=True)
        self.assertEqual(rec["verdict"], "unjudged")

    def test_incomplete_without_suspects_is_not_settled_as_no_suspect(self):
        c = {"key": "o/r@r1", "repo": "o/r", "kind": "revert", "sha": "r1", "message": "Revert", "diff": "",
             "suspects": [], "incomplete": "revert 元の PR を取得できない"}
        self.assertEqual(lm.judge(c, api=lambda m, p: self.fail("呼ばれた"), has_key=True)["verdict"], "unjudged")

    def test_suspects_are_not_shared_between_candidates(self):
        prs = [pr(10, "2026-10-01T00:00:00Z", ["src/a.js"])]
        cs = [commit("f1", "2026-10-05T00:00:00Z", "fix: a", ["src/a.js"]),
              commit("f2", "2026-10-06T00:00:00Z", "fix: b", ["src/a.js"])]
        out = lm.fix_candidates("o/r", cs, prs)
        out[0]["suspects"][0]["diff"] = "x"
        self.assertNotIn("diff", out[1]["suspects"][0])
        self.assertNotIn("diff", prs[0])

    def test_suspect_diff_too_large_is_judged_with_file_list(self):
        saved = lm.gh
        try:
            def too_large(*a, **k):
                raise RuntimeError("gh pr diff: HTTP 406: diff too large")
            lm.gh = too_large
            d, why = lm.suspect_diff("o/r", {"number": 10, "files": ["src/a.js"]})
            self.assertEqual(why, "")
            self.assertIn("src/a.js", d)

            def flaky(*a, **k):
                raise RuntimeError("gh pr diff: HTTP 502")
            lm.gh = flaky
            d, why = lm.suspect_diff("o/r", {"number": 10})
            self.assertTrue(why)
        finally:
            lm.gh = saved


class Round2(unittest.TestCase):
    def test_damaged_or_missing_state_stops(self):
        good = lm.render_month([{"key": "a", "verdict": "judged"}], "2026-10")
        for broken in [good.replace(lm.STATE_BEGIN, "<!-- leak-metrics-stat"), "人が書いた本文"]:
            with self.assertRaises(lm.LedgerError):
                lm.parse_month(broken)
        self.assertEqual(lm.parse_month(""), [])

    def test_records_stay_in_their_part_when_a_later_write_fails(self):
        now = lm.dt.datetime(2026, 10, 1, tzinfo=lm.dt.timezone.utc)
        mk = lambda i: {"key": "o/r@%07x" % i, "repo": "o/r", "kind": "fix", "sha": "%07x" % i,
                        "date": "2026-10-01T00:00:00Z", "message": "fix: " + "あ" * 190, "verdict": "unjudged",
                        "serious": False, "rationale": "い" * 500, "suspects": [10]}
        saved = lm.gh
        try:
            fake = FakeGitHub()
            lm.gh = fake
            recs = {r["key"]: r for r in map(mk, range(150))}
            lm.save_months("o/ledger", recs, {}, {"2026-10"}, now)
            got, slots = lm.load_ledger(list(fake.issues.values()))
            before = {k: r["part"] for k, r in got.items()}
            for k in list(got)[:40]:  # 記録が少し大きくなっても、本は動かない
                got[k] = dict(got[k], attempts=3, rationale="う" * 500)
            got.update({r["key"]: r for r in map(mk, range(150, 170))})

            class Fail2(FakeGitHub):
                pass
            edits = []

            def flaky(*args, input_text=None):
                if args[:2] == ("issue", "edit") and edits:
                    raise RuntimeError("502")
                if args[:2] == ("issue", "edit"):
                    edits.append(args)
                return fake(*args, input_text=input_text)
            lm.gh = flaky
            with self.assertRaises(RuntimeError):
                lm.save_months("o/ledger", got, slots, {"2026-10"}, now)
            after, _ = lm.load_ledger(list(fake.issues.values()))
            self.assertTrue(set(before) <= set(after))  # 書き込みが途中で失敗しても、元の記録は 1 件も消えない
            self.assertTrue(all(len(i["body"]) <= 65536 for i in fake.issues.values()))
        finally:
            lm.gh = saved

    def test_406_is_matched_exactly(self):
        saved = lm.gh
        try:
            def e(*a, **k):
                raise RuntimeError("gh pr diff 1406 -R o/r: HTTP 502")
            lm.gh = e
            self.assertTrue(lm.suspect_diff("o/r", {"number": 1406})[1])
        finally:
            lm.gh = saved

    def test_dot_repo_names_are_rejected(self):
        r = {"key": "o/..@abcdef1", "repo": "o/..", "kind": "fix", "sha": "abcdef1", "suspects": []}
        self.assertFalse(lm.valid_record(r, "o", set()))

    def test_last_suspect_keeps_closing_fence(self):
        c = {"kind": "fix", "message": "fix: x", "diff": "+x\n" * 100000,
             "suspects": [{"number": n, "title": "t" * 300, "diff": "+y\n" * 50000} for n in range(1, 6)]}
        self.assertTrue(lm.build_prompt(c).endswith("```"))


class RefetchRevertWindow(Run):
    def test_refetched_revert_of_pr_outside_window_has_no_suspect(self):
        old = {"key": "o/r@abcdef9", "repo": "o/r", "kind": "revert", "sha": "abcdef9", "date": "2026-08-01T00:00:00Z",
               "message": "Revert", "verdict": "unjudged", "serious": False, "suspects": []}
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-08", "body": lm.render_month([old], "2026-08")}])
        fake.api = {"api --paginate --slurp repos/o/r/commits/abcdef9?per_page=100": json.dumps([{"commit": {"message": 'Revert "x"\n\nThis reverts commit 1234567.'}, "files": []}]),
                    "api repos/o/r/commits/1234567/pulls": json.dumps([{"number": 10}]),
                    "api repos/o/r/pulls/10": json.dumps({"title": "feat: x", "merged_at": "2026-06-18T00:00:00Z"})}  # 44 日前
        lm.gh = fake
        lm.collect = lambda owner, exclude: ([], [])
        lm.run(lm.argparse.Namespace(owner="o", ledger="o/ledger", exclude="", dry_run=False))
        rec = fake.ledgers()["[漏れの記録] 2026-08"][0]
        self.assertEqual(rec["verdict"], "no-suspect")
        self.assertEqual(self.judged, [])


class Round2b(Run):
    def test_rejudged_record_is_not_lost_and_moves_only_backward(self):
        mk = lambda i, v: {"key": "o/r@%07x" % i, "repo": "o/r", "kind": "fix", "sha": "%07x" % i,
                           "date": "2026-10-01T00:00:00Z", "message": "fix: " + "あ" * 190, "verdict": v,
                           "serious": False, "rationale": "い" * 500, "suspects": [10]}
        fake = FakeGitHub()
        lm.gh = fake
        lm.save_months("o/ledger", {r["key"]: r for r in [mk(i, "judged") for i in range(150)] + [mk(150, "unjudged")]},
                       {}, {"2026-10"}, self.now)
        before = {k: r["part"] for k, r in lm.load_ledger(list(fake.issues.values()))[0].items()}
        self.go(fake, [cand("o/r@%07x" % 150, "2026-10-01T00:00:00Z")])
        after = {k: r["part"] for k, r in lm.load_ledger(list(fake.issues.values()))[0].items()}
        self.assertEqual(set(before), set(after))  # 消えない
        self.assertTrue(all(after[k] >= before[k] for k in before))  # 移るなら、後ろの本へだけ

    def test_grown_record_moves_back_without_loss_and_bodies_fit(self):
        mk = lambda i: {"key": "o/r@%07x" % i, "repo": "o/r", "kind": "fix", "sha": "%07x" % i,
                        "date": "2026-10-01T00:00:00Z", "message": "fix: " + "あ" * 190, "verdict": "unjudged",
                        "serious": False, "rationale": "", "suspects": [10]}
        fake = FakeGitHub()
        lm.gh = fake
        lm.save_months("o/ledger", {r["key"]: r for r in map(mk, range(200))}, {}, {"2026-10"}, self.now)
        got, slots = lm.load_ledger(list(fake.issues.values()))
        for r in got.values():  # 全件が判定されて大きくなる
            r.update(verdict="judged", rationale="い" * 500)
        lm.save_months("o/ledger", got, slots, {"2026-10"}, self.now)
        self.assertTrue(all(len(i["body"]) <= 65536 for i in fake.issues.values()))
        self.assertEqual(set(lm.load_ledger(list(fake.issues.values()))[0]), set(got))

    def test_crlf_and_bad_types(self):
        body = lm.render_month([{"key": "a", "verdict": "judged"}], "2026-10").replace("\n", "\r\n")
        self.assertEqual(lm.parse_month(body), [{"key": "a", "verdict": "judged"}])
        for bad in [{"key": "a", "attempts": "x"}, {"key": "a", "suspects": ["1"]}, {"key": "a", "part": True}, {"key": "a", "models": [123]}]:
            with self.assertRaises(lm.LedgerError):
                lm.parse_month(lm.render_month([bad], "2026-10"))

    def test_percent_in_pr_title_is_not_a_format(self):
        c = {"kind": "fix", "message": "fix: 100%", "diff": "+x",
             "suspects": [{"number": 1, "title": "feat: 100% coverage %s %d", "diff": "+y"}]}
        self.assertIn("100% coverage %s %d", lm.build_prompt(c))


class RefetchRevert(Run):
    def test_revert_whose_original_pr_was_unknown_is_looked_up_again(self):
        old = {"key": "o/r@abcdef8", "repo": "o/r", "kind": "revert", "sha": "abcdef8", "date": "2026-08-01T00:00:00Z",
               "message": "Revert", "verdict": "unjudged", "serious": False, "suspects": []}
        fake = FakeGitHub([{"number": 1, "title": "[漏れの記録] 2026-08", "body": lm.render_month([old], "2026-08")}])
        fake.api = {"api --paginate --slurp repos/o/r/commits/abcdef8?per_page=100": json.dumps([{"commit": {"message": 'Revert "x"\n\nThis reverts commit 1234567.'},
                                                                 "files": [{"patch": "-x"}]}]),
                    "api repos/o/r/commits/1234567/pulls": json.dumps([{"number": 10}]),
                    "api repos/o/r/pulls/10": json.dumps({"title": "feat: x", "merged_at": "2026-07-25T00:00:00Z"}),
                    "api --paginate --slurp repos/o/r/pulls/10/files?per_page=100": json.dumps([[{"filename": "src/big.js"}]])}

        def pr_diff(*args, input_text=None):
            if args[:2] == ("pr", "diff"):
                raise RuntimeError("gh pr diff 10: HTTP 406 diff too large")
            return fake(*args, input_text=input_text)
        lm.gh = pr_diff
        lm.collect = lambda owner, exclude: ([], [])
        lm.run(lm.argparse.Namespace(owner="o", ledger="o/ledger", exclude="", dry_run=False))
        rec = fake.ledgers()["[漏れの記録] 2026-08"][0]
        self.assertEqual(rec["verdict"], "judged")
        self.assertEqual(rec["suspects"], [10])
        self.assertTrue(any("src/big.js" in p for p in self.prompts))


class Collect(unittest.TestCase):
    """collect() を偽の gh で通す（1 周目の /ai-review の指摘 L2・L3・L4・L7・L10）"""
    NOW = lm.dt.datetime(2026, 10, 20, tzinfo=lm.dt.timezone.utc)

    def setUp(self):
        self.saved = (lm.gh, lm.utcnow)
        lm.utcnow = lambda: self.NOW
        self.calls = []

    def tearDown(self):
        lm.gh, lm.utcnow = self.saved

    def fake(self, routes, fail=()):
        def gh(*args, input_text=None):
            self.calls.append(args)
            key = " ".join(args)
            for f in fail:
                if f in key:
                    raise RuntimeError("gh api %s: HTTP 410" % f)
            if args[:2] == ("repo", "list"):
                return json.dumps([{"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}])
            for k, v in routes.items():
                if k in key:
                    return v if isinstance(v, str) else json.dumps(v)
            if "--paginate" in args:
                return "[[]]"
            raise RuntimeError("gh %s: HTTP 404" % key)
        lm.gh = gh

    PR10 = {"number": 10, "merged_at": "2026-10-15T00:00:00Z", "title": "feat: x", "merge_commit_sha": "m10"}

    def base_routes(self, commits, detail_pages=None, pulls_of=None):
        r = {"repos/o/r/pulls?state=closed": [[self.PR10]],
             "repos/o/r/pulls/10/files": [[{"filename": "src/a.js"}]],
             "repos/o/r/commits?sha=main": [[{"sha": c, "commit": {"message": m, "committer": {"date": "2026-10-18T00:00:00Z"}}}
                                            for c, m in commits]],
             "pr diff 10": "+x"}
        for c, _ in commits:
            r["repos/o/r/commits/%s?per_page=100" % c] = detail_pages or [{"files": [{"filename": "src/a.js", "patch": "-a"}]}]
            r["repos/o/r/commits/%s/pulls" % c] = (pulls_of or {}).get(c, [])
        return r

    def test_commit_files_are_read_from_every_page(self):
        pages = [{"files": [{"filename": "z%d.js" % i} for i in range(300)]}, {"files": [{"filename": "src/a.js", "patch": "-a"}]}]
        self.fake(self.base_routes([("f1", "fix: a")], detail_pages=pages))
        cands, _ = lm.collect("o", set())
        self.assertEqual([s["number"] for s in cands[0]["suspects"]], [10])

    def test_issue_fetch_failure_keeps_fix_candidates(self):
        self.fake(self.base_routes([("f1", "fix: a")]), fail=("repos/o/r/issues",))
        cands, errors = lm.collect("o", set())
        self.assertEqual([c["key"] for c in cands], ["o/r@f1"])
        self.assertEqual(errors, [])  # Issue を無効にしたリポジトリ（410）は失敗に数えない

    def test_fix_commit_inside_a_merged_pr_is_not_a_candidate(self):
        pulls = {"f2": [{"number": 11, "merge_commit_sha": "m11"}], "m11": [{"number": 11, "merge_commit_sha": "m11"}]}
        self.fake(self.base_routes([("f2", "fix: レビュー中の直し"), ("m11", "fix: PR の squash")], pulls_of=pulls))
        cands, _ = lm.collect("o", set())
        self.assertEqual([c["key"] for c in cands], ["o/r@m11"])

    def test_suspect_diff_failure_is_counted_as_a_fetch_error(self):
        self.fake(self.base_routes([("f1", "fix: a")]), fail=("pr diff 10",))
        cands, errors = lm.collect("o", set())
        self.assertIn("incomplete", cands[0])
        self.assertEqual(len(errors), 1)

    def test_branch_name_is_url_encoded(self):
        def gh(*args, input_text=None):
            self.calls.append(args)
            if args[:2] == ("repo", "list"):
                return json.dumps([{"nameWithOwner": "o/r", "defaultBranchRef": {"name": "rel#1+a"}}])
            return "[[]]"
        lm.gh = gh
        lm.collect("o", set())
        paths = [a[3] for a in self.calls if a[:2] == ("api", "--paginate")]
        self.assertTrue(paths and all("rel#1+a" not in p for p in paths))
        self.assertTrue(any("rel%231%2Ba" in p for p in paths))


class RevertWindow(unittest.TestCase):
    PRS = [pr(10, "2026-10-01T00:00:00Z", ["src/a.js"]), pr(12, "2026-10-08T00:00:00Z", ["src/a.js"])]

    def test_revert_of_pr_outside_window_has_no_suspect_and_no_fallback(self):
        c = commit("r1", "2026-10-20T00:00:00Z", 'Revert "x"\n\nThis reverts commit abcdef1.', ["src/a.js"])
        out = lm.fix_candidates("o/r", [c], self.PRS, revert_pr=lambda sha: 10)  # 元の PR は 19 日前
        self.assertEqual(out[0]["suspects"], [])

    def test_revert_of_pr_not_in_list_has_no_fallback(self):
        c = commit("r2", "2026-10-15T00:00:00Z", 'Revert "x"\n\nThis reverts commit abcdef1.', ["src/a.js"])
        out = lm.fix_candidates("o/r", [c], self.PRS, revert_pr=lambda sha: 99)  # 元の PR は一覧より古い
        self.assertEqual(out[0]["suspects"], [])

    def test_revert_of_pr_inside_window_points_to_it(self):
        c = commit("r3", "2026-10-15T00:00:00Z", 'Revert "x"\n\nThis reverts commit abcdef1.', ["src/a.js"])
        out = lm.fix_candidates("o/r", [c], self.PRS, revert_pr=lambda sha: 12)
        self.assertEqual([s["number"] for s in out[0]["suspects"]], [12])


class Robust2(Run):
    """実行の頑丈さ（1 周目の /ai-review の指摘 L1・L6・L8・L9）"""
    def test_judging_stops_at_time_budget_and_still_saves(self):
        cands = [cand("o/r@%07x" % i, "2026-09-%02dT00:00:00Z" % (1 + i % 28)) for i in range(5)]
        fake = FakeGitHub()
        clock = iter([0, 0, 0, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6])
        saved = lm.monotonic
        lm.monotonic = lambda: next(clock, 10 ** 6)
        try:
            self.go(fake, cands)
        finally:
            lm.monotonic = saved
        recs = {r["key"]: r for rs in fake.ledgers().values() for r in rs}
        self.assertEqual(len(recs), 5)  # 時間で見送った分も保存する
        self.assertLess(len(self.judged), 5)
        skipped = [r for r in recs.values() if "時間の上限" in (r.get("rationale") or "")]
        self.assertTrue(skipped)
        self.assertTrue(all(not r.get("attempts") for r in skipped))  # 見送りは試した回数に数えない（L6）

    def test_over_cap_new_candidates_are_not_counted_as_tried(self):
        cands = [cand("o/r@%07x" % i, "2026-09-%02dT00:00:00Z" % (1 + i % 28)) for i in range(lm.MAX_PER_RUN + 3)]
        fake = FakeGitHub()
        self.go(fake, cands)
        recs = [r for rs in fake.ledgers().values() for r in rs if "上限で見送り" in (r.get("rationale") or "")]
        self.assertEqual(len(recs), 3)
        self.assertTrue(all(not r.get("attempts") for r in recs))

    def test_label_is_not_recolored(self):
        k = "o/r@abcdeff"
        fake = FakeGitHub()
        self.replies = [SERIOUS, SERIOUS]
        self.go(fake, [cand(k, "2026-09-30T00:00:00Z")])
        labels = [c for c in fake.calls if c[:2] == ("label", "create")]
        self.assertTrue(labels and all("--force" not in c for c in labels))

    def test_fetch_errors_make_the_exit_code_nonzero_after_saving(self):
        fake = FakeGitHub()
        rc = self.go(fake, [cand("o/r@abcdee0", "2026-09-30T00:00:00Z")], errors=["o/x: HTTP 403"])
        self.assertNotEqual(rc, 0)
        self.assertTrue(fake.ledgers())  # 保存は済ませてから


class PublicLog(Run):
    """公開リポジトリの Actions のログに private の情報を出さない（1 周目の /ai-review の指摘 L5）"""
    def capture(self, fake, cands, errors):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.go(fake, cands, errors=errors)
        return out.getvalue() + err.getvalue()

    def test_log_has_no_repo_names_or_serious_counts(self):
        os.environ.pop("LEAK_METRICS_LOG_DETAILS", None)
        self.replies = [SERIOUS, SERIOUS]
        log = self.capture(FakeGitHub(), [cand("o/secret-repo@abcde01", "2026-09-30T00:00:00Z")],
                           ["o/private-thing: gh api repos/o/private-thing/pulls: HTTP 403 Forbidden"])
        self.assertNotIn("private-thing", log)
        self.assertNotIn("secret-repo", log)
        self.assertNotIn("重大", log)
        self.assertIn("HTTP 403", log)  # 種類だけは出す

    def test_details_only_when_asked(self):
        os.environ["LEAK_METRICS_LOG_DETAILS"] = "1"
        try:
            log = self.capture(FakeGitHub(), [], ["o/private-thing: HTTP 403"])
        finally:
            os.environ.pop("LEAK_METRICS_LOG_DETAILS", None)
        self.assertIn("private-thing", log)


if __name__ == "__main__":
    unittest.main()
