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
            with open(path, 'w', encoding='utf-8') as f:
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

    def test_項目の構造が壊れた_high_は落とす(self):
        # via が無い・型が違う・依存元の参照先が無い high の項目を黙って無視しない
        broken = [
            {'x': {'severity': 'high'}},
            {'x': {'severity': 'high', 'via': 'braces'}},
            {'x': {'severity': 'critical', 'via': ['missing-package']}},
            {'x': 'not-an-object'},
        ]
        for vulns in broken:
            rc, out = self.run_gate({'auditReportVersion': 2, 'vulnerabilities': vulns}, '')
            self.assertEqual(rc, 1, (vulns, out))

    def test_advisory_本体に到達できない_high_は落とす(self):
        # 空の advisory・自己参照・循環・severity の無い advisory は、high を隠す経路になる
        broken = [
            {'x': {'severity': 'high', 'via': [{}]}},
            {'x': {'severity': 'high', 'via': ['x']}},
            {'x': {'severity': 'high', 'via': ['y']}, 'y': {'severity': 'high', 'via': ['x']}},
            {'x': {'severity': 'high', 'via': [{'url': f'https://github.com/advisories/{BRACES}'}]}},
        ]
        for vulns in broken:
            rc, out = self.run_gate({'auditReportVersion': 2, 'vulnerabilities': vulns}, f'{BRACES} 2026-12-31 修正版なし\n')
            self.assertEqual(rc, 1, (vulns, out))

    def test_severity_の表記違いや不明な値は落とす(self):
        for sev in ('HIGH', 'severe', None):
            via = {'name': 'x', 'url': 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc'}
            if sev is not None:
                via['severity'] = sev
            report = {'auditReportVersion': 2, 'vulnerabilities': {'x': {'severity': 'high', 'via': [via]}}}
            rc, out = self.run_gate(report, '')
            self.assertEqual(rc, 1, (sev, out))
        report = {'advisories': {'1': {'module_name': 'x', 'severity': 'HIGH',
                                       'github_advisory_id': 'GHSA-aaaa-bbbb-cccc'}}, 'metadata': {}}
        rc, out = self.run_gate(report, '', pm='pnpm')
        self.assertEqual(rc, 1, out)

    def test_依存元が既存の項目を指すだけなら構造エラーにしない(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)

    def test_同じ_GHSA_の重複行は書式エラーで落とす(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')),
                                f'{BRACES} 2026-12-31 修正版なし\n{BRACES} 2027-12-31 延長\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('重複', out)

    def test_参照先の無い依存元が混ざったら落とす(self):
        # 別の経路で advisory に届いても、存在しない項目を指す参照は壊れた結果として扱う
        report = npm_report(('braces', BRACES, 'high'))
        report['vulnerabilities']['x'] = {'name': 'x', 'severity': 'high', 'via': ['braces', 'missing-package']}
        rc, out = self.run_gate(report, f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 1, out)

    def test_使われていない例外でも期限切れなら落とす(self):
        rc, out = self.run_gate(npm_report(), f'{BRACES} 2026-10-02 修正版なし\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('期限切れ', out)

    def test_集計の除外件数は使われていない期限切れを含めない(self):
        report = npm_report(('braces', BRACES, 'high'))
        rc, out = self.run_gate(report, f'{BRACES} 2026-12-31 修正版なし\nGHSA-aaaa-bbbb-cccc 2026-10-01 古い例外\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('除外 1 件', out)
        rc, out = self.run_gate(npm_report(), 'GHSA-aaaa-bbbb-cccc 2026-10-01 古い例外\n')
        self.assertNotIn('除外 -', out)

    def test_型の違う値はトレースバックでなくエラーとして落とす(self):
        cases = [
            {'auditReportVersion': 2, 'vulnerabilities': {'x': {'severity': 'high', 'via': [{'severity': 'high', 'url': 123}]}}},
            {'auditReportVersion': 2, 'vulnerabilities': {'x': {'severity': ['high'], 'via': []}}},
            {'auditReportVersion': 2, 'vulnerabilities': {'x': {'severity': 'high', 'via': [{'severity': {'a': 1}}]}}},
        ]
        for report in cases:
            rc, out = self.run_gate(report, '')
            self.assertEqual(rc, 1, (report, out))
            self.assertNotIn('Traceback', out)
        rc, out = self.run_gate({'advisories': {'1': {'severity': ['high'], 'url': 5}}, 'metadata': {}}, '', pm='pnpm')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('Traceback', out)

    def test_既定の今日は_UTC_で決まる(self):
        # TZ を UTC+14 にしても、--today を省いたときの判定日は UTC の日付になる
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'audit-allowlist')
            open(path, 'w', encoding='utf-8').close()
            code = ("import datetime,runpy,sys;sys.argv=['x','--allowlist','/dev/null'];"
                    "g=runpy.run_path(%r);print(g['utc_today']())" % GATE)
            env = dict(os.environ, TZ='Etc/GMT-14')
            got = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env).stdout.strip()
        import datetime
        self.assertEqual(got, datetime.datetime.now(datetime.timezone.utc).date().isoformat())

    def test_理由に_シャープ_を書いてもコメント扱いにしない(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2026-12-31 修正版なし issue #123 参照\n')
        self.assertEqual(rc, 0, out)
        self.assertIn('#123', out)

    def test_ログに出す外部の文字列は改行を無害化する(self):
        report = {'auditReportVersion': 2, 'vulnerabilities': {'x': {'severity': 'high', 'via': [
            {'severity': 'high', 'name': 'x\n::add-mask::secret', 'url': 'https://example.com/a'}]}}}
        rc, out = self.run_gate(report, '')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('\n::add-mask::', out)

    def test_使われていない例外は通知する(self):
        rc, out = self.run_gate(npm_report(), f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)
        self.assertIn('使われていない', out)



class EmbeddedCopyTest(unittest.TestCase):
    """node-ci.yml に埋め込んだ判定スクリプトが scripts/audit-gate.py と同一であること。

    ワークフローと判定の版がずれないよう埋め込んでいるので、片方だけ直すとここで落ちる。
    """
    def test_埋め込みと本体が一致する(self):
        import yaml
        wf = yaml.safe_load(open(os.path.join(HERE, '..', '.github', 'workflows', 'node-ci.yml'), encoding='utf-8'))
        runs = [st['run'] for job in wf['jobs'].values() for st in job.get('steps', [])
                if "<<'AUDIT_GATE_PY'" in st.get('run', '')]
        self.assertEqual(len(runs), 1)
        embedded = runs[0].split("<<'AUDIT_GATE_PY'\n", 1)[1].split('\nAUDIT_GATE_PY\n', 1)[0] + '\n'
        with open(GATE, encoding='utf-8') as f:
            self.assertEqual(embedded, f.read(),
                             'scripts/audit-gate.py を直したら node-ci.yml の埋め込みも同じ内容にしてください')

if __name__ == '__main__':
    unittest.main()
