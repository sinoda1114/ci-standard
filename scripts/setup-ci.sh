#!/usr/bin/env bash
# 既存リポジトリに標準CIを後付けするスクリプト。
#
# やること:
#   1. 言語判定（package.json → Node / pyproject.toml・requirements.txt → Python）
#   2. .github/workflows/ci.yml に呼び出し側ワークフローを配置（既存 ci.yml は .bak 退避）
#   3. main ブランチ保護を設定（必須チェック: ci / build（+ ci / e2e）、会話解決必須）
#
# 使い方:
#   scripts/setup-ci.sh /path/to/repo          # 配置 + 保護設定
#   scripts/setup-ci.sh /path/to/repo --no-protect   # 配置のみ
#
# 注意:
#   - commit / push はしない（内容を確認してから各自でコミットする）
#   - private リポジトリのブランチ保護は GitHub Free では設定不可（403）。
#     その場合は警告を出して続行する。
set -euo pipefail

STANDARD_REPO="sinoda1114/ci-standard"
TARGET="${1:?対象リポジトリのパスを指定してください}"
PROTECT=true
[ "${2:-}" = "--no-protect" ] && PROTECT=false

cd "$TARGET"
[ -d .git ] || { echo "ERROR: $TARGET は git リポジトリではありません"; exit 1; }

NWO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)

# 1. 言語判定
if [ -f package.json ]; then
  LANG_KIND="node"
  CONTEXTS='["ci / build", "ci / e2e"]'
elif [ -f pyproject.toml ] || [ -f requirements.txt ]; then
  LANG_KIND="python"
  CONTEXTS='["ci / build"]'
else
  echo "WARN: $NWO — Node/Python いずれの構成も検出できず。スキップします"
  exit 0
fi
echo "== $NWO: $LANG_KIND として設定します (default branch: $DEFAULT_BRANCH)"

# 2. 呼び出し側 ci.yml を配置
mkdir -p .github/workflows
if [ -f .github/workflows/ci.yml ]; then
  cp .github/workflows/ci.yml .github/workflows/ci.yml.bak
  echo "   既存 ci.yml を ci.yml.bak に退避"
fi
cat > .github/workflows/ci.yml <<EOF
# 標準CI呼び出し（実体: https://github.com/${STANDARD_REPO}）
name: CI

on:
  pull_request:
  push:
    branches: [${DEFAULT_BRANCH}]

jobs:
  ci:
    uses: ${STANDARD_REPO}/.github/workflows/${LANG_KIND}-ci.yml@main
    secrets: inherit
EOF
echo "   .github/workflows/ci.yml 配置完了（コミットは手動で）"

# 3. ブランチ保護
if $PROTECT; then
  if gh api -X PUT "/repos/${NWO}/branches/${DEFAULT_BRANCH}/protection" --input - >/dev/null <<EOF
{
  "required_status_checks": { "strict": false, "contexts": ${CONTEXTS} },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
  then
    echo "   ブランチ保護設定完了 (必須: ${CONTEXTS})"
  else
    echo "   WARN: ブランチ保護の設定に失敗（private + Free プランの場合は仕様）。CI配置のみ有効"
  fi
fi
echo "== $NWO 完了"
