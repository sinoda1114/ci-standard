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
#   5. PR Bot コメント仕分け（pr-triage 呼び出しワークフロー）の配布と TYPESAFE_API_KEY の配布
#
# ファイル配布と保護ブランチ（deliver_file）:
#   既定ブランチへの直接 PUT が保護で拒否（HTTP 409）されたら、
#   - sweeper 自身が掛けた保護（必須チェック "ci / build"）なら一時解除して PUT し、末尾の protect() で再保護する
#   - それ以外（手動の保護・ルールセット）なら sweeper/<name> ブランチに置いて PR を作る（冪等。PR があれば再利用）
#   （2026-09-20 までの sweeper は dependabot.yml の配布が保護リポジトリ全てで 409 のまま放置されていた）
#
# 試験: ONLY="repo1 repo2" bash scripts/sweep.sh で対象を絞れる。
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
#   Contents: RW / Administration: RW / Workflows: RW / Issues: RW（ラベル用）/
#   Pull requests: RW（保護ブランチへの配布を PR で届けるため。無いと「PR作成不可」と報告）/
#   Secrets: RW（TYPESAFE_API_KEY 配布用。無ければ「権限なし」と報告）
set -uo pipefail

OWNER="sinoda1114"
STANDARD_REPO="${OWNER}/ci-standard"
SUPERSEDED_MARK="[sweeper-superseded]"   # sweeper 自身が置き換えで閉じた PR の印（人の却下と区別する）
# 除外: 標準リポ自身 / チーム開発 / 空チュートリアル
EXCLUDE="ci-standard teamdev-2023-apr-team1 desktop-tutorial flue-test2"
ERRLOG=$(mktemp)
REPROTECT_FAILED_FLAG=$(mktemp)   # 再保護に失敗したら中身を書く（ループはサブシェルなので変数では親に伝わらない）

# 保護を一時解除したまま中断（キャンセル・タイムアウト）しないための保険。
# deliver_file / CI 導入で unprotect したら REPROTECT_* に積み、末尾か終了シグナルで protect() を掛け直す
REPROTECT_REPO=""; REPROTECT_BRANCH=""; REPROTECT_CONTEXTS=""
reprotect_pending() {
  if [ -n "$REPROTECT_REPO" ]; then
    if protect "$REPROTECT_REPO" "$REPROTECT_BRANCH" "$REPROTECT_CONTEXTS"; then
      echo "::notice::${REPROTECT_REPO}: 保護を再適用（末尾の protect が走らなかった経路）"
    else
      echo "::error::${REPROTECT_REPO}: 保護の再適用に失敗。手で確認すること"
      echo "$REPROTECT_REPO" >>"$REPROTECT_FAILED_FLAG"     # 末尾で非ゼロ終了させる（緑のまま未保護で残さない）
    fi
    REPROTECT_REPO=""
  fi
}
on_signal() { # TERM/INT: 復旧してから終了する（終了しないと bash はループを再開し、次のリポジトリを解除しにいく）
  echo "::warning::シグナルで中断。保護を復旧して終了する"
  reprotect_pending
  exit 130     # 一時ファイルは親の末尾が読んでから消す（ここで消すと「どのリポジトリが未保護か」が失われる）
}

exists() { # exists <repo> <path> → 0/1
  gh api "/repos/${OWNER}/$1/contents/$2" -q .sha >/dev/null 2>&1
}

put_file() { # put_file <repo> <branch> <path> <message> <content> [sha] → 0 成功 / 2 保護ブランチで拒否 / 1 その他
  local R=$1 B=$2 P=$3 M=$4 C=$5 S=${6:-} ERR
  local ARGS=(-X PUT "/repos/${OWNER}/${R}/contents/${P}" -f message="$M" -f branch="$B" -f content="$(printf '%s\n' "$C" | base64 | tr -d '\n')")
  [ -n "$S" ] && ARGS+=(-f sha="$S")
  if ! ERR=$(gh api "${ARGS[@]}" 2>&1 >/dev/null); then
    # 保護による拒否は HTTP 409（クラシック保護）または 422（ruleset の GH013 系）で、本文に保護の種類が入る
    if printf '%s' "$ERR" | grep -Eq 'HTTP (409|422)' && printf '%s' "$ERR" | grep -Eqi 'status check|pull request|protected branch|repository rule|GH013'; then
      return 2   # 想定内（deliver_file が別経路へ回す）。ERRLOG には残さない
    fi
    printf '%s\n' "$ERR" >>"$ERRLOG"
    echo "::warning::put_file 失敗 ${R}/${P}: $(printf '%s' "$ERR" | tail -1)"
    return 1
  fi
}

