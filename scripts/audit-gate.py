#!/usr/bin/env python3
"""npm / pnpm audit の結果を、リポジトリの例外リスト（.github/audit-allowlist）と突き合わせて合否を出す。

修正版がまだ公開されていない脆弱性で CI が止まり続けるのを避けるための仕組み。
high 以上で落とす方針はそのままにし、除外は理由と期限つきで記録に残す。

使い方: npm audit --json | python3 audit-gate.py --pm npm --allowlist .github/audit-allowlist [--today YYYY-MM-DD]

例外リストの書式（1 行 1 件、# 以降はコメント）:
    GHSA-xxxx-xxxx-xxxx  YYYY-MM-DD  理由
  期限の日を過ぎた行は無効になり、CI を落とす（放置を防ぐため）。

終了コード: 0 = 通す / 1 = 落とす（未登録の high 以上、期限切れ、書式エラー、audit の失敗や想定外の形の JSON）
GHSA の ID を取り出せない high 以上の advisory は、例外リストで扱えないので常に落とす（fail-closed）。
期限の判定は runner の日付（UTC）で行う。
"""
import argparse
import datetime
import json
import re
import sys

BLOCKING = {'high', 'critical'}
SEVERITIES = {'info', 'low', 'moderate', 'high', 'critical'}
GHSA_RE = re.compile(r'GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}')
LINE_RE = re.compile(r'^(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\s+(\d{4}-\d{2}-\d{2})\s+(\S.*)$')


