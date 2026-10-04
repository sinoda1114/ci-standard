#!/usr/bin/env bash
# CI 健全性の見回り: 全リポジトリの既定ブランチの最新 CI 結論と未解決の脆弱性アラートを集め、
# Markdown のレポートを標準出力へ書く。問題が 1 件も無ければ 1 行目に "OK" だけを出す。
#
# 判定する「問題」:
#   1. 既定ブランチの最新の完了済み ci.yml run が success / skipped / neutral 以外
#      （failure だけでなく startup_failure・timed_out・cancelled も赤。2026-09-21 の障害は startup_failure だった）
#   2. critical / high の未解決 Dependabot アラートがある
#   3. 例外リスト（.github/audit-allowlist）に、期限が 14 日以内（または期限切れ）の行がある
#      （期限の日に突然 CI が落ちるのを防ぐ。修正版が出たか確かめて行を消すか、理由を確かめて期限を延ばす）
#   4. 上の 3 つを確認できなかった（権限不足・レート制限・障害）。見えていないことを「問題なし」と言わない
#
# 出力は public リポジトリ ci-standard の Issue に載る。private リポジトリは名前と件数だけにし、
# パッケージ名とリンクは出さない（どの private リポジトリに何の脆弱性があるかを公開しないため）。
#
# 必要な PAT 権限: Actions: read（run の取得）、Dependabot alerts: read（アラートの取得）、Contents: read（例外リストの取得）。
#
# sweeper と違い、このスクリプトは**何も変更しない**（読むだけ）。
# 使い方: GH_TOKEN=<PAT> bash scripts/health-watch.sh
set -uo pipefail

OWNER="${OWNER:-sinoda1114}"
# sweep.sh と同じ除外。sweeper が直さないリポジトリを載せると Issue が永久に閉じない
EXCLUDE="${EXCLUDE:-ci-standard teamdev-2023-apr-team1 desktop-tutorial flue-test2}"
ONLY="${ONLY:-}"   # 動作確認用: スペース区切りでリポジトリ名を指定すると、それだけを見る
TODAY=$(TZ=Asia/Tokyo date +%Y-%m-%d)
TODAY_UTC=$(date -u +%Y-%m-%d)   # 例外リストの期限は CI（audit-gate.py）と同じ UTC の日付で数える
ERR=$(mktemp); trap 'rm -f "$ERR"' EXIT

# アーカイブ済み・フォーク・他人のリポジトリは除く。既定ブランチ名と private かどうかも取る
LIST=$(gh api --paginate "/user/repos?per_page=100&affiliation=owner" \
  -q ".[] | select(.archived == false) | select(.fork == false) | select(.owner.login == \"${OWNER}\")
       | \"\(.name)\t\(.default_branch)\t\(.private)\"" 2>/dev/null) || {
  echo "リポジトリ一覧の取得に失敗しました" >&2; exit 1; }

red=""; vuln=""; soon=""; unknown=""; n_red=0; n_vuln=0; n_soon=0; n_unknown=0; n_total=0
SOON_DAYS="${SOON_DAYS:-14}"

