#!/usr/bin/env bash
# CI 健全性の見回り: 全リポジトリの既定ブランチの最新 CI 結論と未解決の脆弱性アラートを集め、
# Markdown のレポートを標準出力へ書く。問題が 1 件も無ければ 1 行目に "OK" だけを出す。
#
# 判定する「問題」:
#   1. 既定ブランチの最新 ci.yml run が failure（＝赤のまま放置）
#   2. critical / high の未解決 Dependabot アラートがある
#
# sweeper と違い、このスクリプトは**何も変更しない**（読むだけ）。
# 使い方: GH_TOKEN=<PAT> bash scripts/health-watch.sh
set -uo pipefail

OWNER="${OWNER:-sinoda1114}"
EXCLUDE="${EXCLUDE:-}"
ONLY="${ONLY:-}"   # 動作確認用: スペース区切りでリポジトリ名を指定すると、それだけを見る

# アーカイブ済みとフォークは除く。既定ブランチ名も取る
LIST=$(gh api --paginate "/user/repos?per_page=100&affiliation=owner" \
  -q '.[] | select(.archived == false) | select(.fork == false) | "\(.name)\t\(.default_branch)"' 2>/dev/null) || {
  echo "リポジトリ一覧の取得に失敗しました" >&2; exit 1; }

red=""; vuln=""; n_red=0; n_vuln=0; n_total=0

while IFS=$'\t' read -r NAME BRANCH; do
  [ -n "$NAME" ] || continue
  case " $EXCLUDE " in *" $NAME "*) continue;; esac
  if [ -n "$ONLY" ]; then case " $ONLY " in *" $NAME "*) ;; *) continue;; esac; fi
  n_total=$((n_total + 1))

  # 1. 標準CI（ci.yml）の最新 run。ワークフローが無ければ skip（404）
  RUN=$(gh api "/repos/${OWNER}/${NAME}/actions/workflows/ci.yml/runs?branch=${BRANCH}&per_page=1" \
        -q '.workflow_runs[0] | "\(.conclusion // "-")\t\(.created_at[:10])\t\(.html_url)"' 2>/dev/null)
  if [ -n "$RUN" ]; then
    CONC=$(printf '%s' "$RUN" | cut -f1)
    WHEN=$(printf '%s' "$RUN" | cut -f2)
    URL=$(printf '%s' "$RUN" | cut -f3)
    if [ "$CONC" = "failure" ]; then
      red="${red}| [${NAME}](https://github.com/${OWNER}/${NAME}) | ${WHEN} | [run](${URL}) |"$'\n'
      n_red=$((n_red + 1))
    fi
  fi

  # 2. critical / high の未解決アラート。権限が無い/無効なら静かに飛ばす
  AL=$(gh api "/repos/${OWNER}/${NAME}/dependabot/alerts?state=open&severity=critical,high&per_page=100" \
       -q '[.[] | .dependency.package.name] | group_by(.) | map("\(.[0])×\(length)") | join(", ")' 2>/dev/null)
  if [ -n "$AL" ] && [ "$AL" != "" ]; then
    CNT=$(gh api "/repos/${OWNER}/${NAME}/dependabot/alerts?state=open&severity=critical,high&per_page=100" -q 'length' 2>/dev/null || echo 0)
    if [ "${CNT:-0}" -gt 0 ]; then
      vuln="${vuln}| [${NAME}](https://github.com/${OWNER}/${NAME}/security/dependabot) | ${CNT} | ${AL} |"$'\n'
      n_vuln=$((n_vuln + 1))
    fi
  fi
done <<EOF
$LIST
EOF

if [ "$n_red" = 0 ] && [ "$n_vuln" = 0 ]; then
  echo "OK"
  echo
  echo "${n_total} リポジトリを確認し、赤い CI と critical/high の脆弱性はありませんでした（$(date -u +%Y-%m-%d)）。"
  exit 0
fi

echo "毎朝の見回りで問題を検出しました（$(date -u +%Y-%m-%d) 時点・${n_total} リポジトリを確認）。"
echo
echo "**CI は push が無いと走りません。** 休眠リポジトリでは依存だけが古くなり、"
echo "たまに配布 push で CI が走って赤くなっても気付かれないまま残ります。"
echo "このレポートはその放置を拾うためのものです。"
echo

if [ "$n_red" -gt 0 ]; then
  echo "## 既定ブランチの CI が赤のまま（${n_red} 件）"
  echo
  echo "| リポジトリ | 最終実行 | run |"
  echo "|---|---|---|"
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

echo "---"
echo "<sub>ci-standard/health · 毎朝 07:30 JST に自動更新。全部緑になれば自動でクローズされます。</sub>"
