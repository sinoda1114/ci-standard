#!/usr/bin/env bash
# PR のレビュースレッドを JSON で取得する（GraphQL、最大 100 件）。
#   usage: fetch-threads.sh <owner/repo> <PR番号> > threads.json
# 出力: [{"id","url","path","line","author","body","isResolved","isOutdated","comments"}]
set -uo pipefail
repo="${1:?owner/repo}"; num="${2:?PR number}"
owner="${repo%%/*}"; name="${repo##*/}"
gh api graphql -F owner="$owner" -F name="$name" -F num="$num" -f query='
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$num){
      reviewThreads(first:100){
        nodes{
          id isResolved isOutdated path line
          comments(first:10){ totalCount nodes{ url author{login} body } }
        }
      }
    }
  }
}' --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | {
  id, isResolved, isOutdated, path, line,
  url: .comments.nodes[0].url,
  author: .comments.nodes[0].author.login,
  body: .comments.nodes[0].body,
  comments: .comments.totalCount }]'
