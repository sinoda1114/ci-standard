#!/usr/bin/env python3
"""配布する workflow-lint ジョブ（jactionlint）の本体を、書かれたとおりに実行して確かめる。

node-ci.yml・python-ci.yml の workflow-lint ジョブは、呼び出し側の全リポジトリの必須チェック ci / build に効く。
フラグや終了コードの扱いを誤ると全リポジトリが一斉に落ちるので、self-test で実物を動かす。
  1. 2 つのファイルのステップ（env と run）が同一であること
  2. build が workflow-lint の結果で落ちる配線（needs・!cancelled()・最後の結果のステップ）があること
  3. このリポジトリ（correctness の違反なし）で終了コード 0
  4. 標準 CI を @main で呼ぶ見本で終了コード 0、SHA 固定の警告も助言の実行の失敗も出ないこと
  5. run: に読めない式を書いた見本のリポジトリで終了コード 1
使い方: python3 scripts/run-workflow-lint-step.py（GitHub Actions の上で。RUNNER_TEMP と curl・sha256sum を使う）
"""
import os
import subprocess
import sys
import tempfile

import yaml

STEP_NAME = "ワークフローの検査（jactionlint）"
WORKFLOWS = (".github/workflows/node-ci.yml", ".github/workflows/python-ci.yml")
RESULT_STEP_NAME = "ワークフローの検査（jactionlint）の結果"
CALLER_WORKFLOW = """name: CI
on:
  pull_request:
permissions:
  contents: read
jobs:
  ci:
    uses: sinoda1114/ci-standard/.github/workflows/node-ci.yml@main
"""
BROKEN_WORKFLOW = """on: push
jobs:
  t:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -c 'print(f"${{x}}")'
"""


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_wiring(path, workflow):
    """workflow-lint が失敗したとき、必須チェック ci / build が確実に落ちる配線になっているか。"""
    build = workflow["jobs"]["build"]
    needs = build.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    errors = []
    if "workflow-lint" not in needs:
        errors.append("build の needs に workflow-lint が無い")
    if "!cancelled()" not in str(build.get("if", "")):
        errors.append("build の if: が !cancelled() でない（workflow-lint の失敗で build がスキップになり、必須チェックをすり抜ける）")
    last = build["steps"][-1]
    if last.get("name") != RESULT_STEP_NAME or "!cancelled()" not in str(last.get("if", "")):
        errors.append(f"build の最後のステップが「{RESULT_STEP_NAME}」（if: !cancelled()）でない")
    elif last.get("env", {}).get("WORKFLOW_LINT_RESULT") != "${{ needs.workflow-lint.result }}":
        errors.append("最後のステップが needs.workflow-lint.result を見ていない")
    for e in errors:
        print(f"::error file={path}::{e}")
    return not errors


def load_step(workflow, path):
    steps = workflow["jobs"]["workflow-lint"]["steps"]
    matches = [s for s in steps if s.get("name") == STEP_NAME]
    if len(matches) != 1:
        sys.exit(f"{path}: workflow-lint ジョブに「{STEP_NAME}」のステップがちょうど 1 つ無い")
    step = matches[0]
    return {k: str(v) for k, v in step.get("env", {}).items()}, step["run"]


def run_step(env_vars, script, cwd):
    """ステップを動かし、(終了コード, 標準出力と標準エラー) を返す。出力はそのままログにも出す。"""
    with tempfile.TemporaryDirectory() as runner_temp:
        env = {**os.environ, **env_vars, "RUNNER_TEMP": runner_temp}
        # GitHub Actions の既定のシェル（bash -e {0}）と同じ起動のしかた
        proc = subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(proc.stdout, end="")
    return proc.returncode, proc.stdout


def make_repo(root, workflow_text):
    os.makedirs(os.path.join(root, ".github", "workflows"))
    with open(os.path.join(root, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as f:
        f.write(workflow_text)
    subprocess.run(["git", "init", "-q", root], check=True)


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workflows = {p: load_workflow(os.path.join(repo_root, p)) for p in WORKFLOWS}
    if not all([check_wiring(p, w) for p, w in workflows.items()]):
        sys.exit(1)
    steps = [load_step(w, p) for p, w in workflows.items()]
    if steps[0] != steps[1]:
        sys.exit("::error::node-ci.yml と python-ci.yml の workflow-lint のステップが違う")
    env_vars, script = steps[0]

    rc, _ = run_step(env_vars, script, repo_root)
    print(f"このリポジトリ: 終了コード {rc}（期待 0）")
    if rc != 0:
        sys.exit("::error::違反の無いこのリポジトリで workflow-lint のステップが失敗した")

    with tempfile.TemporaryDirectory() as caller:
        make_repo(caller, CALLER_WORKFLOW)
        rc, out = run_step(env_vars, script, caller)
    print(f"標準 CI を @main で呼ぶ見本: 終了コード {rc}（期待 0）")
    if rc != 0 or "must be pinned" in out or "助言の実行が失敗" in out:
        sys.exit("::error::標準 CI の呼び出し（@main）で、失敗・SHA 固定の警告・助言の実行の失敗のどれかが出た")

    with tempfile.TemporaryDirectory() as broken:
        make_repo(broken, BROKEN_WORKFLOW)
        rc, _ = run_step(env_vars, script, broken)
    print(f"壊れた見本: 終了コード {rc}（期待 1）")
    if rc != 1:
        sys.exit("::error::読めない式のある見本で workflow-lint のステップが終了コード 1 を返さなかった")
    print("workflow-lint のステップは期待どおり")


if __name__ == "__main__":
    main()
