#!/usr/bin/env python3
"""npm / pnpm audit の結果を、リポジトリの例外リスト（.github/audit-allowlist）と突き合わせて合否を出す。

修正版がまだ公開されていない脆弱性で CI が止まり続けるのを避けるための仕組み。
high 以上で落とす方針はそのままにし、除外は理由と期限つきで記録に残す。

使い方: npm audit --json | python3 audit-gate.py --pm npm --allowlist .github/audit-allowlist [--today YYYY-MM-DD]

例外リストの書式（1 行 1 件。# で始まる行はコメント。理由の中の # はそのまま理由の一部）:
    GHSA-xxxx-xxxx-xxxx  YYYY-MM-DD  理由
  期限の日を過ぎた行は無効になり、CI を落とす（放置を防ぐため）。

終了コード: 0 = 通す / 1 = 落とす（未登録の high 以上、期限切れ、書式エラー、audit の失敗や想定外の形の JSON）
GHSA の ID を取り出せない high 以上の advisory は、例外リストで扱えないので常に落とす（fail-closed）。
期限の判定は UTC の日付で行う（TZ の設定に左右されない）。
"""
import argparse
import datetime
import json
import re
import sys

BLOCKING = {'high', 'critical'}
SEVERITIES = {'info', 'low', 'moderate', 'high', 'critical'}
GHSA_RE = re.compile(r'GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}')
def utc_today():
    """期限の判定に使う今日の日付（UTC）。TZ が設定されていても変わらない。"""
    return datetime.datetime.now(datetime.timezone.utc).date()


def text(value):
    """外部の値を文字列として扱う（文字列以外は空文字）。型違いで落ちないようにする。"""
    return value if isinstance(value, str) else ''


def severity_of(obj):
    """重大度を返す。辞書でない・文字列でない・未知の値は None（呼び出し側で構造の不正とする）。"""
    sev = obj.get('severity') if isinstance(obj, dict) else None
    return sev if isinstance(sev, str) and sev in SEVERITIES else None


def add_found(found, ghsa, sev, pkg):
    """同じ GHSA が複数パッケージに出ても上書きせず、パッケージ名をまとめ、重い方の重大度を残す。"""
    old_sev, pkgs = found.get(ghsa, (sev, set()))
    found[ghsa] = ('critical' if 'critical' in (old_sev, sev) else sev, pkgs | {pkg})


def emit(kind, message):
    """ワークフローコマンドを 1 行で出す。外部の文字列の改行で別のコマンド行が作られないよう無害化する。"""
    safe = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f'::{kind}::{safe}')


LINE_RE = re.compile(r'^(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})\s+(\d{4}-\d{2}-\d{2})\s+(\S.*)$')


def load_allowlist(path):
    """{GHSA: (期限, 理由)} と書式エラーの一覧を返す。"""
    entries, errors = {}, []
    try:
        with open(path, encoding='utf-8-sig') as f:  # BOM 付きでも読めるように
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        return {}, [f'{path}: 例外リストを UTF-8 として読めません（{type(e).__name__}）']
    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith('#'):
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
    found, bad, broken = {}, [], []
    for adv in report['advisories'].values():
        sev = severity_of(adv)
        mod = text(adv.get('module_name')) if isinstance(adv, dict) else ''
        if sev is None:
            broken.append(f'{mod or "?"} (重大度が不明、または構造が不正)')
        elif sev in BLOCKING:
            ghsa = ''.join(GHSA_RE.findall(text(adv.get('github_advisory_id')))[:1]) \
                or ''.join(GHSA_RE.findall(text(adv.get('url')))[:1])
            if ghsa:
                add_found(found, ghsa, sev, mod or '?')
            else:
                bad.append(f"{mod or '?'} ({sev}, url={text(adv.get('url')) or '-'})")
    return found, bad, broken


def reaches_blocking_advisory(name, vulns, seen=None):
    """npm: 項目 name から、依存元の参照をたどって high 以上の advisory 本体に届くか（循環は届かない扱い）。"""
    seen = seen or set()
    if name in seen or not isinstance(vulns.get(name), dict):
        return False
    seen.add(name)
    for via in vulns[name].get('via') or []:
        if severity_of(via) in BLOCKING:
            return True
        if isinstance(via, str) and reaches_blocking_advisory(via, vulns, seen):
            return True
    return False


