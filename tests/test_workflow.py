import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
RUFF = os.environ.get('YOMITOKI_RUFF')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'sample.py'

    def run_script(self, name, *args):
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPTS / name), '--json', *map(str, args)],
            capture_output=True, encoding='utf-8', check=False,
        )
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def scan(self, source, *options):
        self.source.write_text(source)
        return self.run_script('check.py', *options, self.source)[1]

    def run_text(self, name, *args):
        return subprocess.run(
            [sys.executable, '-B', str(SCRIPTS / name), *map(str, args)],
            capture_output=True, encoding='utf-8', check=False,
        )

    def write_accepted(self, *entries):
        path = self.root / 'accepted.json'
        path.write_text(json.dumps(list(entries)))
        return path

    def compare(self, before, after):
        paths = [self.root / 'before.json', self.root / 'after.json']
        for path, report in zip(paths, [before, after]):
            path.write_text(json.dumps(report))
        return self.run_script('compare_reports.py', *paths)

    def test_reports_and_accepted_lists_saved_as_utf16_or_with_bom_are_read(self):
        report = self.scan('def f(a,b,c,d,e):\n    return a\n')
        paths = [self.root / 'before.json', self.root / 'after.json']
        for encoding in ['utf-16', 'utf-8-sig']:
            for path in paths:
                path.write_bytes(json.dumps(report, ensure_ascii=False).encode(encoding))
            status, result = self.run_script('compare_reports.py', *paths)
            self.assertEqual(status, 0, (encoding, result['errors']))
            accepted = self.write_accepted({'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'actual': 5, 'reason': '理由'})
            accepted.write_bytes(accepted.read_bytes().decode('utf-8').encode(encoding))
            self.assertEqual(self.run_script('check.py', '--accepted', accepted, self.source)[0], 0, encoding)

    def test_comparison_reports_measured_reduction(self):
        before = self.scan('def f(a,b,c,d,e):\n    return a\n')
        after = self.scan('def f(a,b,c,d):\n    return a\n')
        status, result = self.compare(before, after)
        self.assertEqual(status, 0)
        self.assertTrue(result['comparable'])
        self.assertEqual(result['counts']['before'], 1)
        self.assertEqual(result['counts']['after'], 0)
        self.assertEqual(result['ruleChanges'][0]['delta'], -1)
        self.assertEqual(result['metricChanges'][0]['params']['delta'], -1)

    def test_comparison_distinguishes_notices_from_failures(self):
        before = self.scan('def f(value):\n    return value\n')
        documented = self.scan('def f(value):\n    """Return the supplied value."""\n    return value\n')
        broken = self.scan('def broken( {')
        changed_limits = dict(before, limits=dict(before['limits'], **{'max-lines': 60}))
        for after, code, status in [
            (before, 0, 'no-increase'),
            (documented, 1, 'increase-notice'),
            (broken, 2, 'error'),
            (changed_limits, 2, 'not-comparable'),
        ]:
            with self.subTest(status=status):
                actual_code, report = self.compare(before, after)
                self.assertEqual(actual_code, code)
                self.assertEqual(report['status'], status)
                self.assertTrue(report['message'])
                output = self.run_text('compare_reports.py', self.root / 'before.json', self.root / 'after.json')
                self.assertEqual(output.returncode, code)
                self.assertIn(report['message'], output.stdout)
        self.assertEqual(documented['issues'], [])

    def test_line_shift_does_not_create_new_findings(self):
        source = 'def f(a,b,c,d,e):\n    return a\n'
        before = self.scan(source)
        after = self.scan('\n\n' + source)
        status, result = self.compare(before, after)
        self.assertEqual(status, 0)
        self.assertEqual(result['ruleChanges'], [])

    def test_a_new_rule_cannot_be_hidden_by_a_lower_total(self):
        before = self.scan('def f(a,b,c,d,e):\n    return a\ndef g(a,b,c,d,e):\n    return b\n')
        after = self.scan('def f(a,b,c):\n    return (b if c else a) if a else c\n')
        status, result = self.compare(before, after)
        self.assertEqual(status, 1)
        self.assertTrue(any(item['delta'] > 0 for item in result['ruleChanges']))

    def test_threshold_changes_are_not_reported_as_improvement(self):
        source = 'def f(a,b,c,d,e):\n    return a\n'
        before = self.scan(source)
        after = self.scan(source, '--max-params', '5')
        status, result = self.compare(before, after)
        self.assertEqual(status, 2)
        self.assertFalse(result['comparable'])
        self.assertIn('limits', result['conditionChanges'])
        self.assertEqual(result['conditionDetails']['limits']['after']['max-params'], 5)

    def test_removed_files_are_flagged_even_when_counts_fall(self):
        extra = self.root / 'extra.py'
        extra.write_text('def f(a,b,c,d,e):\n    return a\n')
        self.source.write_text('value = 1\n')
        before = self.run_script('check.py', self.root)[1]
        extra.unlink()
        after = self.run_script('check.py', self.root)[1]
        status, result = self.compare(before, after)
        self.assertEqual(status, 2)
        self.assertEqual(result['removedFiles'], [str(extra.resolve())])

    def test_python_or_checker_changes_are_detected(self):
        before = self.scan('value = 1\n')
        for key, value in [('pythonVersion', '0.0.0'), ('checkerSha256', '0' * 64), ('parsers', {'tree-sitter': '0.0.0'})]:
            after = dict(before, **{key: value})
            status, result = self.compare(before, after)
            self.assertEqual(status, 2)
            self.assertIn(key, result['conditionChanges'])

    def test_invalid_and_incomplete_reports_fail(self):
        before = self.scan('value = 1\n')
        candidates = [{}, dict(before, filesChecked=0), dict(before, issues='invalid')]
        candidates.append(self.scan('def broken( {'))
        for after in candidates:
            status, result = self.compare(before, after)
            self.assertEqual(status, 2)
            self.assertTrue(result['errors'])

    def test_missing_ruff_is_an_error_but_ast_results_survive(self):
        self.source.write_text('def f(a,b,c,d,e):\n    return a\n')
        status, report = self.run_script('run_checks.py', '--ruff', self.root / 'missing', self.source)
        self.assertEqual(status, 2)
        self.assertEqual(report['filesChecked'], 1)
        self.assertTrue(any(item['rule'] == 'max-params' for item in report['issues']))
        self.assertTrue(report['errors'])

    def test_malformed_json_and_missing_reports_fail_cleanly(self):
        path = self.root / 'bad.json'
        path.write_text('{broken')
        for other in [path, self.root / 'missing.json']:
            status, result = self.run_script('compare_reports.py', path, other)
            self.assertEqual(status, 2)
            self.assertTrue(result['errors'])
            self.assertEqual(result['status'], 'error')
            self.assertTrue(result['message'])

    def test_suite_exposes_scope_even_when_ruff_is_missing(self):
        self.source.write_text('value = 1\n')
        options = ['--ruff', self.root / 'missing', self.source]
        status, report = self.run_script('run_checks.py', *options)
        self.assertEqual(status, 2)
        self.assertEqual(report['scope']['ruffRules'], ['F401', 'F841', 'SIM', 'RET'])
        self.assertEqual(report['scope']['notRun'], ['project-lint', 'type-check', 'tests', 'build'])
        output = self.run_text('run_checks.py', *options).stdout
        self.assertIn('付属検査の範囲:', output)
        self.assertIn('プロジェクトの検査: 未実行', output)

    def test_ruff_is_not_required_without_python_files(self):
        source = self.root / 'sample.c'
        source.write_text('int f(int a) { return a; }\n')
        status, report = self.run_script('run_checks.py', '--ruff', self.root / 'missing', source)
        if importlib.util.find_spec('tree_sitter') is None:
            self.assertEqual(status, 2)
            return
        self.assertEqual(status, 0, report['errors'])
        self.assertEqual(report['ruff']['filesChecked'], 0)
        self.assertEqual(self.compare(report, report)[0], 0)

    def test_bundled_ruff_is_found_in_the_platform_virtual_environment(self):
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        spec = importlib.util.spec_from_file_location('run_checks', SCRIPTS / 'run_checks.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.__file__ = str(self.root / 'scripts' / 'run_checks.py')
        bundled = self.root / '.venv' / ('Scripts/ruff.exe' if os.name == 'nt' else 'bin/ruff')
        bundled.parent.mkdir(parents=True)
        bundled.write_text('')
        self.assertEqual(module.find_ruff(None), str(bundled.resolve()))
        self.assertEqual(module.find_ruff('explicit-ruff'), 'explicit-ruff')

    def test_ruff_failure_and_invalid_output_are_errors(self):
        fake = self.root / 'fake-ruff'
        self.source.write_text('value = 1\n')
        for body in ['sys.exit(2)', 'print("not json")', 'print("{}")', 'print("[{}]"); sys.exit(1)']:
            fake.write_text(f'#!{sys.executable}\nimport sys\nif "--version" in sys.argv:\n    print("ruff test")\n    sys.exit(0)\n{body}\n')
            fake.chmod(0o755)
            status, result = self.run_script('run_checks.py', '--ruff', fake, self.source)
            self.assertEqual(status, 2)
            self.assertTrue(result['errors'])
            self.assertEqual(result['ruff']['filesChecked'], 0)

    def test_same_rule_moving_to_another_file_is_visible(self):
        extra = self.root / 'extra.py'
        extra.write_text('value = 1\n')
        self.source.write_text('def f(a,b,c,d,e):\n    return a\n')
        before = self.run_script('check.py', self.root)[1]
        extra.write_text(self.source.read_text())
        self.source.write_text('value = 1\n')
        after = self.run_script('check.py', self.root)[1]
        status, result = self.compare(before, after)
        self.assertEqual(status, 1)
        self.assertEqual(result['counts']['delta'], 0)
        self.assertEqual(len(result['ruleChanges']), 2)

    def test_metric_increase_below_threshold_is_visible(self):
        before = self.scan('def f(a):\n    return a\n')
        after = self.scan('def f(a,b):\n    return a + b\n')
        status, result = self.compare(before, after)
        self.assertEqual(status, 1)
        self.assertEqual(result['counts']['delta'], 0)
        self.assertEqual(result['metricChanges'][0]['params']['delta'], 1)

    def test_accepted_findings_are_listed_with_reasons_but_not_counted(self):
        self.source.write_text('def f(a,b,c,d,e):\n    return a\ndef g(a,b,c,d,e):\n    return b\n')
        reason = 'キーワード専用引数で呼び出し側から名前が見える'
        accepted = self.write_accepted(
            {'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'actual': 5, 'reason': reason},
            {'path': 'sample.py', 'function': 'removed', 'rule': 'max-depth', 'actual': 4, 'reason': '削除済み'},
        )
        status, report = self.run_script('check.py', '--accepted', accepted, self.source)
        self.assertEqual(status, 1)
        self.assertEqual([item['function'] for item in report['issues']], ['g'])
        self.assertEqual([(item['function'], item['reason']) for item in report['accepted']], [('f', reason)])
        self.assertEqual([item['function'] for item in report['unusedAccepted']], ['removed'])
        output = self.run_text('check.py', '--accepted', accepted, self.source).stdout
        self.assertIn('許容済み: 1件', output)
        self.assertIn(reason, output)
        self.assertIn('不要になった許容', output)
        accepted.write_text(json.dumps([{'path': 'sample.py', 'function': name, 'rule': 'max-params', 'actual': 5, 'reason': reason} for name in 'fg']))
        self.assertEqual(self.run_script('check.py', '--accepted', accepted, self.source)[0], 0)

    def test_findings_worse_than_when_accepted_are_reported_again(self):
        accepted = self.write_accepted({'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'actual': 5, 'reason': '理由'})
        self.source.write_text('def f(a,b,c,d,e,f,g,h):\n    return a\n')
        status, report = self.run_script('check.py', '--accepted', accepted, self.source)
        self.assertEqual(status, 1)
        self.assertEqual(report['accepted'], [])
        self.assertEqual(report['unusedAccepted'], [])
        self.assertEqual((report['issues'][0]['actual'], report['issues'][0]['acceptedActual']), (8, 5))
        self.assertIn('許容時より悪化', self.run_text('check.py', '--accepted', accepted, self.source).stdout)

    def test_accepted_entries_need_a_reason_and_an_ast_rule(self):
        self.source.write_text('import os\n')
        for entry in [
            {'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'actual': 5, 'reason': ' '},
            {'path': 'sample.py', 'function': None, 'rule': 'F401', 'actual': 1, 'reason': '理由'},
            {'path': 'sample.py', 'function': None, 'rule': 'format-required', 'actual': 1, 'reason': '理由'},
            {'path': 'sample.py', 'rule': 'max-params', 'actual': 5, 'reason': '理由'},
            {'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'reason': '理由'},
            {'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'actual': '5', 'reason': '理由'},
        ]:
            with self.subTest(entry=entry):
                result = self.run_text('run_checks.py', '--json', '--accepted', self.write_accepted(entry), self.source)
                self.assertEqual(result.returncode, 2)
                self.assertIn('許容リスト', result.stderr)

    def test_accepted_paths_take_either_separator_and_no_absolute_path(self):
        source = self.root / 'sub' / 'sample.py'
        source.parent.mkdir()
        source.write_text('def f(a,b,c,d,e):\n    return a\n')
        entry = {'function': 'f', 'rule': 'max-params', 'actual': 5, 'reason': '理由'}
        for path in ['sub/sample.py', 'sub\\sample.py']:
            status, report = self.run_script('check.py', '--accepted', self.write_accepted(dict(entry, path=path)), source)
            self.assertEqual((status, len(report['accepted'])), (0, 1), path)
        for path in ['/sub/sample.py', '\\sub\\sample.py', 'C:\\sub\\sample.py', 'C:sub/sample.py']:
            result = self.run_text('check.py', '--accepted', self.write_accepted(dict(entry, path=path)), source)
            self.assertEqual(result.returncode, 2, path)
            self.assertIn('許容リスト', result.stderr)

    def test_accepting_a_finding_is_not_reported_as_a_reduction(self):
        source = 'def f(a,b,c,d,e):\n    return a\n'
        before = self.scan(source)
        accepted = self.write_accepted({'path': 'sample.py', 'function': 'f', 'rule': 'max-params', 'actual': 5, 'reason': '理由'})
        after = self.scan(source, '--accepted', accepted)
        self.assertEqual(after['exitCode'], 0)
        status, result = self.compare(before, after)
        self.assertEqual(status, 0)
        self.assertTrue(result['comparable'])
        self.assertEqual(result['ruleChanges'], [])
        self.assertEqual(result['counts']['delta'], 0)
        self.assertEqual(result['acceptedCounts']['delta'], 1)
        original = after['accepted'][0]
        for item in [dict(original, reason=''), dict(original, tool='ruff', rule='F401'), dict(original, acceptedActual=4)]:
            status, result = self.compare(before, dict(after, accepted=[item]))
            self.assertEqual(status, 2)
            self.assertTrue(result['errors'])

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_displayed_format_command_resolves_format_findings(self):
        self.source.write_text("x = {'a':1}\n")
        options = ['--ruff', RUFF, '--quote-style', 'double', '--line-length', '100', self.source]
        status, before = self.run_script('run_checks.py', *options)
        self.assertEqual(status, 1)
        prefix = '同じ設定で整形するには: '
        output = self.run_text('run_checks.py', *options).stdout
        command = next(line[len(prefix):] for line in output.splitlines() if line.startswith(prefix))
        for option in ['--isolated', '--line-length', '--target-version', 'format.quote-style']:
            self.assertIn(option, command)
        subprocess.run(shlex.split(command), capture_output=True, check=True)
        status, after = self.run_script('run_checks.py', *options)
        self.assertEqual(status, 0, after['issues'])
        self.assertEqual(self.source.read_text(), 'x = {"a": 1}\n')
        status, result = self.compare(before, after)
        self.assertEqual(status, 0)
        self.assertTrue(result['comparable'])

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_ruff_output_environment_cannot_redirect_or_write_results(self):
        self.source.write_text('import os\n')
        output = self.root / 'must-not-exist'
        environment = dict(os.environ, RUFF_OUTPUT_FILE=str(output), RUFF_OUTPUT_FORMAT='concise')
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPTS / 'run_checks.py'), '--json', '--ruff', RUFF, str(self.source)],
            capture_output=True, encoding='utf-8', env=environment,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(output.exists())
        self.assertTrue(any(item['rule'] == 'F401' for item in json.loads(result.stdout)['issues']))

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_real_ruff_catches_unused_code_simplification_and_formatting(self):
        source = 'import os\ndef f(a):\n    unused = 1\n    if a:\n        return True\n    else:\n        return False\n'
        self.source.write_text(source)
        status, report = self.run_script('run_checks.py', '--ruff', RUFF, self.source)
        self.assertEqual(status, 1)
        rules = {item['rule'] for item in report['issues']}
        self.assertTrue({'F401', 'F841', 'SIM103', 'format-required'} <= rules, rules)
        self.assertEqual(self.source.read_text(), source)
        self.assertEqual(report['ruff']['filesChecked'], 1)
        self.assertTrue(report['ruff']['version'].startswith('ruff '))

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_ruff_reads_crlf_and_utf8_sources_as_written(self):
        for source in [b'value = 1\r\n', 'value = "日本語"\n'.encode('utf-8')]:
            self.source.write_bytes(source)
            status, report = self.run_script('run_checks.py', '--ruff', RUFF, self.source)
            self.assertEqual(status, 0, (source, report['issues'], report['errors']))

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_suite_pass_does_not_imply_project_checks_passed(self):
        self.source.write_text('value = 1\n')
        options = ['--ruff', RUFF, self.source]
        status, report = self.run_script('run_checks.py', *options)
        self.assertEqual(status, 0)
        self.assertIn('project-lint', report['scope']['notRun'])
        self.assertIn('type-check', report['scope']['notRun'])
        output = self.run_text('run_checks.py', *options).stdout
        self.assertIn('付属検査の範囲:', output)
        self.assertIn('プロジェクトの検査: 未実行', output)

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_project_fix_settings_cannot_mutate_files_or_suppress_checks(self):
        (self.root / 'ruff.toml').write_text('fix = true\n[lint]\nignore = ["ALL"]\n')
        self.source.write_text('import os  # noqa: F401\n')
        status, report = self.run_script('run_checks.py', '--ruff', RUFF, self.source)
        self.assertEqual(status, 1)
        self.assertTrue(any(item['rule'] == 'F401' for item in report['issues']))
        self.assertEqual(self.source.read_text(), 'import os  # noqa: F401\n')

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_suite_comparison_checks_ruff_version_and_profile(self):
        self.source.write_text('value = 1\n')
        status, before = self.run_script('run_checks.py', '--ruff', RUFF, self.source)
        self.assertEqual(status, 0)
        status, result = self.compare(before, before)
        self.assertEqual(status, 0)
        self.assertTrue(result['comparable'])
        for key, value in [('version', 'ruff other'), ('profile', {})]:
            after = dict(before, ruff=dict(before['ruff'], **{key: value}))
            status, result = self.compare(before, after)
            self.assertEqual(status, 2)
            self.assertFalse(result['comparable'])

    @unittest.skipUnless(RUFF, 'Set YOMITOKI_RUFF for real Ruff integration tests')
    def test_suite_results_are_repeatable(self):
        self.source.write_text('import os\n')
        first = self.run_script('run_checks.py', '--ruff', RUFF, self.source)
        second = self.run_script('run_checks.py', '--ruff', RUFF, self.source)
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