sweeper_managed_protection() { # sweeper_managed_protection <repo> <branch> → 0 なら sweeper が掛けた保護（必須チェック "ci / build"）
  gh api "/repos/${OWNER}/$1/branches/$2/protection" -q '.required_status_checks.contexts[]?' 2>/dev/null | grep -qx 'ci / build'
}

# deliver_file <repo> <branch> <path> <message> <content> [sha] → DELIVER に結果文字列。0 成功 / 1 失敗
#   直接 PUT → 保護で拒否なら（sweeper 管理の保護）一時解除して PUT / （それ以外）PR を作る
deliver_file() {
  local R=$1 B=$2 P=$3 M=$4 C=$5 S=${6:-} rc
  put_file "$R" "$B" "$P" "$M" "$C" "$S"; rc=$?
  if [ $rc -eq 0 ]; then DELIVER="配布"; return 0; fi
  if [ $rc -ne 2 ]; then DELIVER="失敗"; return 1; fi
  # sweeper 自身が掛けた保護で、かつ標準CIが導入済み（= ループ末尾の protect() が必ず走る）ときだけ一時解除する。
  # それ以外で解除すると再保護の保証がないので PR 経路に回す
  if [ -n "${KIND:-}" ] && [ "${CI_INSTALLED_NOW:-false}" = true ] && sweeper_managed_protection "$R" "$B"; then
    if unprotect "$R" "$B"; then
      UNPROTECTED_FOR_FIX=true
      REPROTECT_REPO="$R"; REPROTECT_BRANCH="$B"; REPROTECT_CONTEXTS="$CONTEXTS"
      put_file "$R" "$B" "$P" "$M" "$C" "$S"; rc=$?
      if [ $rc -eq 0 ]; then DELIVER="配布(保護を一時解除)"; return 0; fi
      # 2 回目も保護で拒否 = ruleset 等の別の保護が併用されている → PR 経路へ
      [ $rc -ne 2 ] && { DELIVER="失敗"; return 1; }
    fi
  fi
  open_pr_with_file "$R" "$B" "$P" "$M" "$C"
}

