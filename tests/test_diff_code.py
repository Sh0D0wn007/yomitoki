import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'diff_code.py'


class CodeDiffTests(unittest.TestCase):
    def compare(self, before, after):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('before.py', 'after.py')]
            for path, source in zip(paths, (before, after)):
                path.write_bytes(source.encode('utf-8'))
            result = subprocess.run(
                [sys.executable, '-B', str(SCRIPT), '--json', *map(str, paths)],
                capture_output=True, text=True,
            )
            repeated = subprocess.run(result.args, capture_output=True, text=True)
            self.assertEqual(result.stdout, repeated.stdout)
            self.assertEqual(result.returncode, repeated.returncode)
            self.assertEqual([p.read_bytes() for p in paths],
                             [s.encode('utf-8') for s in (before, after)])
            self.assertTrue(result.stdout, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(result.returncode, report['exitCode'])
            self.assertTrue(report['limitations'])
            self.assertTrue(all(isinstance(item, str) and item for item in report['limitations']))
            return report

    def test_limitations_are_visible_even_when_ast_is_unchanged(self):
        report = self.compare('value = 1\n', 'value = 1\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'same.py'
            path.write_text('value = 1\n')
            result = subprocess.run([sys.executable, '-B', str(SCRIPT), str(path), str(path)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        for limitation in report['limitations']:
            self.assertIn(limitation, result.stdout)

    def test_format_comments_and_type_ignore_positions(self):
        report = self.compare('x=1 # type: ignore\n', '\n# return danger()\nx = 1 # type: ignore\n')
        self.assertTrue(report['textChanged'])
        self.assertFalse(report['astChanged'])
        self.assertEqual(report['exitCode'], 0)

    def test_contract_changes(self):
        report = self.compare('def f(x=1):\n return x\n', 'async def f(x=2):\n return x\n')
        self.assertIn('definitions', [c['category'] for c in report['changes']])
        self.assertEqual(report['exitCode'], 1)

    def test_conditions_and_cleanup(self):
        report = self.compare('try:\n if x >= 0: work()\nfinally:\n close()\n',
                              'if x > 0: work()\n')
        categories = [c['category'] for c in report['changes']]
        self.assertIn('control', categories)
        self.assertIn('effects', categories)

    def test_same_calls_reordered(self):
        report = self.compare('first()\nsecond()\n', 'second()\nfirst()\n')
        effects = next(c for c in report['changes'] if c['category'] == 'effects')
        self.assertTrue(effects['orderChanged'])
        self.assertEqual(effects['added'], [])
        self.assertEqual(effects['removed'], [])

    def test_changed_mapping_and_local_name(self):
        for before, after in [('x={"a":1,"b":2}\n', 'x={"a":2,"b":1}\n'),
                              ('x=1\n', 'result=1\n'),
                              ('def f():\n return 1\n', 'def f():\n yield 1\n')]:
            with self.subTest(before=before):
                self.assertTrue(self.compare(before, after)['astChanged'])

    def test_unclassified_expression_is_still_detected(self):
        report = self.compare('x\n', 'y\n')
        self.assertTrue(report['astChanged'])
        self.assertEqual(report['changes'], [])
        self.assertEqual(report['exitCode'], 1)

    def test_decorators_annotations_and_class_bases(self):
        for before, after in [('@old\ndef f(x: int): pass\n', '@new\ndef f(x: str): pass\n'),
                              ('class C(A): pass\n', 'class C(B): pass\n')]:
            with self.subTest(before=before):
                report = self.compare(before, after)
                self.assertIn('definitions', [c['category'] for c in report['changes']])

    def test_encoding_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'latin.py'
            raw = '# coding: latin-1\nx="é"\n'.encode('latin-1')
            source.write_bytes(raw)
            result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--json',
                                     str(source), str(source)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(source.read_bytes(), raw)

    def test_source_values_are_not_emitted(self):
        report = self.compare('x="private-original"\n', 'x="private-replacement"\n')
        self.assertNotIn('private-', json.dumps(report))

    def test_changed_function_body_is_named(self):
        report = self.compare('def circle(r):\n return r\n', 'def circle(r):\n return r * 2\n')
        definitions = next(c for c in report['changes'] if c['category'] == 'definitions')
        self.assertEqual([item['name'] for item in definitions['removed'] + definitions['added']], ['circle', 'circle'])
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('before.py', 'after.py')]
            paths[0].write_text('def circle(r):\n return r\n')
            paths[1].write_text('def circle(r):\n return r * 2\n')
            result = subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, paths)], capture_output=True, text=True)
        self.assertIn('removed FunctionDef circle 1:1', result.stdout)

    def test_subscript_call_targets_are_not_emitted(self):
        report = self.compare("handlers['secret-key']()\n", "handlers['secret-key'](1)\n")
        effects = next(c for c in report['changes'] if c['category'] == 'effects')
        self.assertEqual([item['name'] for item in effects['removed'] + effects['added']], [None, None])
        self.assertNotIn('secret-key', json.dumps(report))

    def test_no_execution(self):
        report = self.compare('raise RuntimeError("do not execute")\n', 'raise ValueError()\n')
        self.assertEqual(report['exitCode'], 1)

    def test_invalid_syntax_and_context(self):
        for source in ('def :', 'return 1\n'):
            with self.subTest(source=source):
                self.assertEqual(self.compare('x=1\n', source)['exitCode'], 2)

    def test_identical_is_repeatable(self):
        report = self.compare('名前=1\n', '名前=1\n')
        self.assertFalse(report['textChanged'])
        self.assertFalse(report['astChanged'])
        self.assertEqual(report['changes'], [])

    def test_missing_input(self):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--json',
                                 '/missing-yomitoki-before.py', '/missing-yomitoki-after.py'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['exitCode'], 2)
