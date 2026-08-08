#!/usr/bin/env bash
# 標準CI自動見回り（sweeper）
#
# 全リポジトリを走査し、以下を強制する。人間・エージェントの記憶に依存しない。
#   1. Node/Python リポジトリに標準CI呼び出し（ci.yml）が無ければ自動配置
#      （既存の独自 ci.yml は ci.yml.bak として退避コミット）
#   2. 標準CI導入済みリポジトリにブランチ保護を適用（冪等）
#
# GitHub Actions（.github/workflows/sweeper.yml）から日次実行される想定。
# ローカルでも `GH_TOKEN=... bash scripts/sweep.sh` で実行可能。
#
# 必要権限（fine-grained PAT）: All repositories / Contents: RW / Administration: RW
set -uo pipefail

OWNER="sinoda1114"
STANDARD_REPO="${OWNER}/ci-standard"
# 除外: 標準リポ自身 / チーム開発 / 空チュートリアル
EXCLUDE="ci-standard teamdev-2023-apr-team1 desktop-tutorial flue-test2"

exists() { # exists <repo> <path> → 0/1
  gh api "/repos/${OWNER}/$1/contents/$2" -q .sha >/dev/null 2>&1
}

put_file() { # put_file <repo> <branch> <path> <message> <content> [sha]
  local R=$1 B=$2 P=$3 M=$4 C=$5 S=${6:-}
  local ARGS=(-X PUT "/repos/${OWNER}/${R}/contents/${P}" -f message="$M" -f branch="$B" -f content="$(printf '%s' "$C" | base64)")
  [ -n "$S" ] && ARGS+=(-f sha="$S")
  gh api "${ARGS[@]}" >/dev/null 2>&1
}

protect() { # protect <repo> <branch> <contexts-json>
  gh api -X PUT "/repos/${OWNER}/$1/branches/$2/protection" --input - >/dev/null 2>&1 <<EOF
{
  "required_status_checks": { "strict": false, "contexts": $3 },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
}

echo "| repo | 結果 |"
echo "|---|---|"

gh api '/user/repos?per_page=100' -q '.[] | select(.archived==false and .fork==false) | "\(.name)\t\(.default_branch)"' |
while IFS=$'\t' read -r NAME BRANCH; do
  case " $EXCLUDE " in *" $NAME "*) continue;; esac

  # 言語判定（Contents API のみ、clone不要）
  if exists "$NAME" package.json; then
    KIND="node"
    # e2e必須はplaywright設定のあるリポジトリのみ（未導入リポジトリをマージ不能にしないため）
    if exists "$NAME" playwright.config.ts || exists "$NAME" playwright.config.js || exists "$NAME" playwright.config.mjs; then
      CONTEXTS='["ci / build", "ci / e2e"]'
    else
      CONTEXTS='["ci / build"]'
    fi
  elif exists "$NAME" pyproject.toml || exists "$NAME" requirements.txt; then
    KIND="python"; CONTEXTS='["ci / build"]'
  else
    continue
  fi

  CI_JSON=$(gh api "/repos/${OWNER}/${NAME}/contents/.github/workflows/ci.yml" 2>/dev/null || true)
  CI_SHA=$(printf '%s' "$CI_JSON" | jq -r '.sha // empty' 2>/dev/null)
  CI_BODY=$(printf '%s' "$CI_JSON" | jq -r '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || true)

  if printf '%s' "$CI_BODY" | grep -q "$STANDARD_REPO"; then
    STATUS="導入済み"
  else
    # 既存の独自CIを退避（workflows/ 外へ置く: .yml のままだとGitHubがワークフローとして解釈するため）
    if [ -n "$CI_SHA" ]; then
      put_file "$NAME" "$BRANCH" ".github/ci.yml.bak" \
        "ci: 標準CI導入に伴い旧ci.ymlを退避" "$CI_BODY" || true
    fi
    NEW_CI="# 標準CI呼び出し（実体: https://github.com/${STANDARD_REPO}）
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
"
    if put_file "$NAME" "$BRANCH" ".github/workflows/ci.yml" \
      "ci: 標準CI（${STANDARD_REPO}）を自動導入 [sweeper]" "$NEW_CI" "$CI_SHA"; then
      STATUS="導入 ($KIND)"
    else
      STATUS="導入失敗（保護ブランチ等）"
    fi
  fi

  if protect "$NAME" "$BRANCH" "$CONTEXTS"; then
    STATUS="$STATUS / 保護OK"
  else
    STATUS="$STATUS / 保護不可(private+Free?)"
  fi
  echo "| $NAME | $STATUS |"
done
echo ""
echo "sweep 完了: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
