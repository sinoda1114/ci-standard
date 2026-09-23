#!/usr/bin/env bash
# sweep.sh のスモークテスト: gh をスタブに差し替えて実行し、実行時エラーなく終わることを確認する。
#   1) 一覧が空 → 正常終了（exit 0）し「sweep 完了」が出る
#   2) 一覧取得が失敗 → 非ゼロ終了
#   3) 一覧に 1 件（CI 対象外）→ 行が出て正常終了
#   4) 保護を一時解除して配布した直後に CI 状態の取得が失敗（HTTP 500）しても、保護が戻る
#   5) 手動保護（PR 必須）で PUT が 409 → ブランチ + PR を作る。2 回目は PR済み（作り直さない）
# 使い方: bash scripts/test-sweep-smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.."
STUB=$(mktemp -d); trap 'rm -rf "$STUB"' EXIT
mk_stub() { # mk_stub <mode>
  cat > "$STUB/gh" <<EOF
#!/usr/bin/env bash
# gh スタブ（mode=$1）。STATE ファイルで保護の状態（PROTECTED / UNPROTECTED）を追跡する
STATE="$STUB/state"; A="\$*"; echo "\$A" >> "$STUB/calls"
case "$1" in
  empty) case "\$A" in *"/user/repos"*) exit 0;; esac; exit 1;;
  fail)  case "\$A" in *"/user/repos"*) echo "gh: HTTP 401" >&2; exit 1;; esac; exit 1;;
  one)   case "\$A" in *"/user/repos"*) printf 'smoke-repo\tmain\n'; exit 0;; *"pulls?state=closed"*) echo '[]'; exit 0;; *"commits?sha="*) echo 0; exit 0;; *"contents/"*) echo "gh: HTTP 404 Not Found" >&2; exit 1;; esac; exit 1;;
  prpath)   # CI 対象外リポジトリ。既定ブランチは PR 必須の手動保護。PR の有無を STATE で追跡
    case "\$A" in
      *"/user/repos"*)                       printf 'manual\tmain\n'; exit 0;;
      *"/protection"*)                       exit 1;;                       # sweeper 管理の保護ではない
      *"-X PUT"*"contents/"*"branch=main"*)  echo 'gh: Could not create file: Changes must be made through a pull request. (HTTP 409)' >&2; exit 1;;
      *"-X PUT"*"contents/"*)                exit 0;;                       # sweeper/* ブランチへの PUT
      *"git/ref/heads/main"*)                echo deadbeef; exit 0;;
      *"git/ref/heads/sweeper/"*)            exit 1;;                       # ブランチ未作成
      *"-X POST"*"git/refs"*)                exit 0;;
      *"contents/"*)                         echo "gh: HTTP 404 Not Found" >&2; exit 1;;   # 未配布
      *"/branches?"*)                        exit 1;;
      *"/labels/"*)                          echo '{"color":"x","description":"y"}'; exit 0;;
      *"-X PATCH"*|*"-X POST"*)              exit 0;;
      "pr list"*"--head "*"--state open"*)    # そのブランチの open PR 数（pr create で記録した head 名を数える）
        H=\$(printf '%s' "\$A" | sed -E 's/.*--head ([^ ]+).*/\\1/'); n=\$(grep -cx "PR \$H" "\$STATE" 2>/dev/null); echo "\${n:-0}"; exit 0;;
      *"pulls?state=closed"*)                cat "$STUB/closed.json" 2>/dev/null || echo '[]'; exit 0;;   # 閉じた PR の一覧（テストごとに差し替え）
      *"commits?sha="*)                      [ -f "$STUB/deleted" ] && echo 1 || echo 0; exit 0;;   # そのパスのコミット履歴（-q length の結果）
      "pr list"*)                            echo 0; exit 0;;
      "pr create"*)                          H=\$(printf '%s' "\$A" | sed -E 's/.*--head ([^ ]+).*/\\1/'); echo "PR \$H" >> "\$STATE"; exit 0;;
      *)                                     exit 0;;
    esac;;
  reprotect)
    case "\$A" in
      *"/user/repos"*)                       printf 'smoke\tmain\n'; exit 0;;
      *"contents/package.json"*)             exit 0;;                       # Node リポジトリ
      *"contents/playwright.config"*)        echo "gh: HTTP 404 Not Found" >&2; exit 1;;
      *"workflows/ci.yml"*" -q .content"*)   printf 'uses: sinoda1114/ci-standard/.github/workflows/node-ci.yml@main\n' | base64; exit 0;;  # 標準CI導入済み
      *"workflows/ci.yml"*)                  echo "gh: HTTP 500 Internal Server Error" >&2; exit 1;;   # CI 状態の取得だけ障害
      *"pulls?state=closed"*)                cat "$STUB/closed.json" 2>/dev/null || echo '[]'; exit 0;;
      *"commits?sha="*)                      [ -f "$STUB/deleted" ] && echo 1 || echo 0; exit 0;;
      *"-X DELETE"*"/protection"*)           echo UNPROTECTED >> "\$STATE"; exit 0;;
      *"-X PUT"*"/protection"*)              echo PROTECTED >> "\$STATE"; exit 0;;
      *"/protection"*)                       echo "ci / build"; exit 0;;     # sweeper 管理の保護あり
      *"-X PUT"*"contents/"*)                # ファイル配布: 保護中は 409、解除後は成功
        if [ "\$(tail -1 "\$STATE" 2>/dev/null)" = UNPROTECTED ]; then exit 0; fi
        echo 'gh: Could not create file: Required status check "ci / build" is expected. (HTTP 409)' >&2; exit 1;;
      *"contents/AGENTS.md"*)                [ -f "$STUB/has-agents" ] && { echo sha1; exit 0; }; echo "gh: HTTP 404 Not Found" >&2; exit 1;;
      *"contents/"*)                         echo "gh: HTTP 404 Not Found" >&2; exit 1;;   # dependabot.yml / pr-triage.yml は未配布
      *"/labels/"*)                          echo '{"color":"x","description":"y"}'; exit 0;;
      *"-X PATCH"*|*"-X POST"*)              exit 0;;
      "secret set"*)                         exit 0;;
      "pr list"*)                            echo 0; exit 0;;
      *)                                     exit 0;;
    esac;;