def load_allowlist(path):
    """{GHSA: (期限, 理由)} と書式エラーの一覧を返す。"""
    entries, errors = {}, []
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    for no, raw in enumerate(lines, 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            errors.append(f'{path}:{no}: 書式が違います（GHSA-ID 期限YYYY-MM-DD 理由）: {line}')
            continue
        try:
            expiry = datetime.date.fromisoformat(m.group(2))
        except ValueError:
            errors.append(f'{path}:{no}: 期限が日付として読めません: {m.group(2)}')
            continue
        if m.group(1) in entries:
            errors.append(f'{path}:{no}: 同じ GHSA が重複しています（1 行にまとめてください）: {m.group(1)}')
            continue
        entries[m.group(1)] = (expiry, m.group(3))
    return entries, errors


def shape_error(report, pm):
    """audit が失敗した・想定外の形のときに理由を返す（正常なら None）。

    npm は失敗すると {"error": …} という正しい JSON を出して非 0 で終わり、ワークフローは
    その終了コードを捨てている。形を確かめないと「0 件」として通してしまう（fail-open）。
    """
    if not isinstance(report, dict):
        return f'audit の結果がオブジェクトではありません（{type(report).__name__}）'
    if 'error' in report:
        err = report['error']
        detail = err.get('summary') or err.get('code') if isinstance(err, dict) else err
        return f'audit が失敗しました: {detail}'
    if pm == 'pnpm':
        if not isinstance(report.get('advisories'), dict) or 'metadata' not in report:
            return 'pnpm audit の結果に advisories / metadata がありません'
    elif 'auditReportVersion' not in report or not isinstance(report.get('vulnerabilities'), dict):
        return 'npm audit の結果に auditReportVersion / vulnerabilities がありません'
    return None


def pnpm_advisories(report):
    """pnpm: {GHSA: (重大度, パッケージ名)} と、除外できない指摘（ID なし・重大度が不明）の一覧。"""
    found, bad = {}, []
    for adv in report['advisories'].values():
        sev = adv.get('severity') if isinstance(adv, dict) else None
        if sev not in SEVERITIES:
            bad.append(f'{adv.get("module_name", "?") if isinstance(adv, dict) else adv} (重大度が不明: {sev})')
        elif sev in BLOCKING:
            ghsa = adv.get('github_advisory_id') or ''.join(GHSA_RE.findall(adv.get('url') or '')[:1])
            if ghsa:
                found[ghsa] = (sev, adv.get('module_name', '?'))
            else:
                bad.append(f"{adv.get('module_name', '?')} ({sev}, url={adv.get('url') or '-'})")
    return found, bad


def reaches_blocking_advisory(name, vulns, seen=None):
    """npm: 項目 name から、依存元の参照をたどって high 以上の advisory 本体に届くか（循環は届かない扱い）。"""
    seen = seen or set()
    if name in seen or not isinstance(vulns.get(name), dict):
        return False
    seen.add(name)
    for via in vulns[name].get('via') or []:
        if isinstance(via, dict) and via.get('severity') in BLOCKING:
            return True
        if isinstance(via, str) and reaches_blocking_advisory(via, vulns, seen):
            return True
    return False


def npm_advisories(report):
    """npm: {GHSA: (重大度, パッケージ名)} と、除外できない指摘（ID なし・構造が不正）の一覧。"""
    found, bad = {}, []
    vulns = report['vulnerabilities']
    for name, vuln in vulns.items():
        # 構造の壊れた項目を黙って無視しない（high 以上が隠れる経路になるため）
        if not isinstance(vuln, dict) or not isinstance(vuln.get('via'), list) or vuln.get('severity') not in SEVERITIES:
            bad.append(f'{name} (構造が不正: via がリストでない、または重大度が不明)')
            continue
        for via in vuln['via']:
            if isinstance(via, str):
                if via not in vulns:  # 参照先が無いのは壊れた結果。別の経路で届いても見逃さない
                    bad.append(f'{name} (構造が不正: 参照先の項目がありません: {via})')
                continue  # 依存元を指すだけ。advisory 本体は別の項目にある（到達は下で確かめる）
            if not isinstance(via, dict) or via.get('severity') not in SEVERITIES:
                bad.append(f'{name} (構造が不正: advisory の重大度が不明: {via})')
            elif via['severity'] in BLOCKING:
                ids = GHSA_RE.findall(via.get('url') or '')
                if ids:
                    found[ids[0]] = (via['severity'], via.get('name', name))
                else:
                    bad.append(f"{via.get('name', name)} ({via['severity']}, url={via.get('url') or '-'})")
        if vuln['severity'] in BLOCKING and not reaches_blocking_advisory(name, vulns):
            bad.append(f"{name} ({vuln['severity']}, 構造が不正: high 以上の advisory 本体に到達できません)")
    return found, bad


def advisories(report, pm):
    """{GHSA: (重大度, パッケージ名)} と、例外リストで除外できない high 以上の可能性がある指摘の一覧。"""
    return pnpm_advisories(report) if pm == 'pnpm' else npm_advisories(report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pm', choices=['npm', 'pnpm'], default='npm')
    ap.add_argument('--allowlist', required=True)
    ap.add_argument('--today', default=datetime.date.today().isoformat())
    a = ap.parse_args()
    today = datetime.date.fromisoformat(a.today)

    try:
        report = json.load(sys.stdin)
    except ValueError as e:
        print(f'::error::audit の JSON が読めません: {e}')
        return 1
    problem = shape_error(report, a.pm)
    if problem:
        print(f'::error::{problem}（audit が実行できていない可能性があります。脆弱性なしとは扱いません）')
        return 1
    allow, errors = load_allowlist(a.allowlist)
    found, no_id = advisories(report, a.pm)

    blocking, expired = [], []
    for ghsa, (sev, pkg) in sorted(found.items()):
        if ghsa not in allow:
            blocking.append(f'{ghsa} ({sev}, {pkg})')
        elif allow[ghsa][0] < today:
            expired.append(f'{ghsa} ({sev}, {pkg}) 期限 {allow[ghsa][0]}')
        else:
            print(f'::notice::例外リストにより除外: {ghsa} ({sev}, {pkg}) 期限 {allow[ghsa][0]} 理由: {allow[ghsa][1]}')
    for ghsa in sorted(set(allow) - set(found)):
        if allow[ghsa][0] < today:  # 使われていなくても期限切れの行は放置させない
            expired.append(f'{ghsa}（使われていない例外）期限 {allow[ghsa][0]}')
        else:
            print(f'::notice::使われていない例外です（解消済みなら行を消してください）: {ghsa}')

    for e in errors:
        print(f'::error::{e}')
    for b in blocking:
        print(f'::error::high 以上の脆弱性（例外リストに無し）: {b}')
    for n in no_id:
        print(f'::error::high 以上の可能性がある指摘（GHSA の ID が無い、または構造が不正なため例外リストで除外できません）: {n}')
    for x in expired:
        print(f'::error::例外の期限切れ: {x}（修正版を確認し、行を消すか期限を延ばしてください）')
    ok = not (errors or blocking or expired or no_id)
    print(f'audit-gate: high以上 {len(found) + len(no_id)} 件 / 除外 {len(found) - len(blocking) - len(expired)} 件 / '
          f'未登録 {len(blocking)} 件 / ID なし {len(no_id)} 件 / 期限切れ {len(expired)} 件 → {"通過" if ok else "失敗"}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
