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

と書いたときに素通りする。ただし types の誤りが構文エラーになるのか、単に起動しない
だけなのかは実証していない。そのため報告の文言をイベント名の誤りと分けている。

使い方: python3 scripts/validate-triggers.py [対象ディレクトリ...]
        （省略時は .github/workflows と templates）
"""
import glob
import sys
from typing import NamedTuple

import yaml


class Problem(NamedTuple):
    kind: str  # "event"（構文エラーで起動しないことを実証済み） / "types" / "structure"
    text: str


# types の値が任意（利用者が決める）であることを表す印。値は突き合わせず、構造だけ検証する。
FREEFORM = object()

_PULL_REQUEST_TYPES = frozenset({
    "assigned", "unassigned", "labeled", "unlabeled", "opened", "edited",
    "closed", "reopened", "synchronize", "converted_to_draft",
    "ready_for_review", "locked", "unlocked", "milestoned", "demilestoned",
    "review_requested", "review_request_removed", "auto_merge_enabled",
    "auto_merge_disabled", "enqueued", "dequeued",
})

# 出典: https://docs.github.com/actions/reference/workflows-and-actions/events-that-trigger-workflows
# 確認日: 2026-09-23（check_suite と image_version は同日に一次情報で再確認）
#
# GitHub は activity type を随時追加する。追加されるとこのゲートは正しい記述を拒否する側に
# 反転するので、見慣れない値で NG になったら、まず公式ドキュメントと突き合わせること。
#
# 値は「そのイベントが取れる types の集合」。None は types を取らないイベントで、
# types を書いた時点で誤りと判定する。空集合にしないのはその区別のため。
EVENTS: dict = {
    "branch_protection_rule": frozenset({"created", "edited", "deleted"}),
    "check_run": frozenset({"created", "rerequested", "completed", "requested_action"}),
    # "Although only the completed activity type is supported"。requested / rerequested は
    # webhook 側の値で Actions では使えない（2026-09-23 のレビューで混同があった）
    "check_suite": frozenset({"completed"}),
    "create": None,
    "delete": None,
    "deployment": None,
    "deployment_status": None,
    "discussion": frozenset({"created", "edited", "deleted", "transferred", "pinned",
                             "unpinned", "labeled", "unlabeled", "locked", "unlocked",
                             "category_changed", "answered", "unanswered"}),
    "discussion_comment": frozenset({"created", "edited", "deleted"}),
    "fork": None,
    "gollum": None,
    "image_version": None,  # Activity types 列は "Not applicable"
    "issue_comment": frozenset({"created", "edited", "deleted"}),
    "issues": frozenset({"opened", "edited", "deleted", "transferred", "pinned", "unpinned",
                         "closed", "reopened", "assigned", "unassigned", "labeled",
                         "unlabeled", "locked", "unlocked", "milestoned", "demilestoned",
                         "typed", "untyped"}),
    "label": frozenset({"created", "edited", "deleted"}),
    "merge_group": frozenset({"checks_requested"}),
    "milestone": frozenset({"created", "closed", "opened", "edited", "deleted"}),
    "page_build": None,
    "public": None,
    "pull_request": _PULL_REQUEST_TYPES,
    "pull_request_review": frozenset({"submitted", "edited", "dismissed"}),
    "pull_request_review_comment": frozenset({"created", "edited", "deleted"}),
    "pull_request_target": _PULL_REQUEST_TYPES,
    "push": None,
    "registry_package": frozenset({"published", "updated"}),
    "release": frozenset({"published", "unpublished", "created", "edited", "deleted",
                          "prereleased", "released"}),
    "repository_dispatch": FREEFORM,
    "schedule": None,
    "status": None,
    "watch": frozenset({"started"}),
    "workflow_call": None,
    "workflow_dispatch": None,
    "workflow_run": frozenset({"completed", "requested", "in_progress"}),
}

# よくある取り違え。存在しない名前を書いた理由が分かるよう、正しい名前を添えて落とす。
ALIASES = {
    "pull_request_comment": "issue_comment",
    "pull_request_review_thread": None,  # 代替なし。2026-09-21 の障害の原因
}


def _on_section(doc: object) -> object:
    """on: の中身を返す。YAML 1.1 では裸の on: が真偽値 True として読まれる。"""
    if not isinstance(doc, dict):
        return None
    return doc.get("on", doc.get(True))


def _event_items(on: object) -> dict | None:
    """on: を {イベント名: 設定} に正規化する。形式が不正なら None。"""
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        if not all(isinstance(k, str) for k in on):
            return None  # [push, {pull_request: ...}] のような混在は GitHub が受け付けない
        return {k: None for k in on}
    if isinstance(on, dict):
        return on
    return None


def _check_types(path: str, ev: str, types: object) -> list[Problem]:
    allowed = EVENTS[ev]
    if allowed is None:
        return [Problem("types", f"{path}: '{ev}' は types を取らない")]

    values = [types] if isinstance(types, str) else types
    if not isinstance(values, list) or not all(isinstance(t, str) for t in values):
        return [Problem("structure",
                        f"{path}: '{ev}' の types は文字列か文字列のリストで書く（実際: {types!r}）")]

    if allowed is FREEFORM:
        return []  # 値は利用者が決めるので突き合わせない

    return [
        Problem("types", f"{path}: '{ev}' の types に '{t}' は無い"
                         f"（使えるのは {', '.join(sorted(allowed))}）")
        for t in values if t not in allowed
    ]


def check(path: str, doc: object) -> list[Problem]:
    """1 ファイルを検査し、問題の一覧を返す。"""
    on = _on_section(doc)
    if on is None:
        # GitHub は on: の無いワークフローを起動しない。reusable workflow も on: workflow_call を持つ
        return [Problem("structure", f"{path}: on: が無い（ワークフローとして起動しない）")]

    items = _event_items(on)
    if items is None:
        return [Problem("structure", f"{path}: on: の形式が不正（{on!r}）")]

    found: list[Problem] = []
    for ev, cfg in items.items():
        if ev not in EVENTS:
            hint = ""
            if ev in ALIASES:
                correct = ALIASES[ev]
                hint = f"（{correct} を使う）" if correct else "（代替となるイベントは無い）"
            found.append(Problem("event",
                                 f"{path}: '{ev}' は GitHub Actions のイベントとして存在しない{hint}"))
            continue
        if isinstance(cfg, dict) and "types" in cfg:
            found += _check_types(path, ev, cfg["types"])
    return found


def summarize(problems: list[Problem]) -> str:
    """問題の種別に応じて締めの文言を変える。実証していない影響は断定しない。"""
    head = f"\n{len(problems)} 件の問題があります。"
    if any(p.kind == "event" for p in problems):
        return head + ("存在しないイベント名は、配布すると全リポジトリのワークフローが"
                       "構文エラーで起動しなくなります（2026-09-21 に実証）。")
    return head + ("GitHub Actions が受け付けない、または意図どおりに起動しない可能性があります。"
                   "公式ドキュメントと突き合わせてください。")


def main(argv: list[str]) -> int:
    dirs = argv[1:] or [".github/workflows", "templates"]
    patterns = [f"{d}/*.{e}" for d in dirs for e in ("yml", "yaml")]
    files = sorted({f for pat in patterns for f in glob.glob(pat)})

    if not files:
        # cwd 違い・ディレクトリの改名・引数の打ち間違いで検査が一切走らず緑になるのを防ぐ
        print(f"NG 検査対象が 0 件（{', '.join(dirs)}）。ゲートが空振りしています")
        return 1

    problems: list[Problem] = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except Exception as e:
            problems.append(Problem("structure", f"{f}: YAML として読めない: {e}"))
            continue
        problems += check(f, doc)

    for p in problems:
        print(f"NG {p.text}")
    if problems:
        print(summarize(problems))
        return 1
    print(f"OK {len(files)} ファイルのトリガーと types はすべて有効です")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