esac
EOF
  chmod +x "$STUB/gh"; : > "$STUB/state"; : > "$STUB/calls"
}
fail=0
check() { # check <名前> <期待rc> <実rc> <出力に含むべき語>
  if [ "$2" = "$3" ] && grep -q -- "$4" "$STUB/out"; then echo "ok   $1"; else echo "FAIL $1 (rc=$3 期待 $2)"; sed -n '1,30p' "$STUB/out"; fail=1; fi
}
mk_stub empty; PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1; check "一覧が空で正常終了" 0 $? "sweep 完了"
mk_stub fail;  PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1; check "一覧取得失敗で非ゼロ終了" 1 $? "一覧の取得に失敗"
mk_stub one;   PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1; check "1 件処理して正常終了" 0 $? "| smoke-repo |"
mk_stub reprotect; PATH="$STUB:$PATH" TYPESAFE_API_KEY=dummy bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1; rc=$?
check "解除して配布し CI 取得失敗でも正常終了" 0 $rc "配布(保護を一時解除)"
check "骨格ファイルも配布される" 0 $rc "骨格:AGENTS:配布 CLAUDE:配布 issue:配布"
if [ "$(tail -1 "$STUB/state")" = PROTECTED ] && grep -q "保護を再適用" "$STUB/out"; then echo "ok   CI 状態取得失敗の後に保護が戻る"; else echo "FAIL 保護が戻らない: state=$(tr '\n' ' ' < "$STUB/state")"; sed -n '1,30p' "$STUB/out"; fail=1; fi
mk_stub prpath; PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1; check "手動保護では PR を作る" 0 $? "dependabot:PR作成 / pr-triage:PR作成"
PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1; rc=$?
check "2 回目は PR済み（作り直さない）" 0 $rc "dependabot:PR済み / pr-triage:PR済み"
check "手動保護では骨格は PR にせず見送る" 0 $rc "骨格:AGENTS:保護のため見送り CLAUDE:保護のため見送り issue:保護のため見送り"
if [ "$(grep -c '^PR ' "$STUB/state")" = 2 ]; then echo "ok   PR は 2 本（dependabot / pr-triage）だけ作られた"; else echo "FAIL PR 作成回数=$(grep -c '^PR ' "$STUB/state")"; cat "$STUB/state"; fail=1; fi
closed_pr() { # closed_pr <head> <closed_at> <本文> <ラベル名 or 空>
  local lab=""; [ -n "$4" ] && lab="{\"name\":\"$4\"}"
  printf '{"head":{"ref":"%s","repo":{"full_name":"%s"}},"merged_at":null,"closed_at":"%s","body":"%s","labels":[%s]}' "$1" "${5:-sinoda1114/manual}" "$2" "$3" "$lab"
}
# 人が断った（印無し・印導入後に閉じた）旧内容の PR
printf '[%s,%s]' "$(closed_pr sweeper/pr-triage-00000000 2026-09-22T00:00:00Z '' '')" "$(closed_pr sweeper/dependabot-00000000 2026-09-22T00:00:00Z '' '')" > "$STUB/closed.json"
mk_stub prpath; PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1
check "人が断った新規導入は内容が変わっても再提案しない" 0 $? "dependabot:PR却下済み / pr-triage:PR却下済み"
printf '[%s,%s]' "$(closed_pr sweeper/pr-triage-00000000 2026-09-22T00:00:00Z '' '' sinoda1114/smoke)" "$(closed_pr sweeper/dependabot-00000000 2026-09-22T00:00:00Z '' '' sinoda1114/smoke)" > "$STUB/closed.json"
mk_stub reprotect; PATH="$STUB:$PATH" TYPESAFE_API_KEY=dummy bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1
check "保護を外せるリポジトリでも、断られた新規導入は直接置かない" 0 $? "dependabot:PR却下済み / pr-triage:PR却下済み"
# sweeper 自身が閉じた PR（印の導入前・印無し / ラベル付き / 本文に印）は却下として数えない
printf '[%s,%s,%s]' "$(closed_pr sweeper/pr-triage-00000000 2026-09-21T07:41:00Z '' '')" \
  "$(closed_pr sweeper/dependabot-00000000 2026-09-22T00:00:00Z 'x' sweeper-superseded)" \
  "$(closed_pr sweeper/dependabot-11111111 2026-09-22T00:00:00Z '[sweeper-superseded] 置き換え' '')" > "$STUB/closed.json"