# 例外リストの本文から、期限が SOON_DAYS 日以内の行を "GHSA<TAB>期限<TAB>残り日数" で出す。
# 書式（GHSA-ID 期限 理由。BOM 付き可）と日付の基準（UTC）は audit-gate.py に合わせる。
# 読めない（UTF-8 でない等）ときは非 0 で終わる。呼び出し側で「確認できなかった」にする
soon_entries() { python3 -I -c '
import datetime, re, sys
today, days = datetime.date.fromisoformat(sys.argv[1]), int(sys.argv[2])
text = sys.stdin.buffer.read().decode("utf-8-sig")   # 不正な文字は例外にする（環境の既定に任せると黙って置き換わる）
# 行の分け方と照合は audit-gate.py と同じにする（splitlines は \f や \u2028 でも分けてしまい、CI とずれる）
for line in re.split(r"\r\n|\r|\n", text):
    m = re.match(r"^(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\s+(\d{4}-\d{2}-\d{2})\s+(\S.*)$", line.strip())
    if not m:
        continue
    try:
        left = (datetime.date.fromisoformat(m.group(2)) - today).days
    except ValueError:
        continue
    if left <= days:
        print(f"{m.group(1)}\t{m.group(2)}\t{left}")
' "$TODAY_UTC" "$SOON_DAYS"; }

# 直前の gh api の失敗を分類する。404 は「対象が無い」、それ以外は「確認できなかった」
http_code() { grep -oE 'HTTP [0-9]+' "$ERR" | tail -1; }
note_unknown() { # note_unknown <表示名> <何を>
  unknown="${unknown}| ${1} | ${2} | $(http_code) |"$'\n'; n_unknown=$((n_unknown + 1)); }

while IFS=$'\t' read -r NAME BRANCH PRIVATE; do
  [ -n "$NAME" ] || continue
  case " $EXCLUDE " in *" $NAME "*) continue;; esac
  if [ -n "$ONLY" ]; then case " $ONLY " in *" $NAME "*) ;; *) continue;; esac; fi
  n_total=$((n_total + 1))
  # ブランチ名に & や # が入っても問い合わせが変わらないよう、URL に入れる前にエンコードする
  QBRANCH=$(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$BRANCH")
  if [ "$PRIVATE" = true ]; then LABEL="${NAME} (private)"; else LABEL="[${NAME}](https://github.com/${OWNER}/${NAME})"; fi

  # 1. 標準CI（ci.yml）の最新の完了済み run。実行中の run は結論が無いので見ない。
  #    ci.yml が PR でしか走らないリポジトリでは main 上の最新 run が何週間も前のまま残るので、
  #    既定ブランチの先頭コミットを検証した run だけを見る（古い失敗で Issue が閉じなくなるのを防ぐ）
  if RUN=$(gh api "/repos/${OWNER}/${NAME}/actions/workflows/ci.yml/runs?branch=${QBRANCH}&status=completed&per_page=1" \
        -q '.workflow_runs[0] // empty | "\(.conclusion)\t\(.created_at[:10])\t\(.html_url)\t\(.head_sha)"' 2>"$ERR"); then
    IFS=$'\t' read -r CONC WHEN URL SHA <<<"$RUN"
    if [ -n "$RUN" ] && ! HEAD=$(gh api "/repos/${OWNER}/${NAME}/branches/${QBRANCH}" -q .commit.sha 2>"$ERR"); then
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

  # 2. critical / high の未解決アラート。100 件を超えても数えるよう全ページを取り、
  #    パッケージ名を 1 行 1 件で受けて件数とパッケージ別の内訳を作る（-q はページごとに効くので集計はここで行う）
  if AL=$(gh api --paginate "/repos/${OWNER}/${NAME}/dependabot/alerts?state=open&severity=critical,high&per_page=100" \
       -q '.[] | .dependency.package.name' 2>"$ERR"); then
    CNT=$(printf '%s' "$AL" | grep -c .)
    PKGS=$(printf '%s\n' "$AL" | grep . | sort | uniq -c | awk '{printf "%s%s×%s", (NR>1 ? ", " : ""), $2, $1}')
    if [ "$CNT" -gt 0 ]; then
      if [ "$PRIVATE" = true ]; then VLABEL="$LABEL"; PKGS="（private のため非公開）"
      else VLABEL="[${NAME}](https://github.com/${OWNER}/${NAME}/security/dependabot)"; fi
      vuln="${vuln}| ${VLABEL} | ${CNT} | ${PKGS} |"$'\n'
      n_vuln=$((n_vuln + 1))
    fi
  elif ! grep -qiE 'HTTP 404|alerts are disabled' "$ERR"; then   # Dependabot 無効は対象外
    note_unknown "$LABEL" "脆弱性アラート"
  fi

  # 3. 例外リストの期限。ファイルが無い（404）リポジトリは対象外
  if ALW=$(gh api -H "Accept: application/vnd.github.raw" \
        "/repos/${OWNER}/${NAME}/contents/.github/audit-allowlist?ref=${QBRANCH}" 2>"$ERR"); then
    if ! SOONS=$(printf '%s\n' "$ALW" | soon_entries 2>/dev/null); then
      note_unknown "$LABEL" "例外リスト（解析できない）"; CNT=0
    else
      CNT=$(printf '%s' "$SOONS" | grep -c .)
    fi
    if [ "$CNT" -gt 0 ]; then
      if [ "$PRIVATE" = true ]; then DETAIL="（private のため非公開）"
      else DETAIL=$(printf '%s\n' "$SOONS" | awk -F'\t' '{printf "%s%s（期限 %s、%s）", (NR>1 ? "<br>" : ""), $1, $2, ($3 < 0 ? "期限切れ" : "あと " $3 " 日")}'); fi
      soon="${soon}| ${LABEL} | ${CNT} | ${DETAIL} |"$'\n'
      n_soon=$((n_soon + 1))
    fi
  elif ! grep -q 'HTTP 404' "$ERR"; then
    note_unknown "$LABEL" "例外リスト"
  fi
done <<EOF
$LIST
EOF

# 1 件も見ていないのに OK を出すと、health.yml が既存 Issue を閉じてしまう。
# 一覧の取得は成功しても、PAT の対象リポジトリが絞られると 0 件になる
if [ "$n_total" = 0 ]; then
  echo "対象リポジトリが 0 件でした。PAT の対象リポジトリと権限を確認してください" >&2
  exit 1
fi

if [ "$n_red" = 0 ] && [ "$n_vuln" = 0 ] && [ "$n_soon" = 0 ] && [ "$n_unknown" = 0 ]; then
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

if [ "$n_soon" -gt 0 ]; then
  echo "## 例外リストの期限が近い・切れている（${n_soon} 件）"
  echo
  echo "期限の日を過ぎると CI が落ちます。修正版が出ていれば行を消し、出ていなければ理由を確かめて期限を延ばしてください"
  echo "（期限は今日から 120 日以内まで）。"
  echo
  echo "| リポジトリ | 件数 | 例外 |"
  echo "|---|---|---|"
  printf '%s' "$soon"
  echo
fi

if [ "$n_unknown" -gt 0 ]; then
  echo "## 確認できなかった（${n_unknown} 件）"
  echo
  echo "見えていないものを「問題なし」とは扱いません。多くは PAT の権限不足です"
  echo "（Actions: read / Dependabot alerts: read / Contents: read）。"
  echo
  echo "| リポジトリ | 対象 | 応答 |"
  echo "|---|---|---|"
  printf '%s' "$unknown"
  echo
fi

echo "---"
echo "<sub>ci-standard/health · 毎朝 07:30 JST に自動更新。全部緑になれば自動でクローズされます。</sub>"
