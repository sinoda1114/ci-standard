#!/usr/bin/env python3
"""validate-triggers.py の単体テスト。

ゲート自身が壊れて常に成功を返すようになっても、誰も気付けない。
「正しいものを通す」だけでなく「壊れたものを確実に落とす」ことを検証する。

実行: python3 -m unittest discover -s scripts -p 'test_validate_triggers.py'
"""
import importlib.util
import os
import pathlib
import tempfile
import unittest

import yaml

_spec = importlib.util.spec_from_file_location(
    "validate_triggers", pathlib.Path(__file__).parent / "validate-triggers.py"
)
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)


def problems(src: str) -> list[str]:
    """YAML 文字列を検査し、問題メッセージの一覧を返す。"""
    return [p.text for p in vt.check("t.yml", yaml.safe_load(src))]


def kinds(src: str) -> list[str]:
    """YAML 文字列を検査し、問題の種別の一覧を返す。"""
    return [p.kind for p in vt.check("t.yml", yaml.safe_load(src))]


class TestEventNames(unittest.TestCase):
    def test_valid_event_passes(self):
        self.assertEqual(problems("on:\n  push:\n"), [])

    def test_the_2026_09_21_outage_is_caught(self):
        """実際に全リポジトリを止めた入力。これを通したらゲートの意味が無い。"""
        p = problems("on:\n  pull_request_review_thread:\n    types: [resolved]\n")
        self.assertEqual(len(p), 1)
        self.assertIn("pull_request_review_thread", p[0])
        self.assertIn("代替となるイベントは無い", p[0])

    def test_alias_suggests_correct_name(self):
        p = problems("on:\n  pull_request_comment:\n    types: [created]\n")
        self.assertEqual(len(p), 1)
        self.assertIn("issue_comment を使う", p[0])

    def test_unknown_event_is_caught(self):
        self.assertEqual(len(problems("on:\n  no_such_event:\n")), 1)

    def test_string_form(self):
        self.assertEqual(problems("on: push\n"), [])
        self.assertEqual(len(problems("on: no_such_event\n")), 1)

    def test_list_form(self):
        self.assertEqual(problems("on: [push, pull_request]\n"), [])
        self.assertEqual(len(problems("on: [push, no_such_event]\n")), 1)

    def test_mapping_inside_list_is_reported_not_crash(self):
        """on: のリストにマッピングが混ざると TypeError で落ちていた。形式不正として報告する。"""
        p = problems("on: [push, {pull_request: {types: [opened]}}]\n")
        self.assertEqual(len(p), 1)
        self.assertIn("形式が不正", p[0])


class TestTypes(unittest.TestCase):
    def test_valid_types_pass(self):
        self.assertEqual(problems("on:\n  pull_request_review:\n    types: [submitted]\n"), [])

    def test_invalid_type_is_caught(self):
        """イベント名は正しいが types が存在しない。名前だけ見るゲートはここを素通りした。"""
        p = problems("on:\n  pull_request_review:\n    types: [resolved, unresolved]\n")
        self.assertEqual(len(p), 2)
        self.assertIn("resolved", p[0])
        self.assertIn("submitted", p[0])  # 使える値を案内している

    def test_types_on_event_that_takes_none(self):
        p = problems("on:\n  push:\n    types: [created]\n")
        self.assertEqual(len(p), 1)
        self.assertIn("types を取らない", p[0])

    def test_string_types(self):
        self.assertEqual(problems("on:\n  pull_request_review:\n    types: submitted\n"), [])
        self.assertEqual(len(problems("on:\n  pull_request_review:\n    types: resolved\n")), 1)

    def test_branches_filter_is_not_types(self):
        self.assertEqual(problems("on:\n  push:\n    branches: [main]\n"), [])

    def test_check_suite_accepts_only_completed(self):
        """公式ドキュメント（2026-09-23 確認）: "Although only the completed activity type is supported"。
        レビューで「requested / rerequested も使える」と指摘されたが、それは webhook 側の値で
        Actions では使えない。将来この許可リストを「直して」しまわないよう固定する。"""
        self.assertEqual(problems("on:\n  check_suite:\n    types: [completed]\n"), [])
        self.assertEqual(len(problems("on:\n  check_suite:\n    types: [requested]\n")), 1)

    def test_image_version_takes_no_types(self):
        """公式ドキュメント（2026-09-23 確認）の Activity types 列は Not applicable。"""
        self.assertEqual(problems("on:\n  image_version:\n"), [])
        p = problems("on:\n  image_version:\n    types: [published]\n")
        self.assertEqual(len(p), 1)
        self.assertIn("types を取らない", p[0])

    def test_pull_request_target_shares_pull_request_types(self):
        """二重管理でドリフトしないよう同じ集合を参照する。"""
        self.assertIs(vt.EVENTS["pull_request"], vt.EVENTS["pull_request_target"])


