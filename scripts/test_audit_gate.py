"""audit-gate.py の単体テスト。

ゲートが壊れて常に成功を返しても気付けるよう、「落とすべき入力を確実に落とす」ことを中心に確かめる。
"""
import re
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
ISOLATED = ['-I'] if subprocess.run([sys.executable, '-I', '-c', 'import yaml'], capture_output=True).returncode == 0 else []


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
            with open(path, 'w', encoding='utf-8'):
                pass
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

    def test_同じ_GHSA_が複数パッケージに出たら全部の名前を出す(self):
        url = f'https://github.com/advisories/{BRACES}'
        report = {'auditReportVersion': 2, 'vulnerabilities': {
            'a': {'severity': 'high', 'via': [{'severity': 'high', 'name': 'a', 'url': url}]},
            'b': {'severity': 'high', 'via': [{'severity': 'high', 'name': 'b', 'url': url}]}}}
        rc, out = self.run_gate(report, f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)
        self.assertIn('a, b', out)
        self.assertIn('high以上 2 件', out)

    def test_日付でない_today_はトレースバックでなく使い方のエラー(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'audit-allowlist')
            with open(path, 'w', encoding='utf-8'):
                pass
            p = subprocess.run([sys.executable, GATE, '--allowlist', path, '--today', '2026/10/03'],
                               input='{}', capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn('Traceback', p.stderr)

    def test_使われていない例外は通知する(self):
        rc, out = self.run_gate(npm_report(), f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)
        self.assertIn('使われていない', out)



FIXTURES = os.path.join(HERE, 'fixtures')


class RealOutputTest(unittest.TestCase):
    """実際の npm / pnpm が出した audit の JSON で確かめる（手書きの見本だけに頼らない）。"""
    def run_file(self, name, pm, allowlist):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'audit-allowlist')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(allowlist)
            with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
                p = subprocess.run([sys.executable, GATE, '--pm', pm, '--allowlist', path, '--today', TODAY],
                                   stdin=f, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def test_pnpm_11_の脆弱性0件の出力は通す(self):
        rc, out = self.run_file('pnpm11-audit-empty.json', 'pnpm', '')
        self.assertEqual(rc, 0, out)

    def test_npm_11_の実出力で例外リストが効く(self):
        # next 16.3.8 + eslint-config-next 16.3.8 の lockfile（braces の high が残る）を npm 11.13.0 で audit した結果
        rc, out = self.run_file('npm11-audit-next-eslint.json', 'npm', f'{BRACES} 2026-12-31 修正版なし\n')
        self.assertEqual(rc, 0, out)
        rc, out = self.run_file('npm11-audit-next-eslint.json', 'npm', '')
        self.assertEqual(rc, 1, out)
        self.assertIn(BRACES, out)


class FileAndClockTest(unittest.TestCase):
    def gate(self, allow_bytes, report=None):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'audit-allowlist')
            if allow_bytes is not None:
                with open(path, 'wb') as f:
                    f.write(allow_bytes)
            p = subprocess.run([sys.executable, GATE, '--pm', 'npm', '--allowlist', path, '--today', TODAY],
                               input=json.dumps(report or npm_report(('braces', BRACES, 'high'))),
                               capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def test_BOM_付きの例外リストを読める(self):
        rc, out = self.gate(f'\ufeff{BRACES} 2026-12-31 修正版なし\n'.encode('utf-8'))
        self.assertEqual(rc, 0, out)

    def test_読めない例外リストはトレースバックでなくエラーにする(self):
        for data in (b'\xff\xfe\x00broken', None):
            rc, out = self.gate(data)
            self.assertEqual(rc, 1, out)
            self.assertNotIn('Traceback', out)
            self.assertIn('::error::', out)

    def test_集計の件数に_high_未満の構造エラーや二重計上を混ぜない(self):
        report = {'auditReportVersion': 2, 'vulnerabilities': {
            'x': {'severity': 'high', 'via': ['missing']},
            'm': {'severity': 'moderate', 'via': ['gone']}}}
        rc, out = self.gate(b'', report)
        self.assertEqual(rc, 1, out)
        self.assertIn('high以上 0 件', out)
        self.assertIn('構造エラー 2 件', out)

    def test_既定の今日はローカルでなく_UTC_の日付(self):
        # UTC では 10/3 23:00、ローカル（UTC+10）では 10/4 になる時刻に固定して確かめる
        import datetime as real
        import runpy

        class FakeDateTime(real.datetime):
            @classmethod
            def now(cls, tz=None):
                utc = real.datetime(2026, 10, 3, 23, 0, tzinfo=real.timezone.utc)
                return utc.astimezone(tz) if tz else real.datetime(2026, 10, 4, 9, 0)

        class FakeDate(real.date):
            @classmethod
            def today(cls):
                return real.date(2026, 10, 4)

        g = runpy.run_path(GATE)
        fake = type('M', (), {'datetime': FakeDateTime, 'date': FakeDate, 'timezone': real.timezone})
        g['utc_today'].__globals__['datetime'] = fake
        self.assertEqual(g['utc_today'](), real.date(2026, 10, 3))


class AllowlistPolicyTest(unittest.TestCase):
    """例外リストの使い方の制限（2026-10-03 追加）: 期限は今日から 120 日以内、critical は除外できない。"""
    run_gate = AuditGateTest.run_gate

    def test_期限が120日先までなら通す(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2027-01-31 修正版なし\n')  # 10/3 + 120 日
        self.assertEqual(rc, 0, out)

    def test_期限が121日以上先なら落とす(self):
        rc, out = self.run_gate(npm_report(('braces', BRACES, 'high')), f'{BRACES} 2027-02-01 修正版なし\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('120 日', out)

    def test_使われていない例外でも期限が先すぎれば落とす(self):
        rc, out = self.run_gate(npm_report(), f'{BRACES} 2099-12-31 修正版なし\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('120 日', out)

    def test_critical_は例外リストにあっても落とす_npm(self):
        rc, out = self.run_gate(npm_report(('next', NEXT_RCE, 'critical')), f'{NEXT_RCE} 2026-12-31 様子見\n')
        self.assertEqual(rc, 1, out)
        self.assertIn('critical', out)
        self.assertIn(NEXT_RCE, out)

    def test_critical_は例外リストにあっても落とす_pnpm(self):
        rc, out = self.run_gate(pnpm_report(('next', NEXT_RCE, 'critical')), f'{NEXT_RCE} 2026-12-31 様子見\n', pm='pnpm')
        self.assertEqual(rc, 1, out)
        self.assertIn('critical', out)


class PnpmConfigTest(unittest.TestCase):
    """pnpm の独自の除外設定（auditConfig.ignoreGhsas / ignoreCves）を見つけたら落とす（2026-10-03 追加）。

    例外リストを通さず、理由も期限もなく除外できてしまうため。
    """
    def check(self, files):
        with tempfile.TemporaryDirectory() as d:
            for name, body in files.items():
                with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
                    f.write(body)
            # 本番（node-ci.yml）と同じく -I（隔離モード）で動かす（CI の self-test は system に PyYAML を入れる）。
            # 手元で -I から PyYAML が見えないときだけ -I を外す
            p = subprocess.run([sys.executable, *ISOLATED, GATE, '--pm', 'pnpm', '--config-only', '--project-dir', d],
                               capture_output=True, text=True)
            return p.returncode, p.stdout + p.stderr

    def test_除外設定が無ければ通す(self):
        rc, out = self.check({'package.json': json.dumps({'name': 'x', 'pnpm': {'overrides': {}}})})
        self.assertEqual(rc, 0, out)

    def test_package_json_が無くても通す(self):
        rc, out = self.check({})
        self.assertEqual(rc, 0, out)

    def test_package_json_の_ignoreGhsas_は落とす(self):
        rc, out = self.check({'package.json': json.dumps({'pnpm': {'auditConfig': {'ignoreGhsas': [BRACES]}}})})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreGhsas', out)
        self.assertIn('audit-allowlist', out)

    def test_package_json_の_ignoreCves_は落とす(self):
        rc, out = self.check({'package.json': json.dumps({'pnpm': {'auditConfig': {'ignoreCves': ['CVE-2026-0001']}}})})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreCves', out)

    def test_pnpm_workspace_yaml_の除外設定は落とす(self):
        rc, out = self.check({'package.json': '{}',
                              'pnpm-workspace.yaml': 'packages:\n  - "."\nauditConfig:\n  ignoreGhsas:\n    - GHSA-vfj7-8cjw-p6xm\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('auditConfig.ignoreGhsas', out)  # PyYAML が無いときの別の理由の失敗で通らないよう、検出したキー名まで見る

    def test_pnpm_workspace_yaml_のフロー形式も落とす(self):
        rc, out = self.check({'pnpm-workspace.yaml': 'packages: ["."]\nauditConfig: {ignoreGhsas: [GHSA-vfj7-8cjw-p6xm]}\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreGhsas', out)

    def test_pnpm_workspace_yaml_の引用符つきキーも落とす(self):
        rc, out = self.check({'pnpm-workspace.yaml': 'auditConfig:\n  "ignoreCves":\n    - CVE-2026-0001\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreCves', out)

    def test_pnpm_workspace_yaml_の空の除外設定は通す(self):
        rc, out = self.check({'pnpm-workspace.yaml': 'auditConfig:\n  ignoreGhsas: []\n'})
        self.assertEqual(rc, 0, out)

    def test_pnpm_workspace_yaml_の環境変数を展開するキーは落とす(self):
        # pnpm 11 は最上位のキーの ${VAR} を展開する。展開後に auditConfig になる書き方ですり抜けさせない
        rc, out = self.check({'pnpm-workspace.yaml': '"${AUDIT_KEY}":\n  ignoreGhsas:\n    - GHSA-vfj7-8cjw-p6xm\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('${', out)

    def test_pnpm_workspace_yaml_が壊れていたら落とす(self):
        rc, out = self.check({'pnpm-workspace.yaml': 'auditConfig: {ignoreGhsas: [\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('YAML として読めません', out)

    def test_pnpm_workspace_yaml_の重複キーは落とす(self):
        # PyYAML は重複キーを後の値で黙って上書きする。前の値に除外設定を書いて検査をすり抜けさせない
        rc, out = self.check({'pnpm-workspace.yaml': 'auditConfig: {ignoreGhsas: [GHSA-vfj7-8cjw-p6xm]}\nauditConfig: {}\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreGhsas', out)

    def test_pnpm_workspace_yaml_の2段の継承は通す(self):
        # どの値が勝つかを計算しない。正しい 2 段のマージで落とさない
        rc, out = self.check({'pnpm-workspace.yaml': 'x-base: &base {sharedWorkspaceLockfile: true}\n'
                                                     'x-overrides: &overrides {<<: *base, sharedWorkspaceLockfile: false}\n'
                                                     '<<: *overrides\n'})
        self.assertEqual(rc, 0, out)

    def test_pnpm_workspace_yaml_のマージキーを2回書いた除外設定も落とす(self):
        # PyYAML と pnpm で勝つ値が食い違っても、書かれている除外設定はすべて見る
        rc, out = self.check({'pnpm-workspace.yaml': 'x-a: &a {auditConfig: {ignoreGhsas: [GHSA-vfj7-8cjw-p6xm]}}\n'
                                                     'x-b: &b {auditConfig: {}}\n'
                                                     '<<: *a\n<<: *b\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreGhsas', out)

    def test_pnpm_workspace_yaml_のマージキーは通す(self):
        # pnpm が読める正しい設定（<<: *anchor）を、重複キーの検査で落とさない
        rc, out = self.check({'pnpm-workspace.yaml': 'x-common: &common\n  react: ^19\ncatalog:\n  <<: *common\n  vue: ^3\n'})
        self.assertEqual(rc, 0, out)

    def test_pnpm_workspace_yaml_のマージで持ち込んだ除外設定は落とす(self):
        rc, out = self.check({'pnpm-workspace.yaml': 'x-base: &base\n  auditConfig:\n    ignoreGhsas: [GHSA-vfj7-8cjw-p6xm]\n<<: *base\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignoreGhsas', out)

    def test_pnpm_workspace_yaml_の_on_と_yes_は別のキーとして通す(self):
        # PyYAML はどちらも真偽値 True に変えるが、pnpm は文字列として別々に読む
        rc, out = self.check({'pnpm-workspace.yaml': 'catalog:\n  on: 1.0.0\n  yes: 2.0.0\n'})
        self.assertEqual(rc, 0, out)

    def test_auditConfig_の中の環境変数を展開するキーも落とす(self):
        rc, out = self.check({'pnpm-workspace.yaml': 'auditConfig:\n  "${K}":\n    - GHSA-vfj7-8cjw-p6xm\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('${', out)

    def test_pnpm_workspace_yaml_の綴りを変えたキーも落とす(self):
        # 大文字小文字・ハイフン・下線の違いですり抜けさせない
        rc, out = self.check({'pnpm-workspace.yaml': 'audit-config:\n  ignore-ghsas:\n    - GHSA-vfj7-8cjw-p6xm\n'})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignore-ghsas', out)

    def test_package_json_の綴りを変えたキーも落とす(self):
        rc, out = self.check({'package.json': json.dumps({'pnpm': {'AuditConfig': {'ignore_cves': ['CVE-2026-0001']}}})})
        self.assertEqual(rc, 1, out)
        self.assertIn('ignore_cves', out)

    def test_package_json_が壊れていたら落とす(self):
        rc, out = self.check({'package.json': '{ broken'})
        self.assertEqual(rc, 1, out)


class EmbeddedCopyTest(unittest.TestCase):
    """node-ci.yml に埋め込んだ判定スクリプトが scripts/audit-gate.py と同一であること。

    ワークフローと判定の版がずれないよう埋め込んでいるので、片方だけ直すとここで落ちる。
    """
    def test_埋め込みと本体が一致する(self):
        import yaml
        with open(os.path.join(HERE, '..', '.github', 'workflows', 'node-ci.yml'), encoding='utf-8') as f:
            wf = yaml.safe_load(f)
        runs = [st['run'] for job in wf['jobs'].values() for st in job.get('steps', [])
                if "<<'AUDIT_GATE_PY'" in st.get('run', '')]
        self.assertEqual(len(runs), 1)
        embedded = runs[0].split("<<'AUDIT_GATE_PY'\n", 1)[1].split('\nAUDIT_GATE_PY\n', 1)[0] + '\n'
        with open(GATE, encoding='utf-8') as f:
            self.assertEqual(embedded, f.read(),
                             'scripts/audit-gate.py を直したら node-ci.yml の埋め込みも同じ内容にしてください')

class WorkflowExpressionTest(unittest.TestCase):
    """ワークフローの中の ${{ … }} が、GitHub Actions の式として読める形だけであること（2026-10-03 追加）。

    run: の中の文字列（heredoc の中も含む）でも ${{ は式として評価される。読めない式が 1 つあると
    ワークフロー全体が起動しなくなり、配布先のすべてのリポジトリで CI が止まる。
    """
    EXPR = re.compile(r"\$\{\{(.*?)\}\}")
    ALLOWED = re.compile(r"^[\x20-\x7e]*[^\s][\x20-\x7e]*$")  # ASCII の印字可能文字だけで、空でない

    def test_式として読めない_dollar_brace_brace_が無い(self):
        wf_dir = os.path.join(HERE, '..', '.github', 'workflows')
        for name in sorted(os.listdir(wf_dir)):
            if not name.endswith(('.yml', '.yaml')):
                continue
            with open(os.path.join(wf_dir, name), encoding='utf-8') as f:
                for no, line in enumerate(f, 1):
                    # run: の中のシェルのコメントも Actions は評価するので、# で始まる行も飛ばさない
                    if '${{' not in line:
                        continue
                    exprs = self.EXPR.findall(line)
                    self.assertEqual(line.count('${{'), len(exprs), f'{name}:{no} 閉じていない ${{{{: {line.strip()}')
                    for e in exprs:
                        self.assertRegex(e, self.ALLOWED, f'{name}:{no} 式として読めない: {line.strip()}')

    def test_埋め込みの判定スクリプトに_dollar_brace_brace_が無い(self):
        with open(GATE, encoding='utf-8') as f:
            self.assertNotIn('${{', f.read(), 'node-ci.yml に埋め込むと Actions の式として評価される')


if __name__ == '__main__':
    unittest.main()
