import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check.py'


class CheckTests(unittest.TestCase):
    def check(self, files, options=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--json', *options, str(root)],
                capture_output=True, text=True, check=False,
            )
            for name, content in files.items():
                self.assertEqual((root / name).read_text(), content)
            self.assertTrue(result.stdout, result.stderr)
            return result.returncode, json.loads(result.stdout)

    def test_reports_depth_with_location_and_measurement(self):
        status, report = self.check({'sample.py': 'def f(a):\n    if a:\n        if a:\n            if a:\n                if a:\n                    return 1\n'})
        self.assertEqual(status, 1)
        issue = next(item for item in report['issues'] if item['rule'] == 'max-depth')
        self.assertEqual(issue['line'], 5)
        self.assertEqual(issue['actual'], 4)
        self.assertEqual(issue['limit'], 3)

    def test_comments_and_strings_are_not_control_flow(self):
        status, report = self.check({'sample.py': 'def f():\n    # if if if if\n    text = "if a: if b: if c: if d:"\n    return text\n'})
        self.assertEqual(status, 0)
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['functions'][0]['complexity'], 1)

    def test_nested_ternary_and_parameters(self):
        status, report = self.check({'sample.py': 'def f(a,b,c,d,e):\n    return (b if c else d) if a else e\n'})
        self.assertEqual(status, 1)
        self.assertEqual({item['rule'] for item in report['issues']}, {'max-params', 'no-nested-ternary'})

    def test_line_count_excludes_comments_and_blanks(self):
        comments = '    # comment\n\n' * 80
        status, report = self.check({'sample.py': f'def f():\n{comments}    return 1\n'})
        self.assertEqual(status, 0)
        self.assertEqual(report['functions'][0]['lines'], 2)
        body = '\n'.join(f'    print({i})' for i in range(50))
        status, report = self.check({'sample.py': f'def f():\n{body}\n'})
        self.assertEqual(status, 1)
        self.assertTrue(any(item['rule'] == 'max-lines' for item in report['issues']))

    def test_complexity_counts_boolean_operators_and_branches(self):
        branches = '\n'.join(f'    if a == {i}: return {i}' for i in range(9))
        status, report = self.check({'sample.py': f'def f(a,b,c):\n{branches}\n    return a and b and c\n'})
        self.assertEqual(status, 1)
        self.assertEqual(report['functions'][0]['complexity'], 12)

    def test_elif_and_nested_functions_do_not_inflate_depth(self):
        status, report = self.check({'sample.py': 'def f(a):\n    if a:\n        def inner(b):\n            if b:\n                return 1\n        return inner(a)\n    elif a is None:\n        return 2\n    elif not a:\n        return 3\n'})
        self.assertEqual(status, 0)
        metrics = {item['name']: item for item in report['functions']}
        self.assertEqual(metrics['f']['depth'], 1)
        self.assertEqual(metrics['inner']['depth'], 1)
        self.assertEqual(metrics['f']['complexity'], 4)

    def test_syntax_errors_do_not_pass(self):
        for source in ['def broken( {', 'return 1\n']:
            status, report = self.check({'sample.py': source})
            self.assertEqual(status, 2)
            self.assertEqual(report['filesChecked'], 0)
            self.assertTrue(report['errors'])

    def test_empty_or_unsupported_targets_do_not_pass(self):
        for files in [{}, {'sample.ts': 'const value = 1;'}]:
            status, report = self.check(files)
            self.assertEqual(status, 2)
            self.assertEqual(report['filesChecked'], 0)
            self.assertTrue(report['errors'])

    def test_ignored_directories_and_symlinks_are_declared(self):
        status, report = self.check({'sample.py': 'value = 1\n', '.venv/generated.py': 'invalid code('})
        self.assertEqual(status, 0)
        self.assertEqual(report['filesChecked'], 1)
        self.assertTrue(any(item['path'].endswith('.venv') for item in report['skipped']))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'link.py'
            path.symlink_to(SCRIPT)
            result = subprocess.run([sys.executable, str(SCRIPT), '--json', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(json.loads(result.stdout)['skipped'])

    def test_configurable_threshold_and_exact_boundary(self):
        status, report = self.check({'sample.py': 'def f(a,b,c,d,e):\n    return a\n'}, ['--max-params', '5'])
        self.assertEqual(status, 0)
        self.assertEqual(report['limits']['max-params'], 5)
        status, _ = self.check({'sample.py': 'def f(a,b,c,d,e,f):\n    return a\n'}, ['--max-params', '5'])
        self.assertEqual(status, 1)

    def test_report_is_repeatable_and_deduplicates_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.py'
            path.write_text('def f(a,b,c,d,e):\n    return a\n')
            args = [sys.executable, str(SCRIPT), '--json', directory, str(path)]
            first = subprocess.run(args, capture_output=True, text=True)
            second = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(first.returncode, 1)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(json.loads(first.stdout)['filesChecked'], 1)

    def test_file_encoding_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.py'
            path.write_bytes('# coding: cp932\nvalue = "日本語"\n'.encode('cp932'))
            result = subprocess.run([sys.executable, str(SCRIPT), '--json', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_async_comprehensions_and_exception_handlers(self):
        source = 'async def f(items):\n    try:\n        return [x for x in items if x and x > 0]\n    except ValueError:\n        return []\n'
        status, report = self.check({'sample.py': source})
        self.assertEqual(status, 0)
        self.assertEqual(report['functions'][0]['complexity'], 5)
        self.assertEqual(report['functions'][0]['depth'], 1)

    def test_else_if_is_nested_but_elif_is_not(self):
        source = 'def f(a):\n    if a:\n        return 1\n    else:\n        if a is None:\n            return 2\n'
        status, report = self.check({'sample.py': source})
        self.assertEqual(status, 0)
        self.assertEqual(report['functions'][0]['depth'], 2)

    @unittest.skipIf(sys.version_info < (3, 10), 'match requires Python 3.10')
    def test_match_cases_and_guards(self):
        source = 'def f(a):\n    match a:\n        case 1:\n            return 1\n        case x if x > 2:\n            return 2\n        case _:\n            return 0\n'
        status, report = self.check({'sample.py': source})
        self.assertEqual(status, 0)
        self.assertEqual(report['functions'][0]['complexity'], 4)
        self.assertEqual(report['functions'][0]['depth'], 1)

    def test_docstrings_and_multiline_strings_count_as_code(self):
        source = 'def f():\n    """Description\n\n    with blank line.\n    """\n    return 1\n'
        status, report = self.check({'sample.py': source})
        self.assertEqual(status, 0)
        self.assertEqual(report['functions'][0]['lines'], 6)

    def test_target_code_is_not_executed(self):
        status, report = self.check({'sample.py': 'raise RuntimeError("do not execute")\n'})
        self.assertEqual(status, 0)
        self.assertEqual(report['filesChecked'], 1)

    def test_valid_file_does_not_hide_an_invalid_file(self):
        status, report = self.check({'valid.py': 'value = 1\n', 'invalid.py': 'def broken( {\n'})
        self.assertEqual(status, 2)
        self.assertEqual(report['filesChecked'], 1)
        self.assertEqual(len(report['errors']), 1)


if __name__ == '__main__':
    unittest.main()
