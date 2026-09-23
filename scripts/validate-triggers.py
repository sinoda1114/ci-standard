#!/usr/bin/env python3
"""ワークフローの on: が GitHub Actions に存在するイベント名かを検証する。

2026-09-21 に templates/pr-triage-caller.yml へ存在しない `pull_request_review_thread` を
書いてしまい、sweeper が全リポジトリへ配布した結果、そのワークフローが
"Unexpected value 'pull_request_review_thread'" で startup_failure になり一度も動かなかった。
GitHub は無効なイベント名を無視せずワークフロー全体を構文エラーにする。
YAML として妥当なので yaml.safe_load では検出できない。ここで名前を突き合わせる。

使い方: python3 scripts/validate-triggers.py
"""
import glob, sys, yaml

# https://docs.github.com/actions/reference/workflows-and-actions/events-that-trigger-workflows
VALID = {
    "branch_protection_rule", "check_run", "check_suite", "create", "delete",
    "deployment", "deployment_status", "discussion", "discussion_comment", "fork",
    "gollum", "image_version", "issue_comment", "issues", "label", "merge_group",
    "milestone", "page_build", "public", "pull_request",
    "pull_request_review", "pull_request_review_comment", "pull_request_target",
    "push", "registry_package", "release", "repository_dispatch", "schedule",
    "status", "watch", "workflow_call", "workflow_dispatch", "workflow_run",
}

# よくある取り違え。存在しない名前を書いた理由が分かるよう、正しい名前を添えて落とす。
ALIASES = {
    "pull_request_comment": "issue_comment",
    "pull_request_review_thread": None,  # 代替なし
}


def events(doc):
    if not isinstance(doc, dict):   # Issue フォームの配列など、ワークフローでない YAML は対象外
        return []
    # YAML 1.1 では裸の on: が真偽値 True として読まれる
    on = doc.get("on", doc.get(True))
    if isinstance(on, dict):
        return list(on.keys())
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return on
    return []


def main():
    bad = []
    # GitHub は .yaml も読む。.yml だけを見ていると見逃す。
    patterns = [f"{d}/*.{e}" for d in (".github/workflows", "templates", "templates/skeleton") for e in ("yml", "yaml")]
    files = sorted({f for pat in patterns for f in glob.glob(pat)})
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except Exception as e:
            print(f"NG {f}: YAML として読めない: {e}")
            bad.append(f)
            continue
        for ev in events(doc):
            if ev not in VALID:
                hint = ""
                if ev in ALIASES:
                    correct = ALIASES[ev]
                    hint = f"（{correct} を使う）" if correct else "（代替となるイベントは無い）"
                print(f"NG {f}: '{ev}' は GitHub Actions のイベントとして存在しない{hint}")
                bad.append(f)
    if bad:
        print(f"\n{len(bad)} 件の不正なトリガーがあります。配布すると全リポジトリのワークフローが壊れます。")
        return 1
    print(f"OK {len(files)} ファイルのトリガーはすべて有効です")
    return 0


if __name__ == "__main__":
    sys.exit(main())
