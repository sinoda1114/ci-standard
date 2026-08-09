#!/usr/bin/env bash
# リポジトリ設定の収束エンジン（sweeper）
#
# 全リポジトリを走査し、repo-policy.yml が宣言する「あるべき状態」へ毎日収束させる。
# 人間・エージェントの記憶に依存しない。何度実行しても同じ結果になる（冪等）。
#
# A. 全リポジトリ共通の運用設定
#   1. type:* ラベル（種別分類）の作成・説明/色の是正
#   2. origin/HEAD 相当（既定ブランチ）の確認 ※GitHub側は常に設定済みのため報告のみ
#   3. Secret scanning / push protection の有効化（public は無料）
#   4. Dependabot 設定ファイルの配布
#   4b. セッション間連絡スキル（.claude/skills/pair-message/）の配布
#
# B. Node/Python リポジトリの CI/CD
#   5. 標準CI呼び出し（ci.yml）が無ければ自動配置（既存の独自CIは .github/ci.yml.bak へ退避）
#   6. 標準CI導入「済み」を検証できたリポジトリだけにブランチ保護を適用
#      ※ CI無しで保護だけ効くと push/マージ不能に陥るため、順序を厳守する
#   7. 「保護あり・標準CI無し」の矛盾状態は、保護を一時解除して導入→再保護で自動復旧
#
# GitHub Actions（.github/workflows/sweeper.yml）から日次実行される想定。
# ローカルでも `GH_TOKEN=... bash scripts/sweep.sh` で実行可能。
#
# 必要権限（fine-grained PAT）: All repositories /
#   Contents: RW / Administration: RW / Workflows: RW / Issues: RW（ラベル用）
set -uo pipefail

OWNER="sinoda1114"
STANDARD_REPO="${OWNER}/ci-standard"
# 除外: 標準リポ自身 / チーム開発 / 空チュートリアル
EXCLUDE="ci-standard teamdev-2023-apr-team1 desktop-tutorial flue-test2"
ERRLOG=$(mktemp)
# 配布テンプレートの参照用。Actions でも手元でも同じ場所を指すよう、cwd ではなく
# スクリプトの位置から解決する。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

# ---- 運用設定の収束（全リポジトリ共通） ----

# repo-policy.yml のラベル定義（sweep.sh 単体でも動くようここに展開する。
# 変更時は repo-policy.yml と両方を更新すること）
LABELS="type:feat|0e8a16|新機能
type:fix|d73a4a|バグ修正
type:refactor|fbca04|リファクタリング（挙動不変）
type:perf|1d76db|パフォーマンス改善
type:test|c5def5|テストの追加・修正
type:docs|0075ca|ドキュメント
type:chore|ededed|雑務・依存更新・CI設定など"

sync_labels() { # sync_labels <repo> → "N件是正" / "OK" / "失敗(理由)"
  local R=$1 CHANGED=0 FAILED=0 REASON=""
  while IFS='|' read -r LN LC LD; do
    [ -z "$LN" ] && continue
    local CUR
    CUR=$(gh api "/repos/${OWNER}/${R}/labels/${LN}" 2>/dev/null || true)
    if [ -z "$CUR" ]; then
      if gh api -X POST "/repos/${OWNER}/${R}/labels" -f name="$LN" -f color="$LC" -f description="$LD" >/dev/null 2>>"$ERRLOG"; then
        CHANGED=$((CHANGED+1))
      else
        FAILED=$((FAILED+1)); REASON=$(tail -1 "$ERRLOG")
      fi
    else
      # 色/説明がポリシーと違えば是正（冪等）
      local CC CDESC
      CC=$(printf '%s' "$CUR" | jq -r '.color // empty')
      CDESC=$(printf '%s' "$CUR" | jq -r '.description // empty')
      if [ "$CC" != "$LC" ] || [ "$CDESC" != "$LD" ]; then
        if gh api -X PATCH "/repos/${OWNER}/${R}/labels/${LN}" -f new_name="$LN" -f color="$LC" -f description="$LD" >/dev/null 2>>"$ERRLOG"; then
          CHANGED=$((CHANGED+1))
        else
          FAILED=$((FAILED+1)); REASON=$(tail -1 "$ERRLOG")
        fi
      fi
    fi
  done <<< "$LABELS"
  # 失敗を黙って握り潰さない（0件是正に見えて実は権限不足、という事故を防ぐ）
  if [ "$FAILED" -gt 0 ]; then
    echo "::warning::${R} ラベル同期に失敗 ${FAILED}件: ${REASON}"
    echo "失敗${FAILED}件"
  elif [ "$CHANGED" -gt 0 ]; then
    echo "${CHANGED}件是正"
  else
    echo "OK"
  fi
}

