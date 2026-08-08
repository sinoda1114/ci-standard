#!/usr/bin/env bash
# 標準CI導入済みリポジトリへブランチ保護を一括適用する。
#
# - default branch の ci.yml が sinoda1114/ci-standard を参照しているリポジトリが対象
# - node-ci 参照 → 必須チェック ["ci / build", "ci / e2e"]
#   python-ci 参照 → ["ci / build"]
# - private リポジトリは GitHub Free では保護不可（403）。警告して続行
# - pj-pilot は PR #38 マージ前でも新チェック名で先行設定する（特例）
set -uo pipefail

apply_protection() {
  local NWO=$1 BRANCH=$2 CONTEXTS=$3
  if gh api -X PUT "/repos/${NWO}/branches/${BRANCH}/protection" --input - >/dev/null 2>&1 <<EOF
{
  "required_status_checks": { "strict": false, "contexts": ${CONTEXTS} },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
  then echo "OK    ${NWO} (${CONTEXTS})"
  else echo "WARN  ${NWO} 保護設定失敗（private + Free プランなら仕様）"
  fi
}

# 特例: pj-pilot（標準CI切替PRのマージ前に新チェック名へ更新しておく）
apply_protection "sinoda1114/pj-pilot" "main" '["ci / build", "ci / e2e"]'

gh api '/user/repos?per_page=100' -q '.[] | select(.archived==false and .fork==false) | "\(.name)\t\(.default_branch)"' |
while IFS=$'\t' read -r NAME BRANCH; do
  [ "$NAME" = "pj-pilot" ] && continue
  CI=$(gh api "/repos/sinoda1114/${NAME}/contents/.github/workflows/ci.yml" -q .content 2>/dev/null | base64 -d 2>/dev/null) || continue
  case "$CI" in
    *sinoda1114/ci-standard*node-ci*)   apply_protection "sinoda1114/${NAME}" "$BRANCH" '["ci / build", "ci / e2e"]' ;;
    *sinoda1114/ci-standard*python-ci*) apply_protection "sinoda1114/${NAME}" "$BRANCH" '["ci / build"]' ;;
  esac
done
echo "===== 完了 ====="
