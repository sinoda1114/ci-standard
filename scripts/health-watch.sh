#!/usr/bin/env bash
# CI 健全性の見回り: 全リポジトリの既定ブランチの最新 CI 結論と未解決の脆弱性アラートを集め、
# Markdown のレポートを標準出力へ書く。問題が 1 件も無ければ 1 行目に "OK" だけを出す。
#
# 判定する「問題」:
#   1. 既定ブランチの最新の完了済み ci.yml run が success / skipped / neutral 以外
#      （failure だけでなく startup_failure・timed_out・cancelled も赤。2026-09-21 の障害は startup_failure だった）
#   2. critical / high の未解決 Dependabot アラートがある
#   3. 上の 2 つを確認できなかった（権限不足・レート制限・障害）。見えていないことを「問題なし」と言わない
#
# 出力は public リポジトリ ci-standard の Issue に載る。private リポジトリは名前と件数だけにし、
# パッケージ名とリンクは出さない（どの private リポジトリに何の脆弱性があるかを公開しないため）。
#
# 必要な PAT 権限: Actions: read（run の取得）、Dependabot alerts: read（アラートの取得）。
#
# sweeper と違い、このスクリプトは**何も変更しない**（読むだけ）。
# 使い方: GH_TOKEN=<PAT> bash scripts/health-watch.sh
set -uo pipefail

OWNER="${OWNER:-sinoda1114}"
# sweep.sh と同じ除外。sweeper が直さないリポジトリを載せると Issue が永久に閉じない
EXCLUDE="${EXCLUDE:-ci-standard teamdev-2023-apr-team1 desktop-tutorial flue-test2}"
ONLY="${ONLY:-}"   # 動作確認用: スペース区切りでリポジトリ名を指定すると、それだけを見る
TODAY=$(TZ=Asia/Tokyo date +%Y-%m-%d)
ERR=$(mktemp); trap 'rm -f "$ERR"' EXIT

# アーカイブ済み・フォーク・他人のリポジトリは除く。既定ブランチ名と private かどうかも取る
LIST=$(gh api --paginate "/user/repos?per_page=100&affiliation=owner" \
  -q ".[] | select(.archived == false) | select(.fork == false) | select(.owner.login == \"${OWNER}\")
       | \"\(.name)\t\(.default_branch)\t\(.private)\"" 2>/dev/null) || {
  echo "リポジトリ一覧の取得に失敗しました" >&2; exit 1; }

red=""; vuln=""; unknown=""; n_red=0; n_vuln=0; n_unknown=0; n_total=0

# 直前の gh api の失敗を分類する。404 は「対象が無い」、それ以外は「確認できなかった」
http_code() { grep -oE 'HTTP [0-9]+' "$ERR" | tail -1; }
note_unknown() { # note_unknown <表示名> <何を>
  unknown="${unknown}| ${1} | ${2} | $(http_code) |"$'\n'; n_unknown=$((n_unknown + 1)); }

