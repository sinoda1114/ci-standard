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
import time
import urllib.parse
import urllib.request

WINDOW_DAYS = 14
STAGE1_MODEL = "claude-haiku-5-5"
STAGE2_MODEL = "claude-opus-5-5"
MAX_PER_RUN = 60            # 1 回の実行で判定する上限（料金の上限を決める）
MAX_JUDGE_SECONDS = 20 * 60  # 判定に使う時間の上限。job の timeout-minutes（30）より前に保存まで終える
MAX_PROMPT_CHARS = 60000    # 1 件の判定に渡すプロンプトの上限（文字）
MAX_FIX_DIFF_CHARS = 25000  # そのうち修正の差分に使う分。残りを原因の候補で分ける（候補の節が切り詰めで消えないように）
MAX_SUSPECTS = 5
SEVERITIES = ("critical", "high", "other")
LEAK_VALUES = ("yes", "no", "unclear")
FIX_RE = re.compile(r"^(fix|hotfix|revert)(\([^)]*\))?!?:|^Revert\b", re.I)
REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
STATE_BEGIN = "<!-- leak-metrics-state"
STATE_END = "leak-metrics-state -->"
MAX_BODY_CHARS = 60000      # 台帳 Issue 1 本の本文の上限（GitHub の上限 65,536 文字より小さく取る）
MAX_RATIONALE_CHARS = 500
MAX_SERIOUS_LISTED = 20     # 台帳の 1 本目に一覧で載せる重大の件数（残りは件数だけ。全件は状態に入っている）
SUMMARY_RESERVE = 12000     # 1 本目に集計と重大の一覧を載せるために空けておく文字数（一覧は 1 行 約 450 文字 × 20 件まで）


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
        suspects, incomplete, known = [], "", False
        if target and revert_pr:
            try:
                n = revert_pr(target)
                known = True  # revert 元が分かった（PR が無いことが分かった場合も含む）
            except Exception as e:  # 取れなければ、この候補だけを次回に回す（リポジトリ全体を落とさない）
                n, incomplete = None, "revert 元の PR を取得できない: %s" % str(e)[:150]
            # 原因の候補は revert 元の PR だけ。窓（直前 14 日）の外や一覧に無いなら候補なし（ファイルの重なりで付け替えない）
            suspects = [p for p in prs if p["number"] == n and p.get("merged_at")
                        and dt.timedelta(0) < when - _ts(p["merged_at"]) <= dt.timedelta(days=WINDOW_DAYS)]
        if not suspects and not known:
            files = set(c.get("files") or [])
            own = set(c.get("prs") or [])
            for p in prs:
                if p["number"] in own or not p.get("merged_at"):
                    continue
                age = when - _ts(p["merged_at"])
                if dt.timedelta(0) < age <= dt.timedelta(days=WINDOW_DAYS) and files & set(p.get("files") or []):
                    suspects.append(p)
        suspects = [dict(p) for p in sorted(suspects, key=lambda p: p["merged_at"], reverse=True)[:MAX_SUSPECTS]]  # 候補ごとに別の写し
        out.append({"key": "%s@%s" % (repo, c["sha"]), "repo": repo, "kind": kind, "sha": c["sha"],
                    "date": c["date"], "message": c["message"].splitlines()[0][:200],
                    "diff": c.get("diff", ""), "suspects": suspects, **({"incomplete": incomplete} if incomplete else {})})
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
    d["caused_by_pr"] = pr_number(d.get("caused_by_pr"))
    d["rationale"] = str(d.get("rationale") or "")[:MAX_RATIONALE_CHARS]
    return d


