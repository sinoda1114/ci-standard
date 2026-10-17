#!/usr/bin/env bash
# jactionlint の版とチェックサムが、書いてある全ワークフローで揃っているかを確かめる。
# 取得と照合の手順は reusable workflow ごとに書くしかない（呼び出し側のリポジトリでは ci-standard のファイルを読めない）ため、
# 版を上げるときに 1 か所だけ直す取りこぼしをここで落とす。
# 使い方: bash scripts/check-jactionlint-pins.sh [ワークフロー...]（既定は下の FILES）
set -euo pipefail
FILES=()
for f in "$@"; do FILES+=("$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"); done   # 呼び出したディレクトリ基準で解決してから cd する
cd "$(dirname "$0")/.."
[ ${#FILES[@]} -gt 0 ] || FILES=(.github/workflows/self-test.yml .github/workflows/node-ci.yml .github/workflows/python-ci.yml)

status=0
# 値を取り出して比べられる形にする（引用符・行末のコメント・前後の空白を落とす）
value_of() { sed -E "s/^ *$1: *//; s/[[:space:]]+#.*$//; s/[\"']//g; s/[[:space:]]+$//"; }
for key in JACTIONLINT_VERSION JACTIONLINT_SHA256; do
  values=""
  for f in "${FILES[@]}"; do
    v=$(grep -h "^ *${key}:" "$f" | value_of "$key" || true)
    if [ -z "$v" ]; then
      echo "::error file=$f::${key} が書かれていないか、値が空"
      status=1
    elif [ "$(printf '%s\n' "$v" | wc -l)" -gt 1 ]; then
      echo "::error file=$f::${key} が 2 回以上書かれている"
      status=1
    fi
    values="${values}${v}"$'\n'
  done
  distinct=$(printf '%s' "$values" | grep . | sort -u || true)
  if [ "$(printf '%s\n' "$distinct" | grep -c .)" -gt 1 ]; then
    echo "::error::${key} がファイルごとに違う: $(printf '%s ' $distinct)"
    status=1
  fi
done
[ $status -eq 0 ] && echo "jactionlint の版とチェックサムは揃っている"
exit $status
