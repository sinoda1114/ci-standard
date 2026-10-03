"""audit-gate.py の単体テスト。

ゲートが壊れて常に成功を返しても気付けるよう、「落とすべき入力を確実に落とす」ことを中心に確かめる。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, 'audit-gate.py')
TODAY = '2026-10-03'
BRACES = 'GHSA-vfj7-8cjw-p6xm'
NEXT_RCE = 'GHSA-vcvr-r3jv-pc5j'


def npm_report(*advisories):
    """npm audit --json の形（vulnerabilities[name].via に advisory の dict が入る）。"""
    vulns = {}
    for name, ghsa, severity in advisories:
        vulns[name] = {
            'name': name, 'severity': severity,
            'via': [{'source': 1, 'name': name, 'severity': severity,
                     'url': f'https://github.com/advisories/{ghsa}'}],
        }
        # 依存元（advisory を直接持たず、via が文字列だけ）も混ぜる
        vulns[name + '-parent'] = {'name': name + '-parent', 'severity': severity, 'via': [name]}
    return {'auditReportVersion': 2, 'vulnerabilities': vulns}


def pnpm_report(*advisories):
    """pnpm audit --json の形（advisories[id] に github_advisory_id が入る）。"""
    return {'advisories': {str(i): {'module_name': name, 'severity': severity,
                                    'github_advisory_id': ghsa,
                                    'url': f'https://github.com/advisories/{ghsa}'}
                           for i, (name, ghsa, severity) in enumerate(advisories)},
            'metadata': {'vulnerabilities': {}}}


class AuditGateTest(unittest.TestCase):
    def run_gate(self, report, allowlist, pm='npm'):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'audit-allowlist')
            with open(path, 'w') as f:
                f.write(allowlist)
            p = subprocess.run([sys.executable, GATE, '--pm', pm, '--allowlist', path, '--today', TODAY],
                               input=json.dumps(report), capture_output=True, text=True)
            return p.returncode, p.stdout + p.stderr

    def test_例外リストにない_high_は落とす(self):
        rc, out = self.run_gate(npm_report(('next', NEXT_RCE, 'critical')), f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 1, out)
        self.assertIn(NEXT_RCE, out)

    def test_例外リストにある_high_だけなら通す(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)
        self.assertIn(BRACES, out)

    def test_期限切れの例外は落とす(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2026-10-02 修正版なし\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('期限切れ', out)

    def test_例外と未登録が混在したら落とす(self):
        report = npm_report(('braces', BRACES, 'high'), ('next', NEXT_RCE, 'critical'))
        rc, out = self.run_gate(report, f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 1, out)

    def test_moderate_以下は対象外(self):
        rc, out = self.run_gate(npm_report(('x', 'GHSA-aaaa-bbbb-cccc', 'moderate')), '')
        self.assertEqual(rc, 0, out)

    def test_理由のない行は書式エラーで落とす(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2026-12-31\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('書式', out)

    def test_期限が日付でない行は書式エラーで落とす(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} いつか 修正版なし\n')
        self.assertEqual(rc, 1, out)

    def test_コメントと空行は無視する(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')),
                                f'# コメント\n\n{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)

    def test_pnpm_形式も読める(self):
        rc, _ = self.run_gate(pnpm_report(('braces', BRACES, 'high')), f'{BRACES} 2026-12-31 修正版なし\n', pm='pnpm')
        self.assertEqual(rc, 0)
        rc, _ = self.run_gate(pnpm_report(('next', NEXT_RCE, 'critical')), f'{BRACES} 2026-12-31 修正版なし\n', pm='pnpm')
        self.assertEqual(rc, 1)

    def test_壊れた_JSON_は落とす(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'audit-allowlist')
            open(path, 'w').close()
            p = subprocess.run([sys.executable, GATE, '--pm', 'npm', '--allowlist', path, '--today', TODAY],
                               input='not json', capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)

    def test_audit_のエラー応答は落とす(self):
        # npm はレジストリ障害などで {"error": …} という正しい JSON を出して失敗する
        rc, out = self.run_gate({'error': {'code': 'ENOAUDIT', 'summary': 'Your configured registry does not support audit'}},
                                f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('audit', out)

    def test_想定外の形の_JSON_は落とす(self):
        for report in ({}, {'vulnerabilities': {}}, []):
            rc, out = self.run_gate(report, '')
            self.assertEqual(rc, 1, (report, out))
        rc, out = self.run_gate({'metadata': {}}, '', pm='pnpm')
        self.assertEqual(rc, 1, out)

    def test_GHSA_の無い_high_は除外できず落とす(self):
        report = {'auditReportVersion': 2, 'vulnerabilities': {'x': {'name': 'x', 'severity': 'high', 'via': [
            {'source': 9, 'name': 'x', 'severity': 'high', 'url': 'https://registry.example.com/advisories/9'}]}}}
        rc, out = self.run_gate(report, '')
        self.assertEqual(rc, 1, out)
        rc, out = self.run_gate({'advisories': {'1': {'module_name': 'x', 'severity': 'critical', 'url': ''}}, 'metadata': {}},
                                '', pm='pnpm')
        self.assertEqual(rc, 1, out)

    def test_同じ_GHSA_の重複行は書式エラーで落とす(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')),
                                f'{BRACES} 2026-12-31 修正版なし\n{BRACES} 2027-12-31 延長\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('重複', out)

    def test_使われていない例外は通知する(self):
        rc, out = self.run_gate(npm_report(), f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)
        self.assertIn('使われていない', out)


if __name__ == '__main__':
    unittest.main()