mk_stub prpath; PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1
check "sweeper 自身の置き換え（印の導入前・ラベル・本文の印）は却下と数えない" 0 $? "dependabot:PR作成 / pr-triage:PR作成"
# フォークからの同名 PR（第三者が開いて閉じた）は却下として数えない
printf '[%s,%s]' "$(closed_pr sweeper/pr-triage-00000000 2026-09-22T00:00:00Z '' '' someone/manual)" "$(closed_pr sweeper/dependabot-00000000 2026-09-22T00:00:00Z '' '' someone/manual)" > "$STUB/closed.json"
mk_stub prpath; PATH="$STUB:$PATH" bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1
check "フォークからの同名 PR は却下と数えない" 0 $? "dependabot:PR作成 / pr-triage:PR作成"
rm -f "$STUB/closed.json"
mk_stub reprotect; touch "$STUB/has-agents"; PATH="$STUB:$PATH" TYPESAFE_API_KEY=dummy bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1
check "既にある AGENTS.md は中身に関係なく触らない" 0 $? "骨格:AGENTS:既存 CLAUDE:"
if grep -q 'PUT.*contents/AGENTS.md' "$STUB/calls" 2>/dev/null; then echo "FAIL 既存 AGENTS.md に PUT した"; fail=1; else echo "ok   既存 AGENTS.md へ PUT していない"; fi
rm -f "$STUB/has-agents"
mk_stub reprotect; touch "$STUB/deleted"; PATH="$STUB:$PATH" TYPESAFE_API_KEY=dummy bash scripts/sweep.sh < /dev/null > "$STUB/out" 2>&1
check "人が消した骨格ファイルは置き直さない" 0 $? "骨格:AGENTS:削除済みのため見送り CLAUDE:削除済みのため見送り issue:削除済みのため見送り"
rm -f "$STUB/deleted"
exit $fail
