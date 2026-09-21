#!/usr/bin/env bash
# PR のレビュースレッドを JSON で取得する（GraphQL、ページングで全件）。
#   usage: fetch-threads.sh <owner/repo> <PR番号> > threads.json
# 出力: [{"id","url","path","line","author","authorType","body","isResolved","isOutdated","comments"}]
#   authorType は GraphQL の __typename（"Bot" / "User" / "Mannequin"）。triage.py が Bot 判定に使う。
# 環境変数: PR_TRIAGE_PAGE（1 ページ件数、既定 100。ページング動作の試験用）
set -euo pipefail
repo="${1:?owner/repo}"; num="${2:?PR number}"
owner="${repo%%/*}"; name="${repo##*/}"
page="${PR_TRIAGE_PAGE:-100}"
# --paginate は $endCursor 変数と pageInfo を要求する。--slurp は --jq と併用不可なので、
# ページ配列を jq で 1 つの配列へ畳む。
gh api graphql --paginate --slurp -F owner="$owner" -F name="$name" -F num="$num" -F page="$page" -f query='
query($owner:String!,$name:String!,$num:Int!,$page:Int!,$endCursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$num){
      reviewThreads(first:$page, after:$endCursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved isOutdated path line
          comments(first:10){ totalCount nodes{ url author{login __typename} body } }
        }
      }
    }
  }
}' | jq '[.[] | .data.repository.pullRequest.reviewThreads.nodes[] | {
  id, isResolved, isOutdated, path, line,
  url: .comments.nodes[0].url,
  author: .comments.nodes[0].author.login,
  authorType: .comments.nodes[0].author.__typename,
  body: .comments.nodes[0].body,
  comments: .comments.totalCount }]'