open_pr_with_file() { # open_pr_with_file <repo> <base> <path> <message> <content> → DELIVER="PR作成" / "PR済み" / "失敗"
  local R=$1 B=$2 P=$3 M=$4 C=$5 SLUG HEAD BJ BS BC OPEN REJECTED
  # ブランチ名に内容ハッシュを含める: 人が閉じた PR は「その内容」の却下として翌日作り直さないが、
  # 雛形を直して内容が変われば別ブランチで新しい PR が出る
  SLUG="sweeper/$(basename "$P" | sed 's/\.[^.]*$//')-$(printf '%s\n' "$C" | shasum | cut -c1-8)"
  # 先に PR の状態を見る（ブランチを作ってから却下に気づくと、毎日「作成→削除」を往復し配布先の on: push CI を起動してしまう）。
  # 取得に失敗した日は配布を見送る（0 件と区別しないと、却下済み PR を作り直してしまう）
  OPEN=$(gh pr list -R "${OWNER}/${R}" --head "$SLUG" --base "$B" --state open --json number -q 'length' 2>>"$ERRLOG") || { DELIVER="PR状態の取得に失敗"; return 1; }
  if [ "${OPEN:-0}" -gt 0 ]; then DELIVER="PR済み"; return 0; fi
  # 却下 = 人がマージせずに閉じた PR。sweeper 自身が置き換えで閉じたもの（本文に SUPERSEDED_MARK）は数えない
  REJECTED=$(gh pr list -R "${OWNER}/${R}" --head "$SLUG" --base "$B" --state closed --json mergedAt,body \
               -q "[.[]|select(.mergedAt==null)|select((.body // \"\")|contains(\"${SUPERSEDED_MARK}\")|not)]|length" 2>>"$ERRLOG") || { DELIVER="PR状態の取得に失敗"; return 1; }
  if [ "${REJECTED:-0}" -gt 0 ]; then DELIVER="PR却下済み"; return 0; fi
  if ! gh api "/repos/${OWNER}/${R}/git/ref/heads/${SLUG}" >/dev/null 2>&1; then
    HEAD=$(gh api "/repos/${OWNER}/${R}/git/ref/heads/${B}" -q .object.sha 2>>"$ERRLOG") || { DELIVER="失敗"; return 1; }
    gh api -X POST "/repos/${OWNER}/${R}/git/refs" -f ref="refs/heads/${SLUG}" -f sha="$HEAD" >/dev/null 2>>"$ERRLOG" || { DELIVER="失敗"; return 1; }
  fi
  BJ=$(gh api "/repos/${OWNER}/${R}/contents/${P}?ref=${SLUG}" 2>/dev/null || true)
  BS=$(printf '%s' "$BJ" | jq -r '.sha // empty' 2>/dev/null)
  BC=$(printf '%s' "$BJ" | jq -r '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || true)
  if [ "$BC" != "$C" ]; then
    put_file "$R" "$SLUG" "$P" "$M" "$C" "$BS" || { DELIVER="失敗"; return 1; }
  fi
  local PRERR PREFIX OLD
  if PRERR=$(gh pr create -R "${OWNER}/${R}" --head "$SLUG" --base "$B" --title "$M" \
       --body "sweeper（${STANDARD_REPO}）が配布する標準設定です。既定ブランチが保護されているため PR で届けます。CI が緑なら squash マージしてください。" \
       2>&1 >/dev/null); then
    # 新 PR ができてから、同じファイルの古い内容の sweeper PR（別ハッシュ）を置き換えとして閉じる
    # （先に閉じると、作成が一時失敗した日に配布 PR が 1 本も無くなる）。本文に印を付けてから閉じ、却下判定から除外する
    PREFIX="sweeper/$(basename "$P" | sed 's/\.[^.]*$//')-"
    gh pr list -R "${OWNER}/${R}" --base "$B" --state open --json number,headRefName \
      -q ".[] | select(.headRefName | startswith(\"${PREFIX}\")) | select(.headRefName != \"${SLUG}\") | .number" 2>/dev/null |
    while IFS= read -r OLD; do
      gh pr edit "$OLD" -R "${OWNER}/${R}" --body "${SUPERSEDED_MARK} 内容を更新した新しい PR に置き換えました（sweeper）。" >/dev/null 2>>"$ERRLOG" || true
      gh pr close "$OLD" -R "${OWNER}/${R}" --delete-branch --comment "内容を更新した新しい PR に置き換えます（sweeper）。" >/dev/null 2>>"$ERRLOG" || true
    done
    DELIVER="PR作成"; return 0
  fi
  printf '%s\n' "$PRERR" >>"$ERRLOG"
  if printf '%s' "$PRERR" | grep -Eqi 'HTTP 403|Resource not accessible|not permitted'; then
    DELIVER="PR作成不可(PAT に Pull requests: RW が必要)"; return 1   # ブランチは残す（掃除対象外）。権限を足せば翌日 PR が出る
  fi
  DELIVER="失敗"; return 1
}

cleanup_sweeper_branches() { # cleanup_sweeper_branches <repo>: sweeper が作った名前（sweeper/<name>-<hash8>）で、
  # PR が閉じられた/マージされたブランチだけ消す（雛形更新のたびに増えるため）。PR が無いブランチ（作成失敗直後）や人のブランチは触らない
  local R=$1 BR OPEN CLOSED
  gh api --paginate "/repos/${OWNER}/${R}/branches?per_page=100" -q '.[].name | select(test("^sweeper/(dependabot|pr-triage)-[0-9a-f]{8}$"))' 2>/dev/null |
  while IFS= read -r BR; do
    OPEN=$(gh pr list -R "${OWNER}/${R}" --head "$BR" --state open --json number -q length 2>/dev/null || echo 1)
    [ "${OPEN:-1}" = 0 ] || continue
    CLOSED=$(gh pr list -R "${OWNER}/${R}" --head "$BR" --state closed --json number -q length 2>/dev/null || echo 0)
    [ "${CLOSED:-0}" -gt 0 ] && gh api -X DELETE "/repos/${OWNER}/${R}/git/refs/heads/${BR}" >/dev/null 2>&1
  done
  return 0
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

sync_labels() { # sync_labels <repo> → "N件是正" / "OK" / "失敗N件"
  local R=$1 CHANGED=0 FAILED=0 REASON="" LN LC LD CUR CC CDESC ERR
  while IFS='|' read -r LN LC LD; do
    [ -z "$LN" ] && continue
    # 存在確認。gh api は失敗時もエラーJSONを stdout に出すため、空判定ではなく
    # 終了コードで判定する（空判定だと 404 の本文が入って「存在する」と誤判定し、
    # 作成がスキップされていた。実測で確認）
    if CUR=$(gh api "/repos/${OWNER}/${R}/labels/${LN}" 2>/dev/null); then :; else CUR=""; fi
    if [ -z "$CUR" ]; then
      # 作成。エラーはその場で捕捉する（ERRLOG の末尾を後から読むと、
      # 直前の存在確認の 404 を誤って理由に拾ってしまうため）
      if ERR=$(gh api -X POST "/repos/${OWNER}/${R}/labels" \
                 -f name="$LN" -f color="$LC" -f description="$LD" 2>&1 >/dev/null); then
        CHANGED=$((CHANGED+1))
      else
        FAILED=$((FAILED+1)); REASON="$ERR"
      fi
    else
      # 色/説明がポリシーと違えば是正（冪等）
      CC=$(printf '%s' "$CUR" | jq -r '.color // empty')
      CDESC=$(printf '%s' "$CUR" | jq -r '.description // empty')
      if [ "$CC" != "$LC" ] || [ "$CDESC" != "$LD" ]; then
        if ERR=$(gh api -X PATCH "/repos/${OWNER}/${R}/labels/${LN}" \
                   -f new_name="$LN" -f color="$LC" -f description="$LD" 2>&1 >/dev/null); then
          CHANGED=$((CHANGED+1))
        else
          FAILED=$((FAILED+1)); REASON="$ERR"
        fi
      fi
    fi
  done <<< "$LABELS"
  # 失敗を黙って握り潰さない（0件是正に見えて実は権限不足、という事故を防ぐ）
  if [ "$FAILED" -gt 0 ]; then
    echo "::warning::${R} ラベル同期に失敗 ${FAILED}件: ${REASON}" >&2
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
      interval: "weekly"'

sync_dependabot() { # sync_dependabot <repo> <branch> → DEP="既存" / "配布" / "PR作成" / "PR済み" / "失敗"
  local R=$1 B=$2
  if gh api "/repos/${OWNER}/${R}/contents/.github/dependabot.yml?ref=${B}" -q .sha >/dev/null 2>&1; then
    DEP="既存"; return
  fi
  deliver_file "$R" "$B" ".github/dependabot.yml" "chore: Dependabot 設定を配布 [sweeper]" "$DEPENDABOT_BODY"
  DEP="$DELIVER"
}

PR_TRIAGE_BODY="$(cat "$(dirname "$0")/../templates/pr-triage-caller.yml" 2>/dev/null || true)"

sync_pr_triage() { # sync_pr_triage <repo> <branch> → PRT="既存" / "配布" / "更新" / "PR作成" / "PR済み" / "独自(未変更)" / "失敗" / "雛形なし"
  local R=$1 B=$2 J SHA BODY MSG
  PRT_PRESENT=false   # 既定ブランチに標準の呼び出しが既にあるか（secret の同期可否に使う。配布結果とは別）
  [ -n "$PR_TRIAGE_BODY" ] || { PRT="雛形なし"; return; }
  J=$(gh api "/repos/${OWNER}/${R}/contents/.github/workflows/pr-triage.yml?ref=${B}" 2>/dev/null || true)
  SHA=$(printf '%s' "$J" | jq -r '.sha // empty' 2>/dev/null)
  BODY=$(printf '%s' "$J" | jq -r '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || true)
  printf '%s' "$BODY" | grep -q "$STANDARD_REPO" && PRT_PRESENT=true
  # 雛形と完全一致なら何もしない。雛形（trigger / 権限 / Bot 判定）を直したら翌日全リポジトリに届く
  if [ "$BODY" = "$PR_TRIAGE_BODY" ]; then PRT="既存"; return; fi
  # 標準を参照しない独自ファイルは上書きしない
  if [ -n "$BODY" ] && ! printf '%s' "$BODY" | grep -q "$STANDARD_REPO"; then PRT="独自(未変更)"; return; fi
  if [ -n "$SHA" ]; then MSG="ci: PR Bot コメント仕分け（${STANDARD_REPO}/pr-triage）の呼び出しを更新 [sweeper]"
  else MSG="ci: PR Bot コメント仕分け（${STANDARD_REPO}/pr-triage）を配布 [sweeper]"; fi
  deliver_file "$R" "$B" ".github/workflows/pr-triage.yml" "$MSG" "$PR_TRIAGE_BODY" "$SHA"
  PRT="$DELIVER"
  [ -n "$SHA" ] && [ "$PRT" = "配布" ] && PRT="更新"
  return 0
}

sync_pr_triage_secret() { # sync_pr_triage_secret <repo> → PRS="同期" / "キー未設定" / "権限なし"
  # 名前の有無では飛ばさない（GitHub は値を返さないので、中央の鍵をローテーションしても古い値が残り続ける）。毎回 set する（冪等）
  local R=$1
  [ -n "${TYPESAFE_API_KEY:-}" ] || { PRS="キー未設定"; return; }
  if printf '%s' "$TYPESAFE_API_KEY" | gh secret set TYPESAFE_API_KEY -R "${OWNER}/${R}" 2>>"$ERRLOG"; then
    PRS="同期"
  else
    PRS="権限なし"
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
    # secrets: inherit は付けない（最小権限。必要なリポだけ呼び出し側で secrets 明示マップ）
    uses: ${STANDARD_REPO}/.github/workflows/$1-ci.yml@main
EOF
}

echo "| repo | 結果 |"
echo "|---|---|"

# affiliation=owner と owner.login の二重で自分のリポジトリに絞る（既定は collaborator / organization_member も含み、
# 他オーナーのリポジトリを sinoda1114/<name> として叩いて 404 を量産する）
gh api --paginate "/user/repos?per_page=100&affiliation=owner" \
  -q ".[] | select(.archived==false and .fork==false and .owner.login==\"$OWNER\") | \"\\(.name)\\t\\(.default_branch)\"" |
while IFS=$'\t' read -r NAME BRANCH; do
  case " $EXCLUDE " in *" $NAME "*) continue;; esac
  if [ -n "${ONLY:-}" ]; then case " $ONLY " in *" $NAME "*) ;; *) continue;; esac; fi

  # 言語判定を先に行う（Contents API のみ、clone不要）。deliver_file が「sweeper 管理の保護か」の判断に KIND を使う
  KIND=""; CONTEXTS=""; UNPROTECTED_FOR_FIX=false
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
  fi

  # 標準CIが既に入っているか（deliver_file が「保護を一時解除してよいか」の判断に使う。末尾の protect() が走る保証になる）
  CI_INSTALLED_NOW=false
  if [ -n "$KIND" ] && gh api "/repos/${OWNER}/${NAME}/contents/.github/workflows/ci.yml?ref=${BRANCH}" -q .content 2>/dev/null | base64 -d 2>/dev/null | grep -q "$STANDARD_REPO"; then
    CI_INSTALLED_NOW=true
  fi
  reprotect_pending          # 前のリポジトリで protect が失敗・スキップされていたらここで戻す（値を捨てない）
  trap reprotect_pending EXIT
  trap on_signal INT TERM

  # ---- A. 運用設定の収束（言語を問わず全リポジトリ） ----
  LBL=$(sync_labels "$NAME")
  SS=$(sync_secret_scanning "$NAME")
  sync_dependabot "$NAME" "$BRANCH"        # → DEP（サブシェルにしない: UNPROTECTED_FOR_FIX を親へ伝えるため）
  sync_pr_triage "$NAME" "$BRANCH"         # → PRT
  # 鍵は「標準の呼び出しが既定ブランチにある」か「今回届いた/届く見込み」のリポジトリだけに同期する
  # （古い標準 caller が残っていて更新 PR が却下された場合もローテーションは追従させる）
  if $PRT_PRESENT; then sync_pr_triage_secret "$NAME"; else
    case "$PRT" in
      配布|"配布(保護を一時解除)"|更新|PR作成|PR済み) sync_pr_triage_secret "$NAME";;   # → PRS
      *) PRS="対象外";;   # 独自 / 失敗 / 雛形なし / 却下済み / 作成不可 / 取得失敗: 標準の呼び出しが無いリポジトリに鍵だけ置かない
    esac
  fi
  cleanup_sweeper_branches "$NAME"         # 閉じた/マージ済み sweeper PR のブランチを掃除（却下の記録は閉じた PR 自体に残る）
  OPS="ラベル:${LBL} / scanning:${SS} / dependabot:${DEP} / pr-triage:${PRT}(secret:${PRS})"

  # ---- B. CI/CD（Node/Python のみ） ----
  if [ -z "$KIND" ]; then
    # CI対象外の言語でも運用設定は収束済みなので結果を出す
    echo "| $NAME | $OPS / CI:対象外 |"
    continue
  fi

  # 404（未導入）と API 障害を区別する。障害を「CI なし」と扱うと、導入済みリポジトリの保護を外して sha 無し PUT に失敗し、未保護で終わる
  CI_ERR=""
  if ! CI_JSON=$(gh api "/repos/${OWNER}/${NAME}/contents/.github/workflows/ci.yml?ref=${BRANCH}" 2>&1); then
    CI_ERR="$CI_JSON"; CI_JSON=""
    if ! printf '%s' "$CI_ERR" | grep -q 'HTTP 404'; then
      echo "| $NAME | $OPS / CI:状態取得に失敗のためスキップ（$(printf '%s' "$CI_ERR" | tail -1 | cut -c1-80)） |"
      continue
    fi
  fi
  CI_SHA=$(printf '%s' "$CI_JSON" | jq -r '.sha // empty' 2>/dev/null)
  CI_BODY=$(printf '%s' "$CI_JSON" | jq -r '.content // empty' 2>/dev/null | base64 -d 2>/dev/null || true)

  INSTALLED=false
  if printf '%s' "$CI_BODY" | grep -q "$STANDARD_REPO"; then
    INSTALLED=true
    STATUS="導入済み"
  else
    # 「保護あり・標準CI無し」の矛盾状態なら保護を一時解除して復旧する（deliver_file が既に解除済みならそのまま）
    if ! $UNPROTECTED_FOR_FIX && is_protected "$NAME" "$BRANCH"; then
      if unprotect "$NAME" "$BRANCH"; then
        UNPROTECTED_FOR_FIX=true
        REPROTECT_REPO="$NAME"; REPROTECT_BRANCH="$BRANCH"; REPROTECT_CONTEXTS="$CONTEXTS"
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
      # CI 無しで保護すると push/マージ不能に詰むため、保険（reprotect_pending）の対象からも外す
      REPROTECT_REPO=""
      $UNPROTECTED_FOR_FIX && STATUS="$STATUS ※保護は解除したまま（CI無しで保護すると詰むため）"
    fi
  fi

  # 保護は「標準CIが存在する」ことを検証できた場合のみ適用する
  if $INSTALLED; then
    if protect "$NAME" "$BRANCH" "$CONTEXTS"; then
      STATUS="$STATUS / 保護OK"; REPROTECT_REPO=""
    else
      STATUS="$STATUS / 保護不可(private+Free?)"
    fi
  fi
  echo "| $NAME | $OPS / CI:$STATUS |"
