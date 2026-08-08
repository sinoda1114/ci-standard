#!/usr/bin/env bash
# 標準CI一括ロールアウト（ci.yml配布のみ。ブランチ保護は protect-all.sh で別実行）
set -uo pipefail

WORK="${TMPDIR:-/tmp}/ci-standard-rollout"
STANDARD_REPO="sinoda1114/ci-standard"
mkdir -p "$WORK"
RESULT="$WORK/result.tsv"
: > "$RESULT"

# 除外: 標準リポ自身 / PR対応済み / チーム開発 / 空リポジトリ
EXCLUDE="ci-standard claude-project-starter pj-pilot teamdev-2023-apr-team1 desktop-tutorial flue-test2"

REPOS=$(gh api '/user/repos?per_page=100&sort=pushed' -q '.[] | select(.archived==false and .fork==false) | .name')

for R in $REPOS; do
  case " $EXCLUDE " in *" $R "*) echo "$R	skip	除外リスト" >> "$RESULT"; continue;; esac

  DIR="$WORK/$R"
  rm -rf "$DIR"
  if ! git clone --quiet --depth 1 "https://github.com/sinoda1114/$R.git" "$DIR" 2>/dev/null; then
    echo "$R	error	clone失敗" >> "$RESULT"; continue
  fi
  cd "$DIR"
  BRANCH=$(git rev-parse --abbrev-ref HEAD)

  # 言語判定
  if [ -f package.json ]; then KIND="node"
  elif [ -f pyproject.toml ] || [ -f requirements.txt ]; then KIND="python"
  else echo "$R	skip	Node/Python構成なし" >> "$RESULT"; continue; fi

  # 既に標準CI導入済みならスキップ
  if [ -f .github/workflows/ci.yml ] && grep -q "$STANDARD_REPO" .github/workflows/ci.yml; then
    echo "$R	skip	導入済み" >> "$RESULT"; continue
  fi

  mkdir -p .github/workflows
  BAK=""
  if [ -f .github/workflows/ci.yml ]; then
    cp .github/workflows/ci.yml .github/workflows/ci.yml.bak
    BAK="（旧ci.ymlは.bakに退避）"
  fi
  cat > .github/workflows/ci.yml <<EOF
# 標準CI呼び出し（実体: https://github.com/${STANDARD_REPO}）
name: CI

on:
  pull_request:
  push:
    branches: [${BRANCH}]

concurrency:
  group: ci-\${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  ci:
    uses: ${STANDARD_REPO}/.github/workflows/${KIND}-ci.yml@main
    secrets: inherit
EOF
  git add .github/workflows
  git -c user.name="Yukihiro Shinoda" -c user.email="52367401+sinoda1114@users.noreply.github.com" \
    commit --quiet -m "ci: 標準CI（${STANDARD_REPO}）を導入 ${BAK}"
  if git push --quiet origin "$BRANCH" 2>/dev/null; then
    echo "$R	ok	$KIND $BAK" >> "$RESULT"
  else
    echo "$R	error	push失敗（保護等）" >> "$RESULT"
  fi
done
echo "===== 完了 ====="
column -t -s $'\t' "$RESULT"