while IFS=$'\t' read -r NAME BRANCH PRIVATE; do
  [ -n "$NAME" ] || continue
  case " $EXCLUDE " in *" $NAME "*) continue;; esac
  if [ -n "$ONLY" ]; then case " $ONLY " in *" $NAME "*) ;; *) continue;; esac; fi
  n_total=$((n_total + 1))
  if [ "$PRIVATE" = true ]; then LABEL="${NAME} (private)"; else LABEL="[${NAME}](https://github.com/${OWNER}/${NAME})"; fi

  # 1. 標準CI（ci.yml）の最新の完了済み run。実行中の run は結論が無いので見ない。
  #    ci.yml が PR でしか走らないリポジトリでは main 上の最新 run が何週間も前のまま残るので、
  #    既定ブランチの先頭コミットを検証した run だけを見る（古い失敗で Issue が閉じなくなるのを防ぐ）
  if RUN=$(gh api "/repos/${OWNER}/${NAME}/actions/workflows/ci.yml/runs?branch=${BRANCH}&status=completed&per_page=1" \
        -q '.workflow_runs[0] // empty | "\(.conclusion)\t\(.created_at[:10])\t\(.html_url)\t\(.head_sha)"' 2>"$ERR"); then
    IFS=$'\t' read -r CONC WHEN URL SHA <<<"$RUN"
    if [ -n "$RUN" ] && ! HEAD=$(gh api "/repos/${OWNER}/${NAME}/branches/${BRANCH}" -q .commit.sha 2>"$ERR"); then
      note_unknown "$LABEL" "既定ブランチの先頭"
    elif [ -n "$RUN" ] && [ "$SHA" = "$HEAD" ]; then
      case "$CONC" in
        success|skipped|neutral) ;;
        *)
          if [ "$PRIVATE" = true ]; then LINK="-"; else LINK="[run](${URL})"; fi
          red="${red}| ${LABEL} | ${CONC} | ${WHEN} | ${LINK} |"$'\n'
          n_red=$((n_red + 1));;
      esac
    fi
  elif ! grep -q 'HTTP 404' "$ERR"; then   # 404 は ci.yml が無い（標準CI 未導入）
    note_unknown "$LABEL" "CI の結論"
  fi

  # 2. critical / high の未解決アラート。1 回の取得で件数とパッケージ名を作る
  if AL=$(gh api "/repos/${OWNER}/${NAME}/dependabot/alerts?state=open&severity=critical,high&per_page=100" \
       -q '[.[] | .dependency.package.name] | "\(length)\t\(group_by(.) | map("\(.[0])×\(length)") | join(", "))"' 2>"$ERR"); then
    IFS=$'\t' read -r CNT PKGS <<<"$AL"
    if [ "${CNT:-0}" -gt 0 ]; then
      if [ "$PRIVATE" = true ]; then VLABEL="$LABEL"; PKGS="（private のため非公開）"
      else VLABEL="[${NAME}](https://github.com/${OWNER}/${NAME}/security/dependabot)"; fi
      vuln="${vuln}| ${VLABEL} | ${CNT} | ${PKGS} |"$'\n'
      n_vuln=$((n_vuln + 1))
    fi
  elif ! grep -qiE 'HTTP 404|alerts are disabled' "$ERR"; then   # Dependabot 無効は対象外
    note_unknown "$LABEL" "脆弱性アラート"
  fi
done <<EOF
$LIST
EOF

if [ "$n_red" = 0 ] && [ "$n_vuln" = 0 ] && [ "$n_unknown" = 0 ]; then
  echo "OK"
  echo
  echo "${n_total} リポジトリを確認し、赤い CI と critical/high の脆弱性はありませんでした（${TODAY}）。"
  exit 0
fi

echo "毎朝の見回りで問題を検出しました（${TODAY} 時点・${n_total} リポジトリを確認）。"
echo
echo "**CI は push が無いと走りません。** 休眠リポジトリでは依存だけが古くなり、"
echo "たまに配布 push で CI が走って赤くなっても気付かれないまま残ります。"
echo "このレポートはその放置を拾うためのものです。"
echo

if [ "$n_red" -gt 0 ]; then
  echo "## 既定ブランチの CI が赤のまま（${n_red} 件）"
  echo
  echo "| リポジトリ | 結論 | 最終実行 | run |"
  echo "|---|---|---|---|"
  printf '%s' "$red"
  echo
fi

if [ "$n_vuln" -gt 0 ]; then
  echo "## critical / high の未解決アラート（${n_vuln} 件）"
  echo
  echo "| リポジトリ | 件数 | パッケージ |"
  echo "|---|---|---|"
  printf '%s' "$vuln"
  echo
fi

if [ "$n_unknown" -gt 0 ]; then
  echo "## 確認できなかった（${n_unknown} 件）"
  echo
  echo "見えていないものを「問題なし」とは扱いません。多くは PAT の権限不足です"
  echo "（Actions: read / Dependabot alerts: read）。"
  echo
  echo "| リポジトリ | 対象 | 応答 |"
  echo "|---|---|---|"
  printf '%s' "$unknown"
  echo
fi

echo "---"
echo "<sub>ci-standard/health · 毎朝 07:30 JST に自動更新。全部緑になれば自動でクローズされます。</sub>"
