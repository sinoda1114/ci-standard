#!/usr/bin/env bash
# health-watch.sh のスモークテスト: gh をスタブに差し替えて実行し、判定と出力を確認する。
# 見張りは「見えていない」を「問題なし」と報告すると、誰も気付けないまま Issue が閉じる。
# 正しい状態を OK と言うことより、異常や確認不能を OK と言わないことを重点的に見る。
# 使い方: bash scripts/test-health-smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.."
STUB=$(mktemp -d); trap 'rm -rf "$STUB"' EXIT

# mk_stub <一覧> <runs の応答> <alerts の応答>
#   一覧:    "name<TAB>branch<TAB>private" の行（そのまま出す）。"FAIL" なら取得失敗
#   runs:    -q 適用後の 1 行（"結論<TAB>日付<TAB>URL<TAB>sha"）。"E404" / "E403" / "E500" ならその HTTP エラー
#   alerts:  -q 適用後の 1 行（"件数<TAB>パッケージ"）。"E404" / "E403" / "EDISABLED" ならエラー
#   既定ブランチの先頭 sha は常に HEADSHA
mk_stub() {
  cat > "$STUB/gh" <<EOF
#!/usr/bin/env bash
A="\$*"
err() { case "\$1" in
  E404)      echo "gh: Not Found (HTTP 404)" >&2;;
  E403)      echo "gh: Resource not accessible by personal access token (HTTP 403)" >&2;;
  E500)      echo "gh: Internal Server Error (HTTP 500)" >&2;;
  EDISABLED) echo "gh: Dependabot alerts are disabled for this repository. (HTTP 403)" >&2;;
esac; exit 1; }
case "\$A" in
  *"/user/repos"*)       [ "$1" = FAIL ] && { echo "gh: HTTP 401" >&2; exit 1; }; printf '%b' "$1"; exit 0;;
  *"/branches/"*)        echo HEADSHA; exit 0;;
  *"/actions/workflows/ci.yml/runs"*)
    case "$2" in E*) err "$2";; esac; printf '%b\n' "$2"; exit 0;;
  *"/dependabot/alerts"*)
    case "$3" in E*) err "$3";; esac; printf '%b\n' "$3"; exit 0;;
esac
exit 1
EOF
  chmod +x "$STUB/gh"
}
run() { PATH="$STUB:$PATH" bash scripts/health-watch.sh > "$STUB/out" 2>&1; echo $?; }

fail=0
ok()   { echo "ok   $1"; }
ng()   { echo "FAIL $1"; sed -n '1,30p' "$STUB/out"; fail=1; }
first_is_ok() { [ "$(head -1 "$STUB/out")" = OK ]; }
has()  { grep -qF -- "$1" "$STUB/out"; }

GREEN='success\t2026-09-22\thttps://example/run/1\tHEADSHA'
PUB='pub-repo\tmain\tfalse\n'
PRIV='priv-repo\tmain\ttrue\n'

mk_stub FAIL "$GREEN" '0\t'
[ "$(run)" = 1 ] && ok "一覧取得失敗で非ゼロ終了" || ng "一覧取得失敗で非ゼロ終了"

mk_stub "$PUB" "$GREEN" '0\t'
[ "$(run)" = 0 ] && first_is_ok && ok "全部緑なら OK" || ng "全部緑なら OK"

mk_stub "$PUB" 'startup_failure\t2026-09-21\thttps://example/run/2\tHEADSHA' '0\t'
run >/dev/null; ! first_is_ok && has "pub-repo" && ok "startup_failure を赤として拾う" || ng "startup_failure を赤として拾う"

mk_stub "$PUB" 'timed_out\t2026-09-21\thttps://example/run/3\tHEADSHA' '0\t'
run >/dev/null; ! first_is_ok && ok "timed_out を赤として拾う" || ng "timed_out を赤として拾う"

mk_stub "$PUB" 'skipped\t2026-09-21\thttps://example/run/4\tHEADSHA' '0\t'
run >/dev/null; first_is_ok && ok "skipped は問題にしない" || ng "skipped は問題にしない"

mk_stub "$PUB" E404 '0\t'
run >/dev/null; first_is_ok && ok "ci.yml が無い（404）なら対象外" || ng "ci.yml が無い（404）なら対象外"

mk_stub "$PUB" E403 '0\t'
run >/dev/null; ! first_is_ok && has "確認できなかった" && has "pub-repo" && ok "CI 取得の 403 は OK にしない" || ng "CI 取得の 403 は OK にしない"

mk_stub "$PUB" E500 '0\t'
run >/dev/null; ! first_is_ok && has "確認できなかった" && ok "CI 取得の 500 は OK にしない" || ng "CI 取得の 500 は OK にしない"

mk_stub "$PUB" "$GREEN" E403
run >/dev/null; ! first_is_ok && has "確認できなかった" && ok "アラート取得の 403 は OK にしない" || ng "アラート取得の 403 は OK にしない"

mk_stub "$PUB" "$GREEN" EDISABLED
run >/dev/null; first_is_ok && ok "Dependabot 無効のリポジトリは対象外" || ng "Dependabot 無効のリポジトリは対象外"

mk_stub "$PUB" "$GREEN" '3\tnext×2, undici×1'
run >/dev/null; ! first_is_ok && has "next×2" && ok "public はパッケージ名を出す" || ng "public はパッケージ名を出す"

# 出力は public リポジトリ ci-standard の Issue に載る。private の中身を出さない
mk_stub "$PRIV" "$GREEN" '3\tnext×2, undici×1'
run >/dev/null
if ! first_is_ok && has "priv-repo" && has "(private)" && ! has "next" && ! has "github.com/sinoda1114/priv-repo"; then
  ok "private は名前と件数だけ（パッケージ名・リンクなし）"
else ng "private は名前と件数だけ（パッケージ名・リンクなし）"; fi

mk_stub "$PRIV" 'failure\t2026-09-21\thttps://github.com/sinoda1114/priv-repo/actions/runs/5\tHEADSHA' '0\t'
run >/dev/null
! first_is_ok && has "priv-repo" && ! has "actions/runs/5" && ok "private の赤い CI もリンクを出さない" || ng "private の赤い CI もリンクを出さない"

# ci.yml が PR でしか走らないリポジトリでは、main 上の最新 run が何週間も前の古い失敗のまま残る。
# 今の main を検証した結果ではないので拾わない（拾うと Issue が永久に閉じない。shinoda-dev-lp で実際に起きた）
mk_stub "$PUB" 'failure\t2026-08-08\thttps://example/run/7\tOLDSHA' '0\t'
run >/dev/null; first_is_ok && ok "先頭コミット以外の古い失敗は拾わない" || ng "先頭コミット以外の古い失敗は拾わない"

# sweeper が直さないリポジトリを載せると Issue が永久に閉じない
mk_stub 'ci-standard\tmain\tfalse\n' 'failure\t2026-09-21\thttps://example/run/6\tHEADSHA' '0\t'
run >/dev/null; first_is_ok && ok "既定の除外は sweep.sh と同じ" || ng "既定の除外は sweep.sh と同じ"

exit $fail
