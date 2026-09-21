#!/usr/bin/env python3
"""PR の Bot レビュースレッドを JEV で仕分ける（正誤の判定はしない）。

usage: triage.py --threads threads.json --out triage.json [--md triage.md] [--include-humans] [--include-resolved]

やること:
  0. 対象を絞る: Bot が起票した未解決スレッドだけ（人のレビュー・解決済みは除外。フラグで含められる）
  1. 各スレッドを分類（Choice）: security / bug / design / docs / style / question / meta
  2. 同じファイルのスレッド同士に「同じ問題か」（Noul）を問い、≥ 0.7 でグループ化（連結成分）
  3. グループを security → bug → design → docs → style → question → meta の順に並べた表を出す

方針:
  - JEV は仕分けだけ。本物か却下かは人か Claude が決める（表の「判定」列は空で出す）
  - キー無し・API エラー時はフォールバック（分類 unknown、グループ化はファイル+行の近さのみ）で必ず表を出す
  - JEV が一度エラーを返したら以後の呼び出しは止める（遮断器）。タイムアウト連発で CI の制限時間を食い潰さないため
  - 呼び出し回数に上限（PR_TRIAGE_MAX_CALLS、既定 400）。超えた分はフォールバックで束ねる
  - 秘密情報らしき値を含むスレッド本文は JEV に送らない（分類 unknown で残す）
  - 同一ファイル判定はフルパスで行う（表示だけ basename）
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

ROUTES = [("TYPESAFE_API_KEY", "https://api.typesafe.ai/v1/systemone", "jev-latest"),
          ("AI_GATEWAY_API_KEY", "https://ai-gateway.vercel.sh/typesafe/v1/systemone", "typesafe-ai/jev")]
TIMEOUT = 20


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


MAX_CALLS = _env_int("PR_TRIAGE_MAX_CALLS", 400)          # 呼び出し回数の上限
BUDGET_SECONDS = _env_int("PR_TRIAGE_BUDGET_SECONDS", 480)  # 時間の上限（CI は 15 分、冒頭 3 分待つので 8 分で打ち切る）
CATEGORIES = {
    "security": "a security vulnerability or weakening of a security control (injection, auth bypass, secret exposure, unsafe file/process access, missing validation with security impact)",
    "bug": "incorrect behavior, crash, wrong result, missing error handling, race, or a logic defect that is not security-related",
    "design": "architecture, API shape, maintainability, duplication, or robustness suggestions without a concrete defect",
    "docs": "documentation, comments, PR description, or inconsistency between docs and code",
    "style": "formatting, naming, wording, or cosmetic preferences",
    "question": "a question or request for clarification rather than a finding",
    "meta": "process, tooling, CI configuration, or review-bot meta commentary",
}
CAT_ORDER = ["security", "bug", "design", "docs", "style", "question", "meta", "unknown"]
SAME_Q = "Do thread A and thread B point out the same underlying problem (same root cause), even if worded differently or citing different lines?"
# 外部送信前の秘密情報検知。取りこぼしより過検知を優先する（該当スレッドは JEV に送らず unknown で残るだけ）
SECRET_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|AKIA[0-9A-Z]{16}"                                  # AWS access key
    r"|AIza[0-9A-Za-z_\-]{35}"                            # Google API key
    r"|sk-[A-Za-z0-9_\-]{20,}"                            # OpenAI 系
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"  # GitHub classic / fine-grained
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"                     # Slack
    r"|apikey_[A-Za-z0-9_]{30,}"                          # TypeSafe
    r"|(?i:(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*(['\"][^'\"]{8,}['\"]|[A-Za-z0-9_\-/+=]{16,}))"  # 代入（引用符あり: 8 字以上何でも / なし: 16 字以上の英数記号）
    r")")
# 既知の Bot ログイン（GraphQL の authorType が無い古い threads.json 向けの補助）
BOT_LOGINS = {"chatgpt-codex-connector", "copilot-pull-request-reviewer", "devin-ai-integration", "amazon-q-developer", "cursor"}
SHORT = {"chatgpt-codex-connector": "codex", "copilot-pull-request-reviewer": "copilot", "devin-ai-integration": "devin",
         "amazon-q-developer": "amazon-q", "cursor": "cursor"}


def load_route():
    env = dict(os.environ)
    p = Path.home() / ".config/ai-review/jev.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1); env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for name, ep, model in ROUTES:
        if env.get(name, "").strip():
            return env[name].strip(), ep, model
    return "", "", ""


def call(route, state, questions):
    """JEV を 1 回呼ぶ。(answers, None) か (None, 短いエラー名)。鍵や本文はエラーに含めない。"""
    key, ep, model = route
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(ep, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode()).get("answers", {}), None
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 2:
                time.sleep(2 ** attempt); continue
            return None, f"HTTP {e.code}"
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt); continue
            return None, type(e).__name__
    return None, "unreachable"


def short(author):
    return SHORT.get(author or "", (author or "?").split("[")[0])


def is_bot(t):
    if t.get("authorType"):
        return t["authorType"] == "Bot"
    a = (t.get("author") or "")
    return a in BOT_LOGINS or a.endswith("[bot]")


def clean(body):
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def summarize(text):
    """表の要旨: 先頭行から絵文字・ショートコード・装飾を落として 120 字。秘密情報らしき値を含む行は伏せる"""
    first = next((ln for ln in text.splitlines() if ln.strip()), "") if text else ""
    if SECRET_RE.search(first):
        return "（秘密情報らしき値を含むため要旨を省略）"
    first = re.sub(r":[a-z0-9_+\-]+:", "", first)                 # :stop_sign: 等
    first = re.sub(r"[*_`#]+", " ", first)
    first = re.sub(r"[\U0001F300-\U0001FAFF☀-➿️]", "", first)  # 絵文字
    return re.sub(r"\s+", " ", first).strip(" :-|").strip()[:120]


def near(A, B):
    return bool(A.get("line") and B.get("line") and abs(A["line"] - B["line"]) <= 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", required=True); ap.add_argument("--out", required=True); ap.add_argument("--md", default="")
    ap.add_argument("--include-humans", action="store_true", help="人が起票したスレッドも対象にする")
    ap.add_argument("--include-resolved", action="store_true", help="解決済みスレッドも対象にする")
    a = ap.parse_args()
    raw = json.loads(Path(a.threads).read_text())
    if not isinstance(raw, list):
        print("threads.json は配列である必要があります", file=sys.stderr); return 2

    # 0. 対象を絞る
    dropped = {"human": 0, "resolved": 0}
    threads = []
    for t in raw:
        if not a.include_humans and not is_bot(t):
            dropped["human"] += 1; continue
        if not a.include_resolved and t.get("isResolved"):
            dropped["resolved"] += 1; continue
        threads.append(t)
    for i, t in enumerate(threads):
        t["idx"] = i; t["text"] = clean(t.get("body") or "")[:6000]
        t["file"] = t.get("path") or ""          # フルパスで比較（basename だと別ディレクトリの同名ファイルが混ざる）
        t["has_secret"] = bool(SECRET_RE.search(t["text"]))   # 1 回だけ判定（ペア毎に本文を再走査しない）
    mock = os.environ.get("PR_TRIAGE_MOCK") == "1"
    route = ("mock", "mock", "mock") if mock else load_route()
    jev_ok = bool(route[0]); errors = []
    t0 = time.time(); calls = 0

    def jev(state, questions):
        """遮断器・回数上限つきの呼び出し。None が返ったらフォールバックへ"""
        nonlocal jev_ok, calls
        if not jev_ok:
            return None
        if calls >= MAX_CALLS:
            if "budget" not in errors:
                errors.append("budget")
            return None
        if time.time() - t0 > BUDGET_SECONDS:      # 遅いが成功する API でもジョブの制限時間内に表を出す
            if "deadline" not in errors:
                errors.append("deadline")
            return None
        ans, err = call(route, state, questions); calls += 1
        if err:
            errors.append(err); jev_ok = False      # 以後は呼ばない（タイムアウト連発で CI の制限時間を食い潰さない）
            return None
        if not isinstance(ans, dict):
            errors.append("bad-response"); jev_ok = False
            return None
        return ans

    # 1. 分類
    for t in threads:
        t["category"], t["category_conf"], t["is_security"] = "unknown", None, None
        if not jev_ok or t["has_secret"]:
            continue
        if mock:
            t["category"] = "security" if re.search(r"secur|bypass|inject|secret", t["text"], re.I) else "bug"; t["category_conf"] = 0.9; continue
        ans = jev(f"Review thread by {t.get('author')} on {t.get('path')}:{t.get('line')}\n\n{t['text']}", {
            "category": {"type": "choice", "instructions": "Which kind of review comment is this?", "criteria": CATEGORIES},
            "is_security": {"type": "noul", "instructions": "Does this thread describe a security vulnerability or a weakening of a security control?"},
        })
        if ans is None:
            continue
        c = ans.get("category") if isinstance(ans.get("category"), dict) else {}
        t["category"] = c.get("choice", "unknown") if c.get("choice") in CATEGORIES else "unknown"; t["category_conf"] = c.get("confidence")
        sec = (ans.get("is_security") or {}).get("noul") if isinstance(ans.get("is_security"), dict) else None
        t["is_security"] = sec if isinstance(sec, (int, float)) else None   # 数値以外が返っても落とさない
        if t["is_security"] is not None and t["is_security"] >= 0.7:
            t["category"] = "security"

    # 2. 同一問題のグループ化（同じファイル内のペア。ファイル不明同士も比較）
    n = len(threads); parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(x, y): parent[find(x)] = find(y)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            A, B = threads[i], threads[j]
            if A["file"] != B["file"]:
                continue
            same = None
            if jev_ok and not A["has_secret"] and not B["has_secret"]:
                # 上限超過は jev() が記録するが、ここで手前に立つと記録されないので同じ扱いにする
                if calls >= MAX_CALLS or time.time() - t0 > BUDGET_SECONDS:
                    key = "budget" if calls >= MAX_CALLS else "deadline"
                    if key not in errors:
                        errors.append(key)
                elif mock:
                    same = 0.9 if A.get("line") == B.get("line") else 0.1
                else:
                    ans = jev(f"Thread A ({A.get('path')}:{A.get('line')}, by {A.get('author')}):\n{A['text'][:3000]}\n\n"
                              f"Thread B ({B.get('path')}:{B.get('line')}, by {B.get('author')}):\n{B['text'][:3000]}",
                              {"same_issue": {"type": "noul", "instructions": SAME_Q}})
                    sa = ans.get("same_issue") if ans is not None else None
                    same = sa.get("noul") if isinstance(sa, dict) else None
                    if ans is not None and not isinstance(same, (int, float)):
                        # HTTP は成功したが形が不正。ok を名乗らないよう記録し、以後は呼ばず行近接フォールバックへ
                        errors.append("bad-response"); jev_ok = False; same = None
            if same is None:
                # フォールバック（キー無し・秘密情報・エラー・上限超過）: 行が近い（±5）なら同一とみなす
                if near(A, B):
                    union(i, j); pairs.append({"a": i, "b": j, "same": None, "by": "line"})
                continue
            pairs.append({"a": i, "b": j, "same": same, "by": "jev"})
            if same >= 0.7:
                union(i, j)
    groups = {}
    for t in threads:
        groups.setdefault(find(t["idx"]), []).append(t)
    out_groups = []
    for members in groups.values():
        if not members:
            continue
        cats = [m["category"] for m in members]
        cat = next((c for c in CAT_ORDER if c in cats), "unknown")
        out_groups.append({"category": cat, "members": [m["idx"] for m in members], "authors": sorted({short(m.get("author")) for m in members}),
                           "thread_ids": [m.get("id") for m in members],
                           "path": members[0].get("path"), "lines": sorted({m.get("line") for m in members if m.get("line")}),
                           "summary": summarize(members[0]["text"])})
    out_groups.sort(key=lambda g: (CAT_ORDER.index(g["category"]) if g["category"] in CAT_ORDER else 99, -len(g["members"])))
    if mock:
        status = "ok"
    elif calls == 0:
        status = "unavailable"          # 1 回も呼んでいない（キー無し・全件が秘密情報該当）なら ok を名乗らない
    else:
        status = "ok" if (jev_ok and not errors) else "partial"
    result = {"jev": status, "errors": errors[:20], "calls": calls, "seconds": round(time.time() - t0, 1),
              "dropped": dropped, "threads": threads, "pairs": pairs, "groups": out_groups}
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    if a.md:
        lines = [f"# PR triage（{len(threads)} スレッド → {len(out_groups)} グループ、JEV: {status}、{calls} 呼び出し {result['seconds']}s"
                 + (f"、除外: 人 {dropped['human']} / 解決済み {dropped['resolved']}" if any(dropped.values()) else "") + "）", "",
                 "| # | 種別 | 件数 | 指摘元 | 場所 | 要旨 | 判定 |", "|---|---|---|---|---|---|---|"]
        for k, g in enumerate(out_groups, 1):
            loc = f"{'/'.join((g['path'] or '').split('/')[-2:])}:{','.join(str(x) for x in g['lines'][:3])}".strip(":") or "(場所不明)"
            lines.append(f"| {k} | {g['category']} | {len(g['members'])} | {', '.join(g['authors'])} | {loc} | {g['summary'].replace('|','/')} | |")
        lines += ["", "「判定」列は人または Claude が埋める（本物 / 却下 / 対応済み）。JEV は仕分けだけを行い、正誤は判定しない。"]
        Path(a.md).write_text("\n".join(lines))
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
