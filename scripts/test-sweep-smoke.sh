#!/usr/bin/env bash
# sweep.sh のスモークテスト: gh をスタブに差し替えて実行し、実行時エラーなく終わることを確認する。
#   1) 一覧が空 → 正常終了（exit 0）し「sweep 完了」が出る
#   2) 一覧取得が失敗 → 非ゼロ終了
#   3) 一覧に 1 件（CI 対象外）→ 行が出て正常終了
# 使い方: bash scripts/test-sweep-smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.."
STUB=$(mktemp -d); trap 'rm -rf "$STUB"' EXIT
mk_stub() { # mk_stub <mode>
  cat > "$STUB/gh" <<EOF
#!/usr/bin/env bash
# gh スタブ（mode=$1）。api /user/repos だけ振る舞いを変え、他は失敗（存在しない扱い）にする
case "\$*" in
  *"/user/repos"*)
    case "$1" in
      empty) echo -n "";;
      fail)  echo "gh: HTTP 401" >&2; exit 1;;
      one)   printf 'smoke-repo\tmain\n';;
    esac;;
  *) exit 1;;
esac
EOF
  chmod +x "$STUB/gh"
}
fail=0
check() { # check <名前> <期待rc> <実rc> <出力に含むべき語>
  if [ "$2" = "$3" ] && grep -q -- "$4" "$STUB/out"; then echo "ok   $1"; else echo "FAIL $1 (rc=$3 期待 $2)"; sed -n '1,20p' "$STUB/out"; fail=1; fi
}
mk_stub empty; PATH="$STUB:$PATH" bash scripts/sweep.sh > "$STUB/out" 2>&1; check "一覧が空で正常終了" 0 $? "sweep 完了"
mk_stub fail;  PATH="$STUB:$PATH" bash scripts/sweep.sh > "$STUB/out" 2>&1; check "一覧取得失敗で非ゼロ終了" 1 $? "一覧の取得に失敗"
mk_stub one;   PATH="$STUB:$PATH" bash scripts/sweep.sh > "$STUB/out" 2>&1; check "1 件処理して正常終了" 0 $? "| smoke-repo |"
exit $fail
