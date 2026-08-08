#!/usr/bin/env bash
# 標準CI自動見回り（sweeper）
#
# 全リポジトリを走査し、以下を強制する。人間・エージェントの記憶に依存しない。
#   1. Node/Python リポジトリに標準CI呼び出し（ci.yml）が無ければ自動配置
#      （既存の独自 ci.yml は .github/ci.yml.bak として退避コミット）
#   2. 標準CI導入「済み」を検証できたリポジトリだけにブランチ保護を適用（冪等）
#      ※ CI無しで保護だけ効くと push/マージ不能に陥るため、順序を厳守する
#   3. 「保護あり・標準CI無し」の矛盾状態を検出したら、保護を一時解除して
#      導入→再保護で自動復旧する
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
ERRLOG=$(mktemp)

exists() { # exists <repo> <path> → 0/1
  gh api "/repos/${OWNER}/$1/contents/$2" -q .sha >/dev/null 2>&1
}

put_file() { # put_file <repo> <branch> <path> <message> <content> [sha]
  local R=$1 B=$2 P=$3 M=$4 C=$5 S=${6:-}
  local ARGS=(-X PUT "/repos/${OWNER}/${R}/contents/${P}" -f message="$M" -f branch="$B" -f content="$(printf '%s' "$C" | base64 | tr -d '\n')")
  [ -n "$S" ] && ARGS+=(-f sha="$S")
  if ! gh api "${ARGS[@]}" >/dev/null 2>>"$ERRLOG"; then
    echo "::warning::put_file 失敗 ${R}/${P}: $(tail -1 "$ERRLOG")"
    return 1
  fi
}

is_protected() { # is_protected <repo> <branch>
  gh api "/repos/${OWNER}/$1/branches/$2/protection" >/dev/null 2>&1
}

unprotect() { # unprotect <repo> <branch>
  gh api -X DELETE "/repos/${OWNER}/$1/branches/$2/protection" >/dev/null 2>&1
}

protect() { # protect <repo> <branch> <contexts-json>
  gh api -X PUT "/repos/${OWNER}/$1/branches/$2/protection" --input - >/dev/null 2>>"$ERRLOG" <<EOF
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

standard_ci_body() { # standard_ci_body <kind> <branch>
  cat <<EOF
# 標準CI呼び出し（実体: https://github.com/${STANDARD_REPO}）
name: CI

on:
  pull_request:
  push:
    branches: [$2]

concurrency:
  group: ci-\${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  ci:
    uses: ${STANDARD_REPO}/.github/workflows/$1-ci.yml@main
    secrets: inherit
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

  CI_JSON=$(gh api "/repos/${OWNER}/${NAME}/contents/.github/workflows/ci.yml?ref=${BRANCH}" 2>/dev/null || true)
  CI_SHA=$(printf '%s' "$CI_JSON" | jq -r '.sha // empty' 2>/dev/null)
  CI_BODY=$(printf '%s' "$CI_JSON" | jq -r '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || true)

  INSTALLED=false
  if printf '%s' "$CI_BODY" | grep -q "$STANDARD_REPO"; then
    INSTALLED=true
    STATUS="導入済み"
  else
    # 「保護あり・標準CI無し」の矛盾状態なら保護を一時解除して復旧する
    UNPROTECTED_FOR_FIX=false
    if is_protected "$NAME" "$BRANCH"; then
      if unprotect "$NAME" "$BRANCH"; then
        UNPROTECTED_FOR_FIX=true
      fi
    fi

    # 既存の独自CIを退避（workflows/ 外へ: .ymlのままだとワークフローとして解釈されるため）
    if [ -n "$CI_SHA" ]; then
      OLD_BAK_SHA=$(gh api "/repos/${OWNER}/${NAME}/contents/.github/ci.yml.bak?ref=${BRANCH}" -q .sha 2>/dev/null || true)
      put_file "$NAME" "$BRANCH" ".github/ci.yml.bak" \
        "ci: 標準CI導入に伴い旧ci.ymlを退避 [sweeper]" "$CI_BODY" "$OLD_BAK_SHA" || true
    fi

    if put_file "$NAME" "$BRANCH" ".github/workflows/ci.yml" \
      "ci: 標準CI（${STANDARD_REPO}）を自動導入 [sweeper]" "$(standard_ci_body "$KIND" "$BRANCH")" "$CI_SHA"; then
      INSTALLED=true
      STATUS="導入 ($KIND)"
    else
      STATUS="導入失敗（ログ参照）"
      $UNPROTECTED_FOR_FIX && STATUS="$STATUS ※保護は解除したまま（CI無しで保護すると詰むため）"
    fi
  fi

  # 保護は「標準CIが存在する」ことを検証できた場合のみ適用する
  if $INSTALLED; then
    if protect "$NAME" "$BRANCH" "$CONTEXTS"; then
      STATUS="$STATUS / 保護OK"
    else
      STATUS="$STATUS / 保護不可(private+Free?)"
    fi
  fi
  echo "| $NAME | $STATUS |"
done
echo ""
echo "sweep 完了: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