def pr_number(v):
    """AI が返した PR 番号を int か None にそろえる（"10"・"#10" は読む。それ以外は None）"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    m = re.fullmatch(r"\s*#?(\d{1,9})\s*", str(v)) if isinstance(v, str) else None
    return int(m.group(1)) if m else None


def needs_stage2(j):
    if j["is_leak"] == "no":
        return False
    return j["is_leak"] == "unclear" or j["severity"] in ("critical", "high") or j["security"]


def build_prompt(c):
    """修正の差分と原因の候補に別々の予算を持たせる（末尾で一括に切ると、候補の節が丸ごと消えることがある）"""
    head = ("次の修正（%s）が、直前にマージされた PR が入れた不具合を直したものかを判定してください。\n"
            "判定は次の JSON 1 つだけで返してください。\n"
            '{"is_leak": "yes|no|unclear", "caused_by_pr": <PR 番号か null>, '
            '"severity": "critical|high|other", "security": true|false, "rationale": "<1 文>"}\n'
            "- is_leak: 候補の PR が入れた不具合を直したものなら yes。仕様変更・機能追加・候補と無関係なら no。\n"
            "- severity: critical はデータ消失・認証の迂回・任意コード実行などの大事故、high は利用者に明確な害がある不具合、"
            "other はそれ以外（見た目・軽微な性能など）。\n"
            "- security: 脆弱性（注入・認可・秘密情報の漏えいなど）の修正なら true。\n\n" % c["kind"])
    fix = "## 修正: %s\n```diff\n%s\n```\n\n## 原因の候補\n" % (str(c["message"])[:200], (c.get("diff") or "")[:MAX_FIX_DIFF_CHARS])
    sus_list = c["suspects"][:MAX_SUSPECTS]
    wrap = [("### PR #%s: %s\n```diff\n" % (s["number"], str(s.get("title", ""))[:200]), "\n```") for s in sus_list]
    room = MAX_PROMPT_CHARS - len(head) - len(fix) - sum(len(a) + len(b) for a, b in wrap) - len(wrap)  # 枠と改行の分を正確に引く
    per = max(0, room // max(1, len(sus_list)))
    sus = [a + (s.get("diff") or "")[:per] + b for (a, b), s in zip(wrap, sus_list)]  # 題名の % を書式として読まない
    return (head + fix + "\n".join(sus))[:MAX_PROMPT_CHARS]


def base_record(c, why=""):
    rec = {k: c.get(k) for k in ("key", "repo", "kind", "sha", "date", "message")}
    rec["suspects"] = [s["number"] for s in c["suspects"]]
    rec.update({"verdict": "unjudged", "serious": False, "severity": None, "security": None,
                "caused_by_pr": None, "rationale": why, "models": []})
    return rec


def judge(c, api, has_key):
    rec = base_record(c)
    if c.get("incomplete"):  # 材料が欠けたまま判定しない。候補が無くても確定させない（次回に取り直す）
        rec["rationale"] = "材料が欠けている: %s" % c["incomplete"]
        return rec
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
    pr = final["caused_by_pr"] if final["caused_by_pr"] in rec["suspects"] else None  # 候補にない番号は書かない
    rec.update({"verdict": "judged", "severity": final["severity"], "security": final["security"],
                "caused_by_pr": pr, "rationale": final["rationale"],
                "is_leak": final["is_leak"]})
    rec["serious"] = final["is_leak"] != "no" and (final["severity"] in ("critical", "high") or final["security"])
    return rec


class LedgerError(ValueError):
    """台帳の状態を読めない。空として上書きすると記録が消えるので、実行を止める"""


def md_text(v):
    """コミットの題名など外から来る文字列を、台帳の Markdown に安全に載せる（リンク・メンション・HTML を効かせない）"""
    t = re.sub(r"[\r\n`]", " ", str(v or ""))[:200]
    return "`%s`" % t.replace("<", "‹").replace(">", "›")


def state_json(records):
    """状態の JSON。< > & を \\u エスケープするので、どんな値を入れても区切り（<!-- / -->）にならない"""
    return (json.dumps(records, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render_month(records, month, part=1, parts=1, summary_of=None):
    """台帳 Issue 1 本の本文。集計は 1 本目にだけ、月の全件（summary_of）について書く"""
    lines = ["# 漏れの記録 %s%s" % (month, "（%d/%d）" % (part, parts) if parts > 1 else ""), ""]
    if part == 1:
        allr = summary_of if summary_of is not None else records
        judged = [r for r in allr if r.get("verdict") == "judged"]
        serious = [r for r in allr if r.get("serious")]
        lines += ["| 項目 | 件数 |", "|---|---|",
                  "| 候補 | %d |" % len(allr), "| 判定済み | %d |" % len(judged),
                  "| 漏れ（yes） | %d |" % sum(1 for r in judged if r.get("is_leak") == "yes"),
                  "| 重大・セキュリティ | %d |" % len(serious),
                  "| 未判定（次回に再判定） | %d |" % sum(1 for r in allr if r.get("verdict") == "unjudged"), ""]
        if serious:
            lines += ["## 重大・セキュリティ", ""]
            lines += ["- %s %s（%s）" % (md_text(r["key"]), md_text(r.get("message")), md_text(r.get("severity")))
                      for r in serious[:MAX_SERIOUS_LISTED]]
            if len(serious) > MAX_SERIOUS_LISTED:
                lines.append("- ほか %d 件" % (len(serious) - MAX_SERIOUS_LISTED))
            lines.append("")
    lines += ["自動で更新します（ci-standard の leak-metrics）。手で編集しないでください。", "",
              STATE_BEGIN, state_json(records), STATE_END]
    return "\n".join(lines)


def fits(recs, month, part, summary_of):
    room = MAX_BODY_CHARS - (SUMMARY_RESERVE if part == 1 else 0)  # 1 本目の集計は SUMMARY_RESERVE で見込む
    return len(render_month(recs, month, 2, 2)) <= room


def place(records, month):
    """月の記録を本（1 本目・2 本目…）に分ける。戻り値: 番 → 記録の配列。
    既に本に入っている記録は、その本が上限に収まる限り動かさない。新しい記録は最後の本に入れ、あふれたら次の本を作る。
    判定し直しで記録が大きくなって本があふれたときは、あふれた分を後ろの本へ移す。移す先はいつも番の大きい本なので、
    番の大きい本から順に書けば（save_months）、途中で書き込みが失敗しても、記録はどこかの本に必ず残る"""
    parts = {}
    for r in records:
        if r.get("part"):
            parts.setdefault(int(r["part"]), []).append(r)
    parts.setdefault(max(parts, default=1), [])
    moving = [r for r in records if not r.get("part")]
    for n in sorted(parts):  # あふれた本からは、後ろの記録を移す
        while len(parts[n]) > 1 and not fits(parts[n], month, n, records):
            moving.insert(0, parts[n].pop())
    last = max(parts)
    for r in moving:
        if parts[last] and not fits(parts[last] + [r], month, last, records):
            last += 1
            parts[last] = []
        parts[last].append(r)
    for n, rs in parts.items():
        for r in rs:
            r["month"], r["part"] = month, n
    return parts


def parse_month(body):
    """台帳 Issue の本文から状態を読む。状態は必ず本文の最後の 3 行（開始の区切り・1 行の JSON・終わりの区切り）にある。
    JSON は改行を含まないので、値の中に区切りの文字列があっても（旧形式の台帳でも）、そこを状態と取り違えない。
    本文が空なら空。台帳 Issue なのに最後の 3 行が状態の形でない（区切りが壊れた・消えた）、または読めなければ
    LedgerError（空として読むと、次の書き込みで記録を消すので止める）"""
    if not (body or "").strip():
        return []
    lines = body.replace("\r\n", "\n").rstrip("\n").split("\n")  # ブラウザで保存すると CRLF になる
    if len(lines) < 3 or lines[-3] != STATE_BEGIN or lines[-1] != STATE_END:
        raise LedgerError("状態が本文の最後の 3 行にない")
    try:
        d = json.loads(lines[-2])
    except ValueError as e:
        raise LedgerError("状態の JSON を読めない: %s" % e)
    if not isinstance(d, list) or not all(valid_shape(r) for r in d):
        raise LedgerError("状態の形が違う")
    return d


def valid_shape(r):
    """台帳の 1 件の記録の型（人の編集で壊れていたら、別の例外で落ちる前に LedgerError にする）"""
    if not isinstance(r, dict) or not isinstance(r.get("key"), str):
        return False
    types = {"repo": str, "kind": str, "sha": str, "date": str, "message": str, "verdict": str, "rationale": str,
             "month": str, "severity": str, "is_leak": str, "serious": bool, "notified": bool, "security": bool,
             "attempts": int, "part": int, "caused_by_pr": int, "suspects": list, "models": list}
    for k, t in types.items():
        v = r.get(k)
        if v is not None and not (isinstance(v, t) and not (t is int and isinstance(v, bool))):
            return False
    return all(type(n) is int for n in r.get("suspects") or []) and all(isinstance(m, str) for m in r.get("models") or [])


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


def prioritize(todo, known):
    """試した回数が少ないもの・古いものから選ぶ（毎回同じ先頭の候補だけが枠を使い続けないように）"""
    tries = {r["key"]: int(r.get("attempts") or 0) for r in known}
    return sorted(todo, key=lambda c: (tries.get(c["key"], 0), c.get("date") or "", c["key"]))


def month_of(rec, now):
    """記録を入れる月。一度入った月（month）からは動かさない。初めての記録は日付の月で、未来なら今の月"""
    if re.fullmatch(r"\d{4}-\d{2}", str(rec.get("month") or "")):
        return rec["month"]
    d = str(rec.get("date") or "")[:7]
    cur = now.strftime("%Y-%m")
    return d if re.fullmatch(r"\d{4}-\d{2}", d) and d <= cur else cur


LEDGER_TITLE_RE = re.compile(r"^\[漏れの記録\] (\d{4}-\d{2})(?: \((\d+)\))?$")


def ledger_title(month, part):
    return "[漏れの記録] %s" % month + (" (%d)" % part if part > 1 else "")


def load_ledger(issues):
    """台帳リポジトリの Issue（number・title・body）から、全部の月の記録と、月ごとの台帳 Issue を読む。
    同じ月・同じ番の Issue が 2 本あっても（過去の重複）、両方の記録を読む（判定済みを優先）。
    戻り値: (key → 記録, 月 → {番: Issue 番号}) 。読めない台帳があれば LedgerError"""
    recs, slots = {}, {}
    rank = {"judged": 0, "no-suspect": 0, "unjudged": 1}
    for i in sorted(issues, key=lambda i: i["number"]):
        m = LEDGER_TITLE_RE.match(i.get("title") or "")
        if not m:
            continue
        month, part = m.group(1), int(m.group(2) or 1)
        slots.setdefault(month, {}).setdefault(part, i["number"])
        for r in parse_month(i.get("body") or ""):
            r = dict(r, month=month, part=part)  # 入っている本を覚え、書き直しでも動かさない
            old = recs.get(r["key"])
            if old is None or rank.get(r.get("verdict"), 2) < rank.get(old.get("verdict"), 2):
                recs[r["key"]] = r
    return recs, slots


def leak_mark(key):
    return "<!-- ai-review-leak:%s -->" % key


def marked_keys(issues):
    """[漏れ] Issue の本文の最終行の目印だけを読む（本文の途中に書かれた目印は、AI の出力などから入りうるので信じない）"""
    keys = set()
    for i in issues:
        last = (i.get("body") or "").rstrip("\n").split("\n")[-1]
        m = re.fullmatch(r"<!-- ai-review-leak:(\S+) -->", last)
        if m:
            keys.add(m.group(1))
    return keys


def serious_issue(r):
    title = ("[漏れ] %s: %s" % (r["repo"], re.sub(r"[\r\n]", " ", r.get("message") or "")))[:200]
    why = re.sub(r"`", "'", str(r.get("rationale") or ""))[:MAX_RATIONALE_CHARS]
    body = "\n".join([
        "マージ後 %d 日以内に、重大またはセキュリティの修正が入りました。" % WINDOW_DAYS, "",
        "- 修正: %s %s（%s）" % (r["repo"], r.get("sha") or r["key"], r.get("kind")),
        "- 原因と判定された PR: %s" % ("%s#%s" % (r["repo"], r["caused_by_pr"]) if r.get("caused_by_pr") else "不明"),
        "- 重大度: %s / セキュリティ: %s" % (r.get("severity"), r.get("security")),
        "- 判定の理由（AI の出力。そのまま載せる）:", "", "```text", why, "```",
        "- 判定したモデル: %s" % ", ".join(r.get("models") or []), "",
        "push 前の /ai-review で止められたはずかを確かめ、必要なら設計を見直してください。", "",
        leak_mark(r["key"])])
    return title, body


# ------------------------------------------------------------ 外部との入出力

def monotonic():
    return time.monotonic()


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


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


def suspect_diff(repo, s):
    """原因の候補の PR の差分。(差分, 欠けた理由)。大きすぎて取れない（HTTP 406）ものは、取り直しても同じなので
    変更ファイルの一覧で代えて判定を続ける。一時的な失敗は欠けた理由を返す（その候補は次回に判定し直す）"""
    try:
        return gh("pr", "diff", str(s["number"]), "-R", repo)[:MAX_PROMPT_CHARS], ""
    except Exception as e:
        if re.search(r"\bHTTP 406\b", str(e)) or "too large" in str(e).lower():
            return "（差分が大きすぎて取得できない。変更ファイル: %s）" % ", ".join((s.get("files") or [])[:50]), ""
        return "", "PR #%s の差分を取得できない: %s" % (s["number"], str(e)[:150])


def collect(owner, exclude):
    now = utcnow()
    since_fix = (now - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_pr = now - dt.timedelta(days=WINDOW_DAYS * 2)
    repos = json.loads(gh("repo", "list", owner, "--limit", "200", "--no-archived", "--source",
                          "--json", "nameWithOwner,defaultBranchRef"))
    cands, errors = [], []
    for r in repos:
        name = r["nameWithOwner"]
        if name.split("/")[1] in exclude or not r.get("defaultBranchRef"):
            continue
        branch = urllib.parse.quote(r["defaultBranchRef"]["name"], safe="")  # API のパスに入れるので & # + % を逃がす
        try:
            prs = []
            for p in _paged("repos/%s/pulls?state=closed&base=%s&sort=updated&direction=desc&per_page=50" % (name, branch)):
                if p.get("merged_at") and _ts(p["merged_at"]) >= since_pr:
                    files = [f["filename"] for f in _paged("repos/%s/pulls/%d/files?per_page=100" % (name, p["number"]))]
                    prs.append({"number": p["number"], "merged_at": p["merged_at"], "title": p["title"], "files": files})
            commits = []
            for c in _paged("repos/%s/commits?sha=%s&since=%s&per_page=100" % (name, branch, since_fix)):
                msg = c["commit"]["message"]
                if not is_fix_message(msg):
                    continue
                try:  # 1 件の取得の失敗で、リポジトリの残りの候補を落とさない
                    # 変更ファイルは 300 件ごとにページが分かれるので、全ページを読む
                    files_all = [f for page in _paged("repos/%s/commits/%s?per_page=100" % (name, c["sha"]))
                                 for f in (page.get("files") or [] if isinstance(page, dict) else [])]
                    own_prs = json.loads(gh("api", "repos/%s/commits/%s/pulls" % (name, c["sha"])))
                except Exception as e:
                    errors.append("%s@%s: %s" % (name, c["sha"][:7], str(e)[:200]))
                    continue
                own = [p["number"] for p in own_prs]
                if own_prs and c["sha"] not in {p.get("merge_commit_sha") for p in own_prs}:
                    continue  # PR の中で直したコミット（マージコミット方式で既定ブランチに入った）。マージ後の直しではない
                date = min(c["commit"]["committer"]["date"], now.strftime("%Y-%m-%dT%H:%M:%SZ"))  # 未来の日時は今に丸める
                commits.append({"sha": c["sha"], "date": date, "message": msg,
                                "files": [f["filename"] for f in files_all], "prs": own,
                                "diff": "\n".join(f.get("patch", "") for f in files_all)})

            def revert_pr(sha, name=name):
                ps = json.loads(gh("api", "repos/%s/commits/%s/pulls" % (name, sha)))
                return ps[0]["number"] if ps else None
            fc = fix_candidates(name, commits, prs, revert_pr=revert_pr)
            seen_why = set()
            diffs = {}  # 同じ PR の差分は 1 回だけ取る（失敗も同じ結果を、その PR を候補に持つ全員に返す）
            for c in fc:  # 原因の候補の差分は、判定に使うものだけ取る
                for s in c["suspects"]:
                    if s["number"] not in diffs:
                        diffs[s["number"]] = suspect_diff(name, s)
                    s["diff"], why = diffs[s["number"]]
                    if why:
                        c["incomplete"] = why
                        if why not in seen_why:  # 取得の失敗にも数える（終了コードで気づけるように。同じ失敗は 1 回だけ）
                            seen_why.add(why)
                            errors.append("%s: %s" % (name, why))
            try:  # Issue を無効にしたリポジトリ（HTTP 410）などで失敗しても、修正コミットの候補は残す
                issues = [i for i in _paged("repos/%s/issues?state=all&labels=type:fix&since=%s&per_page=100" % (name, since_fix))
                          if "pull_request" not in i and _ts(i["created_at"]) >= now - dt.timedelta(days=WINDOW_DAYS)]
            except Exception as e:
                if "HTTP 410" not in str(e):
                    errors.append("%s: Issue を取得できない: %s" % (name, str(e)[:200]))
                issues = []
            cands += fc + issue_candidates(name, issues, prs)
        except Exception as e:  # 1 リポジトリの失敗で全体を止めない。失敗は記録に出す
            if "HTTP 409" in str(e):  # 空のリポジトリ（コミットが無い）は対象外
                continue
            errors.append("%s: %s" % (name, str(e)[:200]))
    return cands, errors


def valid_record(r, owner, exclude):
    """台帳の記録を、取り直しの前に確かめる（台帳は人が編集できるので、取得先を記録の値に任せない）"""
    repo = r.get("repo")
    if not isinstance(repo, str) or not re.fullmatch(r"%s/[A-Za-z0-9._-]+" % re.escape(owner), repo) \
            or repo.split("/")[1] in (".", ".."):
        return False
    if repo.split("/")[1] in exclude:
        return False
    sus = r.get("suspects")
    if not isinstance(sus, list) or len(sus) > MAX_SUSPECTS or not all(type(n) is int and n > 0 for n in sus):
        return False
    if r.get("kind") in ("fix", "revert"):
        sha = r.get("sha")
        return isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{7,40}", sha)) and r.get("key") == "%s@%s" % (repo, sha)
    if r.get("kind") == "issue":
        return isinstance(r.get("key"), str) and bool(re.fullmatch(r"%s#\d+" % re.escape(repo), r["key"]))
    return False


def refetch(stale, owner, exclude, errors):
    """窓から外れた未判定の記録を、判定できる形に取り直す（試した回数の少ない順に、1 回の上限まで）。
    戻り値: (取り直せた候補, 取り直せなかった記録の key)。取り直せなかったものも「試した」に数える"""
    out, failed = [], []
    for r in sorted(stale, key=lambda r: (int(r.get("attempts") or 0), r.get("date") or ""))[:MAX_PER_RUN]:
        if not valid_record(r, owner, exclude):
            errors.append("台帳の記録が不正なので取り直さない: %s" % str(r.get("key"))[:100])
            failed.append(r["key"])
            continue
        repo = r["repo"]
        try:
            if r["kind"] == "issue":
                i = json.loads(gh("api", "repos/%s/issues/%s" % (repo, r["key"].rsplit("#", 1)[1])))
                diff = (i.get("body") or "")[:8000]
                sus = [{"number": n, "title": json.loads(gh("api", "repos/%s/pulls/%d" % (repo, n)))["title"], "diff": ""}
                       for n in r["suspects"]]
            else:
                # 通常の集め方（collect・fix_candidates）とそろえる: 変更ファイルは全ページ、revert は元の PR だけを窓の中で
                pages = [x for x in _paged("repos/%s/commits/%s?per_page=100" % (repo, r["sha"])) if isinstance(x, dict)]
                d = pages[0] if pages else {}
                diff = "\n".join(f.get("patch", "") for x in pages for f in x.get("files") or [])
                nums = list(r["suspects"])
                target = revert_target((d.get("commit") or {}).get("message", ""))
                if target:  # revert は元の PR を探し直す（前回は取得に失敗して、ファイルの重なりで代えていたかもしれない）
                    ps = json.loads(gh("api", "repos/%s/commits/%s/pulls" % (repo, target)))
                    nums = [ps[0]["number"]] if ps and type(ps[0].get("number")) is int else []
                sus = []
                for n in nums:
                    p = json.loads(gh("api", "repos/%s/pulls/%d" % (repo, n)))
                    if target:
                        m = p.get("merged_at")
                        if not (m and dt.timedelta(0) < _ts(r["date"]) - _ts(m) <= dt.timedelta(days=WINDOW_DAYS)):
                            continue  # revert 元の PR が窓（直前 14 日）の外なら原因の候補にしない
                    s_ = {"number": n, "title": p.get("title", ""),
                          "files": [f["filename"] for f in _paged("repos/%s/pulls/%d/files?per_page=100" % (repo, n))]}
                    s_["diff"], why = suspect_diff(repo, s_)
                    if why:
                        raise RuntimeError(why)
                    sus.append(s_)
        except Exception as e:  # 取り直せなければ未判定のまま残す
            errors.append("%s: 取り直しに失敗: %s" % (r["key"], str(e)[:200]))
            failed.append(r["key"])
            continue
        out.append({k: r.get(k) for k in ("key", "repo", "kind", "sha", "date", "message")} | {"diff": diff, "suspects": sus})
    return out, failed


def list_issues(ledger, label=None):
    """台帳リポジトリの Issue の全件（検索 API は索引が遅れるので使わない。件数で打ち切らない）"""
    path = "repos/%s/issues?state=all&per_page=100" % ledger + ("&labels=%s" % label if label else "")
    return [{"number": i["number"], "title": i.get("title") or "", "body": i.get("body") or ""}
            for i in _paged(path) if "pull_request" not in i]


def save_months(ledger, records, slots, months, now):
    """変わった月の台帳を書き直す。記録は入っている本から動かさず、新しい記録は最後の本（あふれたら次の本）に入れる"""
    for month in sorted(months):
        recs = [r for r in records.values() if month_of(r, now) == month]
        parts = place(recs, month)
        n = max(parts)
        for part in sorted(parts, reverse=True):  # 移す先（番の大きい本）を先に書く（place の説明）
            text = render_month(parts[part], month, part, n, summary_of=recs)
            num = slots.get(month, {}).get(part)
            if num:
                gh("issue", "edit", str(num), "-R", ledger, "--body-file", "-", input_text=text)
            else:
                url = gh("issue", "create", "-R", ledger, "--title", ledger_title(month, part), "--body-file", "-",
                         input_text=text).strip()
                slots.setdefault(month, {})[part] = int(url.rsplit("/", 1)[-1])


def notify(ledger, records, errors):
    """重大で、まだ [漏れ] Issue を立てていない記録について起票する。立て終えたものだけ notified にする。
    失敗したものは notified にしないので、次回に起票し直す（目印で二重の起票を防ぐ）。変わった月を返す"""
    pending_r = [r for r in records.values() if r.get("serious") and not r.get("notified")]
    if not pending_r:
        return []
    try:  # ラベルは無いときだけ作る（--force は毎回、色を乱数で塗り替える）
        gh("label", "create", "type:fix", "-R", ledger, "--color", "d73a4a", "--description", "漏れの記録")
    except Exception:
        pass  # すでにある（あるいは作れない。そのときは起票の失敗として次回に回る）
    try:
        done = marked_keys(list_issues(ledger))  # ラベルを外された [漏れ] Issue も見る
    except Exception as e:  # 一覧が取れないと二重に起票しうるので、今回は起票しない
        errors.append("[漏れ] Issue の確認に失敗: %s" % str(e)[:200])
        return []
    changed = []
    for r in pending_r:
        try:
            if r["key"] not in done:
                title, body = serious_issue(r)
                gh("issue", "create", "-R", ledger, "--title", title, "--label", "type:fix", "--body-file", "-",
                   input_text=body)
            r["notified"] = True
            changed.append(r)
        except Exception as e:  # 1 件の失敗で残りを止めない
            errors.append("[漏れ] Issue の起票に失敗（次回に再試行）: %s: %s" % (r["key"], str(e)[:200]))
    return changed


def run(a):
    started = monotonic()
    now = utcnow()
    exclude = set(a.exclude.split(",")) | {a.ledger.split("/")[1]}
    known, slots = load_ledger(list_issues(a.ledger))  # 読めない台帳があれば止める（上書きで消さない）
    cands, errors = collect(a.owner, exclude)
    stale = [r for r in known.values() if r.get("verdict") == "unjudged" and r["key"] not in {c["key"] for c in cands}]
    again, failed = refetch(stale, a.owner, exclude, errors)  # 窓から外れた未判定も判定し直す
    cands += again
    for k in failed:  # 取り直せなかったものも試した回数に数え、次回は他の記録に順番を回す
        known[k] = dict(known[k], attempts=int(known[k].get("attempts") or 0) + 1)
    todo = prioritize(pending(cands, list(known.values())), list(known.values()))
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    new, skipped = [], set()
    for c in todo[:MAX_PER_RUN]:
        if monotonic() - started > MAX_JUDGE_SECONDS:
            # 時間の上限: 残りは判定せずに未判定として残す（job の時間切れで、判定と料金がまるごと失われないように）
            if c["key"] not in known:
                new.append(base_record(c, "時間の上限で見送り"))
            skipped.add(c["key"])
            continue
        new.append(judge(c, anthropic_api, has_key))
    for c in todo[MAX_PER_RUN:]:  # 上限で見送った分も未判定として残す（次回以降、試した回数の少ない順に選ぶ）
        if c["key"] not in known:
            new.append(base_record(c, "1 回の上限で見送り"))
        skipped.add(c["key"])
    for r in new:
        old = known.get(r["key"]) or {}
        # 見送った分（判定していない）は「試した回数」に数えない
        tried = r["verdict"] == "unjudged" and r["key"] not in skipped
        r["attempts"] = int(old.get("attempts") or 0) + (1 if tried else 0)
        if r.get("serious"):
            r["notified"] = bool(old.get("notified"))
        for k in ("month", "part"):  # 判定し直しても、入っている本から動かさない
            if old.get(k):
                r[k] = old[k]
        known[r["key"]] = r
    print("候補 %d / 今回判定 %d / 重大 %d / 未判定 %d / 取得失敗 %d" % (
        len(cands), len(new), sum(1 for r in new if r["serious"]),
        sum(1 for r in new if r["verdict"] == "unjudged"), len(errors)))
    if a.dry_run:
        for e in errors:
            print("取得失敗: " + e, file=sys.stderr)
        return 0
    changed = {month_of(r, now) for r in new} | {month_of(known[k], now) for k in failed}
    save_months(a.ledger, known, slots, changed, now)  # 起票の前に判定を保存する
    notified = notify(a.ledger, known, errors)
    if notified:  # 起票できたものだけ notified を保存する（失敗したものは次回に起票し直す）
        save_months(a.ledger, known, slots, {month_of(r, now) for r in notified}, now)
    for e in errors:
        print("取得失敗: " + e, file=sys.stderr)
    # 取得に失敗したリポジトリ・記録があれば、保存と起票を済ませてから 0 以外で終える（ワークフローを赤くして気づけるように）
    return 1 if errors else 0


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
