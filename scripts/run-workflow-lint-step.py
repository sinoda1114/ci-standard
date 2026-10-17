#!/usr/bin/env python3
"""配布する workflow-lint ジョブ（jactionlint）の本体を、書かれたとおりに実行して確かめる。

node-ci.yml・python-ci.yml の workflow-lint ジョブは、呼び出し側の全リポジトリの必須チェック ci / build に効く。
フラグや終了コードの扱いを誤ると全リポジトリが一斉に落ちるので、self-test で実物を動かす。
  1. 2 つのファイルのステップ（env と run）が同一であること
  2. このリポジトリ（correctness の違反なし）で終了コード 0
  3. run: に読めない式を書いた見本のリポジトリで終了コード 1
使い方: python3 scripts/run-workflow-lint-step.py（GitHub Actions の上で。RUNNER_TEMP と curl・sha256sum を使う）
"""
import os
import subprocess
import sys
import tempfile

import yaml

STEP_NAME = "ワークフローの検査（jactionlint）"
WORKFLOWS = (".github/workflows/node-ci.yml", ".github/workflows/python-ci.yml")
BROKEN_WORKFLOW = """on: push
jobs:
  t:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -c 'print(f"${{x}}")'
"""


def load_step(path):
    with open(path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)
    steps = workflow["jobs"]["workflow-lint"]["steps"]
    matches = [s for s in steps if s.get("name") == STEP_NAME]
    if len(matches) != 1:
        sys.exit(f"{path}: workflow-lint ジョブに「{STEP_NAME}」のステップがちょうど 1 つ無い")
    step = matches[0]
    return {k: str(v) for k, v in step.get("env", {}).items()}, step["run"]


def run_step(env_vars, script, cwd):
    with tempfile.TemporaryDirectory() as runner_temp:
        env = {**os.environ, **env_vars, "RUNNER_TEMP": runner_temp}
        # GitHub Actions の既定のシェル（bash -e {0}）と同じ起動のしかた
        return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env).returncode


def make_broken_repo(root):
    os.makedirs(os.path.join(root, ".github", "workflows"))
    with open(os.path.join(root, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as f:
        f.write(BROKEN_WORKFLOW)
    subprocess.run(["git", "init", "-q", root], check=True)


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    steps = [load_step(os.path.join(repo_root, p)) for p in WORKFLOWS]
    if steps[0] != steps[1]:
        sys.exit("::error::node-ci.yml と python-ci.yml の workflow-lint のステップが違う")
    env_vars, script = steps[0]

    rc = run_step(env_vars, script, repo_root)
    print(f"このリポジトリ: 終了コード {rc}（期待 0）")
    if rc != 0:
        sys.exit("::error::違反の無いこのリポジトリで workflow-lint のステップが失敗した")

    with tempfile.TemporaryDirectory() as broken:
        make_broken_repo(broken)
        rc = run_step(env_vars, script, broken)
    print(f"壊れた見本: 終了コード {rc}（期待 1）")
    if rc != 1:
        sys.exit("::error::読めない式のある見本で workflow-lint のステップが終了コード 1 を返さなかった")
    print("workflow-lint のステップは期待どおり")


if __name__ == "__main__":
    main()
