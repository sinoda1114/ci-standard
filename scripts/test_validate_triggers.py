#!/usr/bin/env python3
"""validate-triggers.py の単体テスト。

ゲート自身が壊れて常に成功を返すようになっても、誰も気付けない。
「正しいものを通す」だけでなく「壊れたものを確実に落とす」ことを検証する。

実行: python3 -m unittest discover -s scripts -p 'test_validate_triggers.py'
"""
import importlib.util, pathlib, tempfile, unittest, os, sys

_spec = importlib.util.spec_from_file_location(
    "validate_triggers", pathlib.Path(__file__).parent / "validate-triggers.py"
)
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)

import yaml


def problems(src):
    """YAML 文字列を検査し、問題メッセージの一覧を返す。"""
    return vt.check("t.yml", yaml.safe_load(src))


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

    def test_repository_dispatch_types_are_freeform(self):
        """利用者が任意の文字列を決められるので突き合わせない。"""
        self.assertEqual(problems("on:\n  repository_dispatch:\n    types: [my-custom-event]\n"), [])

    def test_branches_filter_is_not_types(self):
        self.assertEqual(problems("on:\n  push:\n    branches: [main]\n"), [])


class TestRobustness(unittest.TestCase):
    def test_empty_file_does_not_crash(self):
        """空ファイルで AttributeError を出して落ちていた。"""
        self.assertEqual(vt.check("t.yml", None), [])

    def test_non_dict_document(self):
        self.assertEqual(vt.check("t.yml", ["a", "b"]), [])

    def test_no_on_section(self):
        self.assertEqual(problems("name: x\njobs: {}\n"), [])

    def test_on_read_as_boolean_key(self):
        """YAML 1.1 では裸の on: が True として読まれる。そちらの経路も検査する。"""
        doc = yaml.safe_load("on:\n  no_such_event:\n")
        self.assertIn(True, doc)  # 前提の確認（計測器の検証）
        self.assertEqual(len(vt.check("t.yml", doc)), 1)


class TestFileDiscovery(unittest.TestCase):
    def _run_in(self, files):
        d = tempfile.mkdtemp()
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


if __name__ == "__main__":
    unittest.main()