done
# ループはパイプのサブシェル。一覧取得の失敗とシグナル中断（130）を親の終了コードに反映する。
# 2 つの代入は必ず 1 コマンドで行う（1 つ目の代入自体が PIPESTATUS を上書きし、2 つ目が set -u で落ちる）
LIST_RC=${PIPESTATUS[0]} LOOP_RC=${PIPESTATUS[1]}
echo ""
echo "sweep 完了: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
# 失敗の詳細（「ログ参照」の参照先）。Actions ならジョブサマリーにも出す
if [ -s "$ERRLOG" ]; then
  echo ""; echo "<details><summary>エラー詳細（$(wc -l < "$ERRLOG" | tr -d ' ') 行）</summary>"; echo ""; echo '```'; cat "$ERRLOG"; echo '```'; echo "</details>"
fi
rm -f "$ERRLOG"
if [ -s "$REPROTECT_FAILED_FLAG" ]; then
  echo "::error::保護を戻せなかったリポジトリ: $(tr '\n' ' ' <"$REPROTECT_FAILED_FLAG")。手で確認すること"; rm -f "$REPROTECT_FAILED_FLAG"; exit 1
fi
rm -f "$REPROTECT_FAILED_FLAG"
[ "${LIST_RC:-0}" = 0 ] || { echo "::error::リポジトリ一覧の取得に失敗（収束は走っていない）"; exit 1; }
[ "${LOOP_RC:-0}" = 130 ] && { echo "::error::シグナルで中断された（保護は復旧済み）"; exit 130; }
exit 0
