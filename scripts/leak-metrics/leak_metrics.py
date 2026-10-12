#!/usr/bin/env python3
"""マージ後の「漏れ」を集計し、台帳リポジトリに残す（/ai-review の効果を結果で測るための記録）。

漏れの候補: 既定ブランチに入った fix・revert のコミット（直近 14 日）と、type:fix の Issue。
原因の候補: その 14 日前までにマージされ、同じファイルを触った PR（revert は元の PR）。
判定: 2 段。1 段目（安いモデル）で全件を見て、重大・セキュリティ・不明のものだけ 2 段目（上位モデル）で判定し直す。
     判定できなかったもの（キー無し・API の失敗・読めない出力）は「漏れなし」にせず unjudged として残し、次回に再判定する。
記録: 台帳リポジトリの月ごとの Issue（[漏れの記録] YYYY-MM）の本文に全件を JSON で持つ。
     重大・セキュリティのものは、別に Issue（[漏れ] ...、type:fix）を立てる。
標準ライブラリと gh CLI だけで動く。テスト: test_leak_metrics.py
使い方: leak_metrics.py run --owner <owner> --ledger <owner/repo> [--dry-run]
環境変数: GH_TOKEN（全リポジトリの読み取りと台帳への起票）、ANTHROPIC_API_KEY（判定。無ければ全件 unjudged）
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request

WINDOW_DAYS = 14
STAGE1_MODEL = "claude-haiku-5-5"
STAGE2_MODEL = "claude-opus-5-5"
MAX_PER_RUN = 60            # 1 回の実行で判定する上限（料金の上限を決める）
MAX_PROMPT_CHARS = 60000    # 1 件の判定に渡す差分の上限（文字）
MAX_SUSPECTS = 5
SEVERITIES = ("critical", "high", "other")
LEAK_VALUES = ("yes", "no", "unclear")
FIX_RE = re.compile(r"^(fix|hotfix|revert)(\([^)]*\))?!?:|^Revert\b", re.I)
REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
STATE_BEGIN = "<!-- leak-metrics-state"
STATE_END = "leak-metrics-state -->"


# ------------------------------------------------------------ 純粋な判断（テスト対象）

def is_fix_message(msg):
    return bool(FIX_RE.match((msg or "").strip()))


def revert_target(msg):
    m = REVERT_RE.search(msg or "")
    return m.group(1) if m else None


def _ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def fix_candidates(repo, commits, prs, revert_pr=None):
    """fix・revert のコミットを候補にし、原因の候補（suspects）を付ける"""
    out = []
    for c in commits:
        if not is_fix_message(c["message"]):
            continue
        when = _ts(c["date"])
        target = revert_target(c["message"])
        kind = "revert" if target or c["message"].lower().startswith("revert") else "fix"
        suspects = []
        if target and revert_pr:
            n = revert_pr(target)
            suspects = [p for p in prs if p["number"] == n]
        if not suspects:
            files = set(c.get("files") or [])
            own = set(c.get("prs") or [])
            for p in prs:
                if p["number"] in own or not p.get("merged_at"):
                    continue
                age = when - _ts(p["merged_at"])
                if dt.timedelta(0) < age <= dt.timedelta(days=WINDOW_DAYS) and files & set(p.get("files") or []):
                    suspects.append(p)
        suspects = sorted(suspects, key=lambda p: p["merged_at"], reverse=True)[:MAX_SUSPECTS]
        out.append({"key": "%s@%s" % (repo, c["sha"]), "repo": repo, "kind": kind, "sha": c["sha"],
                    "date": c["date"], "message": c["message"].splitlines()[0][:200],
                    "diff": c.get("diff", ""), "suspects": suspects})
    return out


def issue_candidates(repo, issues, prs):
    """type:fix の Issue を候補にする。原因の候補は直前 14 日にマージされた PR（題名だけで判定する）"""
    out = []
    for i in issues:
        when = _ts(i["created_at"])
        sus = [p for p in prs if p.get("merged_at")
               and dt.timedelta(0) < when - _ts(p["merged_at"]) <= dt.timedelta(days=WINDOW_DAYS)]
        sus = sorted(sus, key=lambda p: p["merged_at"], reverse=True)[:MAX_SUSPECTS]
        out.append({"key": "%s#%d" % (repo, i["number"]), "repo": repo, "kind": "issue", "sha": "",
                    "date": i["created_at"], "message": i["title"][:200],
                    "diff": (i.get("body") or "")[:8000],
                    "suspects": [{"number": p["number"], "title": p["title"], "diff": ""} for p in sus]})
    return out


def parse_judgement(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("is_leak") not in LEAK_VALUES or d.get("severity") not in SEVERITIES:
        return None
    if not isinstance(d.get("security"), bool) or "caused_by_pr" not in d:
        return None
    d.setdefault("rationale", "")
    return d


def needs_stage2(j):
    if j["is_leak"] == "no":
        return False
    return j["is_leak"] == "unclear" or j["severity"] in ("critical", "high") or j["security"]


def build_prompt(c):
    sus = []
    for s in c["suspects"]:
        sus.append("### PR #%s: %s\n```diff\n%s\n```" % (s["number"], s.get("title", ""), (s.get("diff") or "")[:15000]))
    body = ("次の修正（%s）が、直前にマージされた PR が入れた不具合を直したものかを判定してください。\n"
            "判定は次の JSON 1 つだけで返してください。\n"
            '{"is_leak": "yes|no|unclear", "caused_by_pr": <PR 番号か null>, '
            '"severity": "critical|high|other", "security": true|false, "rationale": "<1 文>"}\n'
            "- is_leak: 候補の PR が入れた不具合を直したものなら yes。仕様変更・機能追加・候補と無関係なら no。\n"
            "- severity: critical はデータ消失・認証の迂回・任意コード実行などの大事故、high は利用者に明確な害がある不具合、"
            "other はそれ以外（見た目・軽微な性能など）。\n"
            "- security: 脆弱性（注入・認可・秘密情報の漏えいなど）の修正なら true。\n\n"
            "## 修正: %s\n```diff\n%s\n```\n\n## 原因の候補\n%s\n"
            % (c["kind"], c["message"], c.get("diff", ""), "\n".join(sus)))
    return body[:MAX_PROMPT_CHARS]


def judge(c, api, has_key):
    rec = {k: c.get(k) for k in ("key", "repo", "kind", "sha", "date", "message")}
    rec["suspects"] = [s["number"] for s in c["suspects"]]
    rec.update({"verdict": "unjudged", "serious": False, "severity": None, "security": None,
                "caused_by_pr": None, "rationale": "", "models": []})
    if not c["suspects"]:
        rec["verdict"] = "no-suspect"
        return rec
    if not has_key:
        rec["rationale"] = "API キーが無い"
        return rec
    prompt = build_prompt(c)
    final = None
    for model in (STAGE1_MODEL, STAGE2_MODEL):
        try:
            j = parse_judgement(api(model, prompt))
        except Exception as e:  # API の失敗は unjudged（次回に再判定）
            rec["rationale"] = "API の失敗: %s" % str(e)[:200]
            return rec
        rec["models"].append(model)
        if j is None:
            rec["rationale"] = "%s の出力を読めない" % model
            return rec
        final = j
        if model == STAGE1_MODEL and not needs_stage2(j):
            break
    rec.update({"verdict": "judged", "severity": final["severity"], "security": final["security"],
                "caused_by_pr": final["caused_by_pr"], "rationale": str(final["rationale"])[:500],
                "is_leak": final["is_leak"]})
    rec["serious"] = final["is_leak"] != "no" and (final["severity"] in ("critical", "high") or final["security"])
    return rec


def render_month(records, month):
    judged = [r for r in records if r.get("verdict") == "judged"]
    leaks = [r for r in judged if r.get("is_leak") == "yes"]
    serious = [r for r in records if r.get("serious")]
    unj = [r for r in records if r.get("verdict") == "unjudged"]
    lines = ["# 漏れの記録 %s" % month, "",
             "| 項目 | 件数 |", "|---|---|",
             "| 候補 | %d |" % len(records), "| 判定済み | %d |" % len(judged),
             "| 漏れ（yes） | %d |" % len(leaks), "| 重大・セキュリティ | %d |" % len(serious),
             "| 未判定（次回に再判定） | %d |" % len(unj), ""]
    if serious:
        lines += ["## 重大・セキュリティ", ""]
        lines += ["- %s %s（%s）" % (r["key"], r.get("message", ""), r.get("severity")) for r in serious]
        lines.append("")
    lines += ["自動で更新します（ci-standard の leak-metrics）。手で編集しないでください。", "",
              STATE_BEGIN, json.dumps(records, ensure_ascii=False), STATE_END]
    return "\n".join(lines)


def parse_month(body):
    if not body or STATE_BEGIN not in body:
        return []
    try:
        raw = body.split(STATE_BEGIN, 1)[1].split(STATE_END, 1)[0]
        d = json.loads(raw)
    except (ValueError, IndexError):
        return []
    return d if isinstance(d, list) else []


def pending(candidates, known):
    done = {r["key"] for r in known if r.get("verdict") in ("judged", "no-suspect")}
    return [c for c in candidates if c["key"] not in done]


def merge(known, new):
    idx = {r["key"]: r for r in known}
    for r in new:
        idx[r["key"]] = r
    return list(idx.values())


def cap(todo):
    return todo[:MAX_PER_RUN]


def serious_issue(r):
    title = "[漏れ] %s: %s" % (r["repo"], r.get("message", ""))[:200]
    body = "\n".join([
        "マージ後 %d 日以内に、重大またはセキュリティの修正が入りました。" % WINDOW_DAYS, "",
        "- 修正: %s %s（%s）" % (r["repo"], r.get("sha") or r["key"], r.get("kind")),
        "- 原因と判定された PR: %s" % ("%s#%s" % (r["repo"], r["caused_by_pr"]) if r.get("caused_by_pr") else "不明"),
        "- 重大度: %s / セキュリティ: %s" % (r.get("severity"), r.get("security")),
        "- 判定の理由: %s" % r.get("rationale", ""),
        "- 判定したモデル: %s" % ", ".join(r.get("models") or []), "",
        "push 前の /ai-review で止められたはずかを確かめ、必要なら設計を見直してください。", "",
        "<!-- ai-review-leak:%s -->" % r["key"]])
    return title, body


# ------------------------------------------------------------ 外部との入出力

def gh(*args, input_text=None):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, input=input_text)
    if r.returncode != 0:
        raise RuntimeError("gh %s: %s" % (" ".join(args[:3]), r.stderr.strip()[:300]))
    return r.stdout


def _paged(path):
    """ページ分けされた一覧を 1 つの配列にする（--slurp はページごとの配列の配列を返す）"""
    pages = json.loads(gh("api", "--paginate", "--slurp", path))
    return [x for page in pages for x in (page if isinstance(page, list) else [page])]


def anthropic_api(model, prompt):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": model, "max_tokens": 800,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def collect(owner, exclude):
    now = dt.datetime.now(dt.timezone.utc)
    since_fix = (now - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_pr = now - dt.timedelta(days=WINDOW_DAYS * 2)
    repos = json.loads(gh("repo", "list", owner, "--limit", "200", "--no-archived", "--source",
                          "--json", "nameWithOwner,defaultBranchRef"))
    cands, errors = [], []
    for r in repos:
        name = r["nameWithOwner"]
        if name.split("/")[1] in exclude or not r.get("defaultBranchRef"):
            continue
        branch = r["defaultBranchRef"]["name"]
        try:
            prs = []
            for p in _paged("repos/%s/pulls?state=closed&base=%s&sort=updated&direction=desc&per_page=50" % (name, branch))[:100]:
                if p.get("merged_at") and _ts(p["merged_at"]) >= since_pr:
                    files = [f["filename"] for f in _paged("repos/%s/pulls/%d/files?per_page=100" % (name, p["number"]))]
                    prs.append({"number": p["number"], "merged_at": p["merged_at"], "title": p["title"], "files": files})
            commits = []
            for c in _paged("repos/%s/commits?sha=%s&since=%s&per_page=100" % (name, branch, since_fix)):
                msg = c["commit"]["message"]
                if not is_fix_message(msg):
                    continue
                detail = json.loads(gh("api", "repos/%s/commits/%s" % (name, c["sha"])))
                own = [p["number"] for p in json.loads(gh("api", "repos/%s/commits/%s/pulls" % (name, c["sha"])))]
                commits.append({"sha": c["sha"], "date": c["commit"]["committer"]["date"], "message": msg,
                                "files": [f["filename"] for f in detail.get("files", [])], "prs": own,
                                "diff": "\n".join(f.get("patch", "") for f in detail.get("files", []))})

            def revert_pr(sha, name=name):
                ps = json.loads(gh("api", "repos/%s/commits/%s/pulls" % (name, sha)))
                return ps[0]["number"] if ps else None
            fc = fix_candidates(name, commits, prs, revert_pr=revert_pr)
            for c in fc:  # 原因の候補の差分は、判定に使うものだけ取る
                for s in c["suspects"]:
                    s.setdefault("diff", gh("pr", "diff", str(s["number"]), "-R", name)[:15000])
            issues = [i for i in _paged("repos/%s/issues?state=all&labels=type:fix&since=%s&per_page=100" % (name, since_fix))
                      if "pull_request" not in i and _ts(i["created_at"]) >= now - dt.timedelta(days=WINDOW_DAYS)]
            cands += fc + issue_candidates(name, issues, prs)
        except Exception as e:  # 1 リポジトリの失敗で全体を止めない。失敗は記録に出す
            if "HTTP 409" in str(e):  # 空のリポジトリ（コミットが無い）は対象外
                continue
            errors.append("%s: %s" % (name, str(e)[:200]))
    return cands, errors


def month_issue(ledger, month):
    title = "[漏れの記録] %s" % month
    found = json.loads(gh("issue", "list", "-R", ledger, "--state", "all", "--limit", "100",
                          "--search", "in:title \"%s\"" % title, "--json", "number,title,body"))
    for i in found:
        if i["title"] == title:
            return i["number"], i["body"]
    return None, ""


def run(a):
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    exclude = set(a.exclude.split(",")) | {a.ledger.split("/")[1]}
    cands, errors = collect(a.owner, exclude)
    number, body = month_issue(a.ledger, month)
    known = parse_month(body)
    todo = cap(pending(cands, known))
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    new = [judge(c, anthropic_api, has_key) for c in todo]
    records = merge(known, new)
    print("候補 %d / 今回判定 %d / 重大 %d / 未判定 %d / 取得失敗 %d" % (
        len(cands), len(new), sum(1 for r in new if r["serious"]),
        sum(1 for r in new if r["verdict"] == "unjudged"), len(errors)))
    for e in errors:
        print("取得失敗: " + e, file=sys.stderr)
    if a.dry_run:
        return 0
    text = render_month(records, month)
    if number:
        gh("issue", "edit", str(number), "-R", a.ledger, "--body-file", "-", input_text=text)
    else:
        gh("issue", "create", "-R", a.ledger, "--title", "[漏れの記録] %s" % month, "--body-file", "-", input_text=text)
    for r in new:
        if r["serious"]:
            title, ibody = serious_issue(r)
            gh("issue", "create", "-R", a.ledger, "--title", title, "--label", "type:fix", "--body-file", "-",
               input_text=ibody)
    return 1 if errors and not cands else 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--owner", required=True)
    r.add_argument("--ledger", required=True)
    r.add_argument("--exclude", default="")
    r.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    sys.exit(run(a))


if __name__ == "__main__":
    main()
