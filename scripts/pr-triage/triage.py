#!/usr/bin/env python3
"""PR の Bot レビュースレッドを JEV で仕分ける（正誤の判定はしない）。

usage: triage.py --threads threads.json --out triage.json [--md triage.md]

やること:
  1. 各スレッドを分類（Choice）: security / bug / design / docs / style / question / meta
  2. 同じファイルのスレッド同士に「同じ問題か」（Noul）を問い、≥ 0.7 でグループ化（連結成分）
  3. グループを security → bug → design → docs → style → question → meta の順に並べた表を出す

方針:
  - JEV は仕分けだけ。本物か却下かは人か Claude が決める（表の「判定」列は空で出す）
  - キー無し・API エラー時はフォールバック（分類 unknown、グループ化はファイル+行の近さのみ）で必ず表を出す
  - 秘密情報らしき値を含むスレッド本文は JEV に送らない（分類 unknown で残す）
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

ROUTES = [("TYPESAFE_API_KEY", "https://api.typesafe.ai/v1/systemone", "jev-latest"),
          ("AI_GATEWAY_API_KEY", "https://ai-gateway.vercel.sh/typesafe/v1/systemone", "typesafe-ai/jev")]
TIMEOUT = 20
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
SECRET_RE = re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|"
                       r"(?i:(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]))")


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


SHORT = {"chatgpt-codex-connector": "codex", "copilot-pull-request-reviewer": "copilot", "devin-ai-integration": "devin",
         "amazon-q-developer": "amazon-q", "cursor": "cursor"}


def short(author):
    return SHORT.get(author or "", (author or "?").split("[")[0])


def clean(body):
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", required=True); ap.add_argument("--out", required=True); ap.add_argument("--md", default="")
    a = ap.parse_args()
    threads = json.loads(Path(a.threads).read_text())
    for i, t in enumerate(threads):
        t["idx"] = i; t["text"] = clean(t.get("body") or "")[:6000]; t["file"] = (t.get("path") or "").split("/")[-1]
    mock = os.environ.get("PR_TRIAGE_MOCK") == "1"
    route = ("mock", "mock", "mock") if mock else load_route()
    jev_ok = bool(route[0]); errors = []
    t0 = time.time(); calls = 0

    # 1. 分類
    for t in threads:
        t["category"], t["category_conf"], t["is_security"] = "unknown", None, None
        if not jev_ok or SECRET_RE.search(t["text"]):
            continue
        if mock:
            t["category"] = "security" if re.search(r"secur|bypass|inject|secret", t["text"], re.I) else "bug"; t["category_conf"] = 0.9; continue
        ans, err = call(route, f"Review thread by {t.get('author')} on {t.get('path')}:{t.get('line')}\n\n{t['text']}", {
            "category": {"type": "choice", "instructions": "Which kind of review comment is this?", "criteria": CATEGORIES},
            "is_security": {"type": "noul", "instructions": "Does this thread describe a security vulnerability or a weakening of a security control?"},
        }); calls += 1
        if err:
            errors.append(f"{t['idx']}:{err}"); continue
        c = ans.get("category", {}); t["category"] = c.get("choice", "unknown"); t["category_conf"] = c.get("confidence")
        t["is_security"] = ans.get("is_security", {}).get("noul")
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
            if not jev_ok or SECRET_RE.search(A["text"]) or SECRET_RE.search(B["text"]):
                # フォールバック: 行が近い（±5）なら同一とみなす
                if A.get("line") and B.get("line") and abs(A["line"] - B["line"]) <= 5:
                    union(i, j); pairs.append({"a": i, "b": j, "same": None, "by": "line"})
                continue
            if mock:
                same = 0.9 if A.get("line") == B.get("line") else 0.1
            else:
                ans, err = call(route, f"Thread A ({A.get('path')}:{A.get('line')}, by {A.get('author')}):\n{A['text'][:3000]}\n\nThread B ({B.get('path')}:{B.get('line')}, by {B.get('author')}):\n{B['text'][:3000]}",
                                {"same_issue": {"type": "noul", "instructions": SAME_Q}}); calls += 1
                if err:
                    errors.append(f"{i}x{j}:{err}"); continue
                same = ans.get("same_issue", {}).get("noul")
            pairs.append({"a": i, "b": j, "same": same, "by": "jev"})
            if same is not None and same >= 0.7:
                union(i, j)
    groups = {}
    for t in threads:
        groups.setdefault(find(t["idx"]), []).append(t)
    out_groups = []
    for members in groups.values():
        cats = [m["category"] for m in members]
        cat = next((c for c in CAT_ORDER if c in cats), "unknown")
        out_groups.append({"category": cat, "members": [m["idx"] for m in members], "authors": sorted({short(m.get("author")) for m in members}),
                           "thread_ids": [m.get("id") for m in members],
                           "path": members[0].get("path"), "lines": sorted({m.get("line") for m in members if m.get("line")}),
                           "summary": re.sub(r"[*_`#:]+", " ", members[0]["text"].splitlines()[0]).strip()[:120] if members[0]["text"] else ""})
    out_groups.sort(key=lambda g: (CAT_ORDER.index(g["category"]) if g["category"] in CAT_ORDER else 99, -len(g["members"])))
    result = {"jev": "ok" if (jev_ok and not errors) else ("partial" if jev_ok else "unavailable"), "errors": errors[:20],
              "calls": calls, "seconds": round(time.time() - t0, 1), "threads": threads, "pairs": pairs, "groups": out_groups}
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    if a.md:
        lines = [f"# PR triage（{len(threads)} スレッド → {len(out_groups)} グループ、JEV: {result['jev']}、{calls} 呼び出し {result['seconds']}s）", "",
                 "| # | 種別 | 件数 | 指摘元 | 場所 | 要旨 | 判定 |", "|---|---|---|---|---|---|---|"]
        for k, g in enumerate(out_groups, 1):
            loc = f"{(g['path'] or '').split('/')[-1]}:{','.join(str(x) for x in g['lines'][:3])}"
            lines.append(f"| {k} | {g['category']} | {len(g['members'])} | {', '.join(g['authors'])} | {loc} | {g['summary'].replace('|','/')} | |")
        lines += ["", "「判定」列は人または Claude が埋める（本物 / 却下 / 対応済み）。JEV は仕分けだけを行い、正誤は判定しない。"]
        Path(a.md).write_text("\n".join(lines))
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
