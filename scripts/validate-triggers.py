#!/usr/bin/env python3
"""ワークフローの on: が GitHub Actions に存在するイベント名・types かを検証する。

2026-09-21 に templates/pr-triage-caller.yml へ存在しない `pull_request_review_thread` を
書いてしまい、sweeper が全リポジトリへ配布した結果、そのワークフローが
"Unexpected value 'pull_request_review_thread'" で startup_failure になり一度も動かなかった。
GitHub は無効なイベント名を無視せずワークフロー全体を構文エラーにする。
YAML として妥当なので yaml.safe_load では検出できない。ここで名前を突き合わせる。

types も検証する。イベント名だけを見ていると、同じ動機（スレッド解決を拾いたい）から

    on:
      pull_request_review:
        types: [resolved, unresolved]

と書いたときに素通りし、GitHub 側では "Unexpected value 'resolved'" で
まったく同じ規模の障害になる。イベント名の検証だけでは再発を止められない。

使い方: python3 scripts/validate-triggers.py [対象ディレクトリ...]
        （省略時は .github/workflows と templates）
"""
import glob, os, sys, yaml

# https://docs.github.com/actions/reference/workflows-and-actions/events-that-trigger-workflows
#
# 値は「そのイベントが取れる types の集合」。None は types を取らないイベント。
# 空集合ではなく None にしているのは「types を書いた時点で誤り」と判定するため。
EVENTS = {
    "branch_protection_rule": {"created", "edited", "deleted"},
    "check_run": {"created", "rerequested", "completed", "requested_action"},
    "check_suite": {"completed"},
    "create": None,
    "delete": None,
    "deployment": None,
    "deployment_status": None,
    "discussion": {"created", "edited", "deleted", "transferred", "pinned", "unpinned",
                   "labeled", "unlabeled", "locked", "unlocked", "category_changed",
                   "answered", "unanswered"},
    "discussion_comment": {"created", "edited", "deleted"},
    "fork": None,
    "gollum": None,
    # 別セッションが main へ追加した項目。EVENTS 化で落とさないよう types 付きで復元する
    "image_version": {"published"},
    "issue_comment": {"created", "edited", "deleted"},
    "issues": {"opened", "edited", "deleted", "transferred", "pinned", "unpinned",
               "closed", "reopened", "assigned", "unassigned", "labeled", "unlabeled",
               "locked", "unlocked", "milestoned", "demilestoned", "typed", "untyped"},
    "label": {"created", "edited", "deleted"},
    "merge_group": {"checks_requested"},
    "milestone": {"created", "closed", "opened", "edited", "deleted"},
    "page_build": None,
    "public": None,
    "pull_request": {"assigned", "unassigned", "labeled", "unlabeled", "opened", "edited",
                     "closed", "reopened", "synchronize", "converted_to_draft",
                     "ready_for_review", "locked", "unlocked", "milestoned", "demilestoned",
                     "review_requested", "review_request_removed", "auto_merge_enabled",
                     "auto_merge_disabled", "enqueued", "dequeued"},
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "pull_request_target": {"assigned", "unassigned", "labeled", "unlabeled", "opened",
                            "edited", "closed", "reopened", "synchronize",
                            "converted_to_draft", "ready_for_review", "locked", "unlocked",
                            "milestoned", "demilestoned", "review_requested",
                            "review_request_removed", "auto_merge_enabled",
                            "auto_merge_disabled", "enqueued", "dequeued"},
    "push": None,
    "registry_package": {"published", "updated"},
    "release": {"published", "unpublished", "created", "edited", "deleted",
                "prereleased", "released"},
    "repository_dispatch": set(),  # types を取るが値は任意（FREEFORM_TYPES で突き合わせを飛ばす）
    "schedule": None,
    "status": None,
    "watch": {"started"},
    "workflow_call": None,
    "workflow_dispatch": None,
    "workflow_run": {"completed", "requested", "in_progress"},
}

# types を任意の文字列で定義できるイベント。値を突き合わせない。
FREEFORM_TYPES = {"repository_dispatch"}

# よくある取り違え。存在しない名前を書いた理由が分かるよう、正しい名前を添えて落とす。
ALIASES = {
    "pull_request_comment": "issue_comment",
    "pull_request_review_thread": None,  # 代替なし。2026-09-21 の障害の原因
}


def on_section(doc):
    """on: の中身を返す。YAML 1.1 では裸の on: が真偽値 True として読まれる。"""
    if not isinstance(doc, dict):
        return None
    return doc.get("on", doc.get(True))


def check(path, doc):
    """1 ファイルを検査し、問題のメッセージ一覧を返す。"""
    problems = []
    on = on_section(doc)
    if on is None:
        return problems  # on: が無いのは Reusable Workflow の一部などでありうる

    if isinstance(on, str):
        items = {on: None}
    elif isinstance(on, list):
        items = {k: None for k in on}
    elif isinstance(on, dict):
        items = on
    else:
        return [f"{path}: on: の形式が不正（{type(on).__name__}）"]

    for ev, cfg in items.items():
        if ev not in EVENTS:
            hint = ""
            if ev in ALIASES:
                correct = ALIASES[ev]
                hint = f"（{correct} を使う）" if correct else "（代替となるイベントは無い）"
            problems.append(f"{path}: '{ev}' は GitHub Actions のイベントとして存在しない{hint}")
            continue

        if not isinstance(cfg, dict) or "types" not in cfg:
            continue

        if ev in FREEFORM_TYPES:
            continue   # 値は利用者が決めるので突き合わせない

        allowed = EVENTS[ev]
        if allowed is None:
            problems.append(f"{path}: '{ev}' は types を取らない")
            continue

        types = cfg["types"]
        types = [types] if isinstance(types, str) else (types or [])
        for t in types:
            if t not in allowed:
                problems.append(
                    f"{path}: '{ev}' の types に '{t}' は無い"
                    f"（使えるのは {', '.join(sorted(allowed))}）"
                )
    return problems


def main(argv):
    dirs = argv[1:] or [".github/workflows", "templates"]
    patterns = [f"{d}/*.{e}" for d in dirs for e in ("yml", "yaml")]
    files = sorted({f for pat in patterns for f in glob.glob(pat)})

    problems = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except Exception as e:
            problems.append(f"{f}: YAML として読めない: {e}")
            continue
        problems += check(f, doc)

    for p in problems:
        print(f"NG {p}")
    if problems:
        print(f"\n{len(problems)} 件の問題があります。"
              "配布すると全リポジトリのワークフローが構文エラーで起動しなくなります。")
        return 1
    print(f"OK {len(files)} ファイルのトリガーと types はすべて有効です")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
