#!/usr/bin/env python3
"""配布する GitHub Issue フォーム（.github/ISSUE_TEMPLATE/*.yml）の形を最低限検査する。
GitHub はスキーマ違反のフォームがあるとテンプレート選択画面ごと表示しなくなるため、全リポジトリへ配る前に止める。
usage: check-issue-forms.py <file>...
"""
import re
import sys
import yaml

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")   # GitHub は英数字・-・_ のみ許す

TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}


def check(path):
    errs = []
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return [f"{path}: 読めない / YAML として不正: {e}"]
    if not isinstance(d, dict):
        return [f"{path}: トップレベルが辞書でない"]
    for k in ("name", "description", "body"):
        if not d.get(k):
            errs.append(f"{path}: 必須キー {k} が無い")
    body = d.get("body") or []
    if not isinstance(body, list) or not body:
        errs.append(f"{path}: body が空か配列でない")
        return errs
    ids = set()
    non_md = 0
    for i, el in enumerate(body):
        if not isinstance(el, dict):
            errs.append(f"{path}: body[{i}] が辞書でない")
            continue
        t = el.get("type")
        if t not in TYPES:
            errs.append(f"{path}: body[{i}] の type {t!r} が不正")
            continue
        attrs = el.get("attributes") or {}
        if t == "markdown":
            if not attrs.get("value"):
                errs.append(f"{path}: body[{i}] markdown に attributes.value が無い")
            continue
        non_md += 1
        if not attrs.get("label"):
            errs.append(f"{path}: body[{i}] に attributes.label が無い")
        if t in ("dropdown", "checkboxes") and not attrs.get("options"):
            errs.append(f"{path}: body[{i}] {t} に options が無い")
        eid = el.get("id")   # markdown 以外は id 必須（無いと GitHub がフォームを無効にする）
        if not eid:
            errs.append(f"{path}: body[{i}] {t} に id が無い")
        elif not ID_RE.match(str(eid)):
            errs.append(f"{path}: body[{i}] の id {eid!r} は英数字・-・_ 以外を含む")
        elif eid in ids:
            errs.append(f"{path}: id {eid!r} が重複")
        else:
            ids.add(eid)
    if non_md == 0:
        errs.append(f"{path}: markdown 以外の入力要素が 1 つも無い")
    return errs


if __name__ == "__main__":
    errors = [e for p in sys.argv[1:] for e in check(p)]
    for e in errors:
        print(e)
    print("OK" if not errors else f"NG {len(errors)} 件")
    sys.exit(1 if errors else 0)