class TestRepositoryDispatch(unittest.TestCase):
    def test_freeform_type_names_pass(self):
        """利用者が任意の文字列を決められるので、値は突き合わせない。"""
        self.assertEqual(problems("on:\n  repository_dispatch:\n    types: [my-custom-event]\n"), [])
        self.assertEqual(problems("on:\n  repository_dispatch:\n    types: deploy\n"), [])

    def test_mapping_types_is_caught(self):
        """値は自由でも構造は文字列かそのリストでなければならない。"""
        self.assertEqual(len(problems("on:\n  repository_dispatch:\n    types: {foo: bar}\n")), 1)

    def test_number_types_is_caught(self):
        self.assertEqual(len(problems("on:\n  repository_dispatch:\n    types: 123\n")), 1)

    def test_non_string_element_is_caught(self):
        self.assertEqual(len(problems("on:\n  repository_dispatch:\n    types: [ok, 123]\n")), 1)


class TestSeverityWording(unittest.TestCase):
    """イベント名の誤りは 2026-09-21 に構文エラーになることを実証した。
    types の誤りが構文エラーか単に起動しないだけかは未検証なので、同じ断定をしない。"""

    def test_unknown_event_is_classified_as_event(self):
        self.assertEqual(kinds("on:\n  no_such_event:\n"), ["event"])

    def test_bad_type_is_classified_as_types(self):
        self.assertEqual(kinds("on:\n  pull_request_review:\n    types: [resolved]\n"), ["types"])

    def test_summary_for_types_only_does_not_claim_syntax_error(self):
        summary = vt.summarize([vt.Problem("types", "x")])
        self.assertNotIn("構文エラー", summary)

    def test_summary_for_event_claims_syntax_error(self):
        summary = vt.summarize([vt.Problem("event", "x")])
        self.assertIn("構文エラー", summary)


class TestRobustness(unittest.TestCase):
    def test_empty_file_is_reported_not_crash(self):
        """空ファイルで AttributeError を出して落ちていた。on: が無い扱いで報告する。"""
        self.assertEqual(len(vt.check("t.yml", None)), 1)

    def test_non_dict_document(self):
        self.assertEqual(len(vt.check("t.yml", ["a", "b"])), 1)

    def test_missing_on_section_is_caught(self):
        """ワークフローに on: が無いと GitHub は起動しない。reusable workflow も on: workflow_call を持つ。"""
        p = problems("name: x\njobs: {}\n")
        self.assertEqual(len(p), 1)
        self.assertIn("on:", p[0])

    def test_on_read_as_boolean_key(self):
        """YAML 1.1 では裸の on: が True として読まれる。そちらの経路も検査する。"""
        doc = yaml.safe_load("on:\n  no_such_event:\n")
        self.assertIn(True, doc)  # 前提の確認（計測器の検証）
        self.assertEqual(len(vt.check("t.yml", doc)), 1)


class TestFileDiscovery(unittest.TestCase):
    def _run_in(self, files: dict[str, str]) -> int:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github/workflows"), exist_ok=True)
            for name, body in files.items():
                with open(os.path.join(d, ".github/workflows", name), "w") as fh:
                    fh.write(body)
            cwd = os.getcwd()
            try:
                os.chdir(d)
                return vt.main(["validate-triggers.py"])
            finally:
                os.chdir(cwd)

    def test_yaml_extension_is_checked(self):
        """GitHub は .yaml も読む。.yml だけ見ていると見逃す。"""
        rc = self._run_in({"a.yaml": "on:\n  pull_request_review_thread:\n    types: [resolved]\n"})
        self.assertEqual(rc, 1)

    def test_yml_extension_is_checked(self):
        rc = self._run_in({"a.yml": "on:\n  pull_request_review_thread:\n"})
        self.assertEqual(rc, 1)

    def test_valid_files_pass(self):
        rc = self._run_in({"a.yml": "on:\n  push:\n", "b.yaml": "on:\n  pull_request:\n    types: [opened]\n"})
        self.assertEqual(rc, 0)

    def test_unreadable_yaml_is_reported(self):
        rc = self._run_in({"a.yml": "on:\n  push:\n   bad: [unclosed\n"})
        self.assertEqual(rc, 1)

    def test_zero_files_fails(self):
        """対象が 0 件でも OK を返していた。cwd 違い・改名・引数ミスで検査が走らず緑になる経路。"""
        self.assertEqual(self._run_in({}), 1)

    def test_nonexistent_directory_fails(self):
        self.assertEqual(vt.main(["validate-triggers.py", "no/such/dir"]), 1)


if __name__ == "__main__":
    unittest.main()