sync_secret_scanning() { # sync_secret_scanning <repo> → "on" / "既on" / "不可"
  local R=$1 CUR
  CUR=$(gh api "/repos/${OWNER}/${R}" -q '.security_and_analysis.secret_scanning.status // "unknown"' 2>/dev/null)
  if [ "$CUR" = "enabled" ]; then echo "既on"; return; fi
  if gh api -X PATCH "/repos/${OWNER}/${R}" --input - >/dev/null 2>>"$ERRLOG" <<EOF
{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"}}}
EOF
  then echo "on"; else echo "不可"; fi
}

DEPENDABOT_BODY='# 依存更新の自動PR（sweeper が配布。編集は ci-standard 側で）
version: 2
updates:
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
'

sync_dependabot() { # sync_dependabot <repo> <branch> → "配布" / "既存" / "失敗"
  local R=$1 B=$2
  if gh api "/repos/${OWNER}/${R}/contents/.github/dependabot.yml?ref=${B}" -q .sha >/dev/null 2>&1; then
    echo "既存"; return
  fi
  if put_file "$R" "$B" ".github/dependabot.yml" "chore: Dependabot 設定を配布 [sweeper]" "$DEPENDABOT_BODY"; then
    echo "配布"
  else
    echo "失敗"
  fi
}

# クラウド↔ローカルのセッション間連絡スキル（規約は docs/AGENT-PAIRING.md）
#
# プラグインではなくリポジトリに直接置く。クラウドセッションでは marketplace が
# clone されずプラグインが一切ロードされないため、必須のものは repo-native で持つ。
#
# 配布するのはファイルまで。セッションへのタグ付けや trigger の作成はリポジトリの
# 状態ではないので収束の対象にしない（スキルが実行時に行う）。
PAIR_SKILL_PATH=".claude/skills/pair-message/SKILL.md"
PAIR_SKILL_FILE="${REPO_ROOT}/templates/pair-message-skill.md"

sync_pair_skill() { # sync_pair_skill <repo> <branch> → "配布" / "既存" / "元なし" / "失敗"
  local R=$1 B=$2 BODY
  # テンプレートが見つからないときは黙って配らない代わりに、その旨を出して気づけるようにする
  [ -f "$PAIR_SKILL_FILE" ] || { echo "元なし"; return; }
  if gh api "/repos/${OWNER}/${R}/contents/${PAIR_SKILL_PATH}?ref=${B}" -q .sha >/dev/null 2>&1; then
    echo "既存"; return
  fi
  BODY=$(cat "$PAIR_SKILL_FILE")
  if put_file "$R" "$B" "$PAIR_SKILL_PATH" "chore: セッション間連絡スキルを配布 [sweeper]" "$BODY"; then
    echo "配布"
  else
    echo "失敗"
  fi
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
    # secrets: inherit は付けない（最小権限。標準CIはシークレット不使用、値は .github/ci.env 供給）
    uses: ${STANDARD_REPO}/.github/workflows/$1-ci.yml@main
EOF
}

echo "| repo | 結果 |"
echo "|---|---|"

gh api '/user/repos?per_page=100' -q '.[] | select(.archived==false and .fork==false) | "\(.name)\t\(.default_branch)"' |
while IFS=$'\t' read -r NAME BRANCH; do
  case " $EXCLUDE " in *" $NAME "*) continue;; esac

  # ---- A. 運用設定の収束（言語を問わず全リポジトリ） ----
  LBL=$(sync_labels "$NAME")
  SS=$(sync_secret_scanning "$NAME")
  DEP=$(sync_dependabot "$NAME" "$BRANCH")
  PAIR=$(sync_pair_skill "$NAME" "$BRANCH")
  OPS="ラベル:${LBL} / scanning:${SS} / dependabot:${DEP} / pair-skill:${PAIR}"

  # ---- B. CI/CD（Node/Python のみ） ----
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
    # CI対象外の言語でも運用設定は収束済みなので結果を出す
    echo "| $NAME | $OPS / CI:対象外 |"
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
  echo "| $NAME | $OPS / CI:$STATUS |"
done
echo ""
echo "sweep 完了: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