def npm_advisories(report):
    """npm: {GHSA: (重大度, パッケージ名)} と、除外できない指摘（ID なし・構造が不正）の一覧。"""
    found, bad, broken = {}, [], []
    vulns = report['vulnerabilities']
    for name, vuln in vulns.items():
        # 構造の壊れた項目を黙って無視しない（high 以上が隠れる経路になるため）
        if not isinstance(vuln, dict) or not isinstance(vuln.get('via'), list) or severity_of(vuln) is None:
            broken.append(f'{name} (via がリストでない、または重大度が不明)')
            continue
        dangling = False
        for via in vuln['via']:
            if isinstance(via, str):
                if via not in vulns:  # 参照先が無いのは壊れた結果。別の経路で届いても見逃さない
                    broken.append(f'{name} (参照先の項目がありません: {via})')
                    dangling = True
                continue  # 依存元を指すだけ。advisory 本体は別の項目にある（到達は下で確かめる）
            sev = severity_of(via)
            if sev is None:
                broken.append(f'{name} (advisory の重大度が不明)')
            elif sev in BLOCKING:
                ids = GHSA_RE.findall(text(via.get('url')))
                pkg = text(via.get('name')) or name
                if ids:
                    add_found(found, ids[0], sev, pkg)
                else:
                    bad.append(f"{pkg} ({sev}, url={text(via.get('url')) or '-'})")
        if not dangling and severity_of(vuln) in BLOCKING and not reaches_blocking_advisory(name, vulns):
            broken.append(f"{name} ({vuln['severity']}, high 以上の advisory 本体に到達できません)")
    return found, bad, broken


def advisories(report, pm):
    """{GHSA: (重大度, パッケージ名)}、GHSA の ID が無い high 以上、構造エラーの 3 つを返す（後の 2 つは除外できない）。"""
    return pnpm_advisories(report) if pm == 'pnpm' else npm_advisories(report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pm', choices=['npm', 'pnpm'], default='npm')
    ap.add_argument('--allowlist', required=True)
    ap.add_argument('--today', type=datetime.date.fromisoformat, default=None,
                    help='判定日 YYYY-MM-DD（省略時は UTC の今日）。日付でない値は使い方のエラーになる')
    a = ap.parse_args()
    today = a.today or utc_today()

    try:
        report = json.load(sys.stdin)
    except ValueError as e:
        emit('error', f'audit の JSON が読めません: {e}')
        return 1
    problem = shape_error(report, a.pm)
    if problem:
        emit('error', f'{problem}（audit が実行できていない可能性があります。脆弱性なしとは扱いません）')
        return 1
    allow, errors = load_allowlist(a.allowlist)
    try:
        found, no_id, broken = advisories(report, a.pm)
    except Exception as e:  # 想定外の構造でもトレースバックではなく判定の失敗として扱う
        emit('error', f'audit の結果を解釈できません（{type(e).__name__}: {e}）')
        return 1

    blocking, expired, allowed = [], [], 0
    for ghsa, (sev, pkgs) in sorted(found.items()):
        pkg = ', '.join(sorted(pkgs))
        if ghsa not in allow:
            blocking.append(f'{ghsa} ({sev}, {pkg})')
        elif allow[ghsa][0] < today:
            expired.append(f'{ghsa} ({sev}, {pkg}) 期限 {allow[ghsa][0]}')
        else:
            allowed += 1
            emit('notice', f'例外リストにより除外: {ghsa} ({sev}, {pkg}) 期限 {allow[ghsa][0]} 理由: {allow[ghsa][1]}')
    for ghsa in sorted(set(allow) - set(found)):
        if allow[ghsa][0] < today:  # 使われていなくても期限切れの行は放置させない
            expired.append(f'{ghsa}（使われていない例外）期限 {allow[ghsa][0]}')
        else:
            emit('notice', f'使われていない例外です（解消済みなら行を消してください）: {ghsa}')

    for e in errors:
        emit('error', e)
    for b in blocking:
        emit('error', f'high 以上の脆弱性（例外リストに無し）: {b}')
    for n in no_id:
        emit('error', f'high 以上の脆弱性（GHSA の ID が無いため例外リストで除外できません）: {n}')
    for b in broken:
        emit('error', f'audit の結果の構造が不正です（high 以上が隠れている可能性があるため通しません）: {b}')
    for x in expired:
        emit('error', f'例外の期限切れ: {x}（修正版を確認し、行を消すか期限を延ばしてください）')
    ok = not (errors or blocking or expired or no_id or broken)
    print(f'audit-gate: high以上 {sum(len(p) for _, p in found.values()) + len(no_id)} 件 / 除外 {allowed} 件 / 未登録 {len(blocking)} 件 / '
          f'ID なし {len(no_id)} 件 / 構造エラー {len(broken)} 件 / 期限切れ {len(expired)} 件 → {"通過" if ok else "失敗"}')
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main())
