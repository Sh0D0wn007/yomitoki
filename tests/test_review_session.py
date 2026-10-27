import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import check
import review_session


class ReviewSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target_dir = self.root / 'source files'
        self.target_dir.mkdir()
        self.source = self.target_dir / '日本語 module.py'
        self.source.write_text('VALUE = 1\n', encoding='utf-8')
        self.state = self.root / 'review state'

    def report(self, paths):
        report = check.build_report(paths, check.DEFAULT_LIMITS)
        report['kind'] = 'yomitoki-suite'
        report['scope'] = {'included': ['ast-metrics', 'ruff-lint', 'ruff-format'],
                           'ruffRules': ['F401', 'F841', 'SIM', 'RET'],
                           'notRun': ['project-lint', 'type-check', 'tests', 'build']}
        report['ruff'] = {
            'version': 'ruff test', 'profile': {'rules': 'F401,F841,SIM,RET'},
            'filesChecked': len(paths), 'runnerSha256': 'a' * 64,
        }
        report['exitCode'] = 2 if report['errors'] else int(bool(report['issues']))
        return report

    def fake_runner(self, options):
        paths = [str(Path(path).resolve()) for path in options if path.endswith('.py')]
        report = self.report(paths)
        data = json.dumps(report, ensure_ascii=False).encode('utf-8')
        return SimpleNamespace(stdout=data, stderr=b'', returncode=report['exitCode'])

    def begin(self, target=None, context=False):
        args = SimpleNamespace(
            state_dir=str(self.state), targets=[str(target or self.source)], ruff=None,
            line_length=88, quote_style='preserve', target_version='py311',
            context=context,
        )
        with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
            with contextlib.redirect_stdout(io.StringIO()):
                return review_session.begin(args)

    def finish(self, include_diffs=False, review_notes=None):
        args = SimpleNamespace(state_dir=str(self.state), include_diffs=include_diffs,
                               review_notes=review_notes)
        with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_session.finish(args)
        return code, json.loads(output.getvalue())

    def test_snapshots_current_modified_bytes_and_refuses_overwrite(self):
        self.source.write_text('VALUE = 2\n', encoding='utf-8')
        self.assertEqual(self.begin(), 0)
        session = json.loads((self.state / 'session.json').read_text(encoding='utf-8'))
        entry = session['files'][0]
        snapshot = self.state / entry['snapshot']
        self.assertEqual(snapshot.read_bytes(), self.source.read_bytes())
        self.assertEqual(entry['sha256'], hashlib.sha256(snapshot.read_bytes()).hexdigest())
        self.assertIn('--target-version', session['runnerArgs'])
        self.assertEqual(session['directoryTargets'], [])
        with self.assertRaisesRegex(review_session.SessionError, 'already exists'):
            self.begin()

    def test_incomplete_begin_preserves_diagnostic_but_cannot_finish(self):
        failed = SimpleNamespace(stdout=b'{"errors": ["missing Ruff"], "exitCode": 2}',
                                 stderr=b'', returncode=2)
        args = SimpleNamespace(state_dir=str(self.state), targets=[str(self.source)],
                               ruff=None, line_length=88, quote_style='preserve', target_version='py311')
        with patch.object(review_session, 'invoke_runner', return_value=failed):
            with self.assertRaisesRegex(review_session.SessionError, 'incomplete'):
                review_session.begin(args)
        self.assertTrue((self.state / 'before.json').is_file())
        self.assertFalse((self.state / 'session.json').exists())
        with self.assertRaises(review_session.SessionError):
            self.finish()

    def test_existing_state_root_allocates_unique_sessions_and_returns_finish_path(self):
        self.state.mkdir()
        marker = self.state / 'other-task.json'
        marker.write_bytes(b'keep existing work')
        sessions = []
        for _ in range(2):
            with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
                with patch.object(check, 'use_utf8_output'):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(review_session.main([
                            'begin', '--state-root', str(self.state), '--context', str(self.source),
                        ]), 0)
            summary = json.loads(output.getvalue())
            state = Path(summary['stateDir'])
            sessions.append(state)
            self.assertEqual(state.parent, self.state)
            self.assertTrue((state / 'session.json').is_file())
            self.assertEqual(summary['reviewNotesPath'], str(state / 'review-notes.json'))
            self.assertEqual(summary['finishCommand'][-5:], [
                '--state-dir', str(state), '--include-diffs', '--review-notes', summary['reviewNotesPath'],
            ])
            self.assertEqual(summary['context']['sources'][0]['content'], self.source.read_bytes().decode('utf-8'))
        self.assertNotEqual(*sessions)
        self.assertEqual(marker.read_bytes(), b'keep existing work')
        self.state = sessions[0]
        self.assertEqual(self.finish()[0], 0)

    def test_new_state_root_is_created_outside_target(self):
        with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(review_session.begin(SimpleNamespace(
                    state_root=str(self.state / 'new parent'), targets=[str(self.source)], ruff=None,
                    line_length=88, quote_style='preserve', target_version='py311',
                )), 0)
        state = Path(json.loads(output.getvalue())['stateDir'])
        self.assertEqual(state.parent, self.state / 'new parent')
        self.assertTrue((state / 'before.json').is_file())

    def test_state_root_inside_target_is_rejected_before_creating_anything(self):
        root = self.target_dir / 'not created'
        with patch.object(review_session, 'invoke_runner', side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(review_session.SessionError, 'outside target'):
                review_session.begin(SimpleNamespace(state_root=str(root), targets=[str(self.source)]))
        self.assertFalse(root.exists())

    def test_state_root_file_is_not_replaced(self):
        self.state.write_bytes(b'keep this file')
        with self.assertRaises(OSError):
            review_session.begin(SimpleNamespace(state_root=str(self.state), targets=[str(self.source)]))
        self.assertEqual(self.state.read_bytes(), b'keep this file')

    def test_source_changed_after_checks_is_not_a_success(self):
        self.begin()

        def moving_runner(options):
            result = self.fake_runner(options)
            self.source.write_text('VALUE = 3\n', encoding='utf-8')
            return result

        with patch.object(review_session, 'invoke_runner', side_effect=moving_runner):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_session.finish(SimpleNamespace(state_dir=str(self.state)))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'error')

    def test_failed_structural_diff_is_reported_as_incomplete(self):
        self.begin()
        self.source.write_text('VALUE = 2\n', encoding='utf-8')
        original_process = review_session.process

        def failing_diff(args):
            if args[0] == review_session.PYTHON_DIFF:
                return SimpleNamespace(stdout=b'{"errors": ["diff unavailable"], "exitCode": 2}',
                                       stderr=b'', returncode=2)
            return original_process(args)

        with patch.object(review_session, 'process', side_effect=failing_diff):
            code, summary = self.finish()
        self.assertEqual(code, 2)
        self.assertEqual(summary['status'], 'error')
        self.assertTrue(summary['errors'])

    def test_rejects_state_inside_target_directory(self):
        args = SimpleNamespace(
            state_dir=str(self.target_dir / 'state'), targets=[str(self.target_dir)],
            ruff=None, line_length=88, quote_style='preserve', target_version='py311',
        )
        with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
            with self.assertRaisesRegex(review_session.SessionError, 'outside target'):
                review_session.begin(args)

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks are unavailable')
    def test_rejects_state_symlink_resolving_inside_target(self):
        alias = self.root / 'state alias'
        try:
            alias.symlink_to(self.target_dir, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        args = SimpleNamespace(
            state_dir=str(alias / 'new state'), targets=[str(self.source)],
            ruff=None, line_length=88, quote_style='preserve', target_version='py311',
        )
        with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
            with self.assertRaisesRegex(review_session.SessionError, 'outside target'):
                review_session.begin(args)

    def test_findings_are_expected_and_source_is_never_executed_or_modified(self):
        marker = self.root / 'executed'
        original = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nVALUE = 1\n"
        self.source.write_text(original, encoding='utf-8')

        def finding_runner(options):
            result = self.fake_runner(options)
            report = json.loads(result.stdout)
            report['issues'] = [{'path': str(self.source.resolve()), 'line': 1, 'column': 1,
                                 'tool': 'ast', 'rule': 'max-lines', 'actual': 60, 'limit': 50}]
            report['exitCode'] = 1
            result.stdout = json.dumps(report).encode('utf-8')
            result.returncode = 1
            return result

        args = SimpleNamespace(state_dir=str(self.state), targets=[str(self.source)], ruff=None,
                               line_length=88, quote_style='preserve', target_version='py311')
        with patch.object(review_session, 'invoke_runner', side_effect=finding_runner):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(review_session.begin(args), 0)
            baseline_summary = json.loads(output.getvalue())
            self.assertEqual(baseline_summary['checkExitCode'], 1)
            self.assertEqual(baseline_summary['issueCount'], 1)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(review_session.finish(SimpleNamespace(state_dir=str(self.state))), 0)
            final_summary = json.loads(output.getvalue())
            self.assertEqual(final_summary['checkExitCode'], 1)
            self.assertEqual(final_summary['issueCount'], 1)
            self.assertEqual(final_summary['comparisonStatus'], 'no-increase')
        self.assertFalse(marker.exists())
        self.assertEqual(self.source.read_text(encoding='utf-8'), original)

    def test_finish_captures_edits_and_rejects_removed_target(self):
        self.begin()
        self.source.write_text('VALUE = 3\n', encoding='utf-8')
        code, summary = self.finish()
        self.assertEqual(code, 0)
        self.assertEqual(summary['changedFiles'], [str(self.source.resolve())])
        self.assertFalse(summary['behaviorProven'])
        artifacts = Path(summary['artifacts']['unified-diffs.json'])
        self.assertIn('VALUE = 3', artifacts.read_text(encoding='utf-8'))
        self.assertTrue(json.loads(Path(summary['artifacts']['python-diffs.json']).read_text(encoding='utf-8'))[0]['astChanged'])

        second = self.root / 'second state'
        self.state = second
        self.begin()
        self.source.unlink()
        with self.assertRaisesRegex(review_session.SessionError, 'target is missing'):
            review_session.validate_session(second)

    def test_finish_with_comment_edit_in_deep_expression(self):
        source = 'value = ' + '+'.join(['1'] * 1500) + '\n'
        self.source.write_text(source, encoding='utf-8')
        self.assertEqual(self.begin(), 0)
        self.source.write_text(source + '# comment\n', encoding='utf-8')
        code, summary = self.finish()
        self.assertEqual(code, 0, summary['errors'])
        self.assertEqual(summary['status'], 'no-increase')
        self.assertEqual(summary['changedFiles'], [str(self.source.resolve())])
        self.assertEqual(summary['pythonStructuralChanges'], [{
            'path': str(self.source.resolve()), 'astChanged': False, 'errors': [],
        }])

    def test_include_diffs_embeds_the_saved_artifacts(self):
        self.begin()
        self.source.write_text('VALUE = 3\n', encoding='utf-8')
        code, summary = self.finish(include_diffs=True)
        self.assertEqual(code, 0)
        self.assertEqual(summary['unifiedDiffs'], json.loads(
            Path(summary['artifacts']['unified-diffs.json']).read_text(encoding='utf-8')))
        self.assertEqual(summary['pythonDiffs'], json.loads(
            Path(summary['artifacts']['python-diffs.json']).read_text(encoding='utf-8')))

    def test_context_is_full_and_decoded_from_verified_snapshots_without_execution(self):
        marker = self.root / 'context-executed'
        unicode_source = self.target_dir / 'unicode.py'
        unicode_text = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nVALUE = '日本語'\n"
        unicode_source.write_bytes(unicode_text.encode('utf-8'))
        cookie_source = self.target_dir / 'legacy.py'
        cookie_bytes = '# coding: latin-1\nVALUE = "café"\n'.encode('latin-1')
        cookie_source.write_bytes(cookie_bytes)
        runner_calls = []
        with patch.object(review_session, 'invoke_runner', side_effect=lambda options: (
                runner_calls.append(list(options)) or self.fake_runner(options))):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(review_session.begin(SimpleNamespace(
                    state_dir=str(self.state), targets=[str(self.target_dir)], ruff=None,
                    line_length=88, quote_style='preserve', target_version='py311', context=True,
                )), 0)
        summary = json.loads(output.getvalue())
        context = summary['context']
        references = Path(__file__).resolve().parents[1] / 'references'
        self.assertEqual(context['principles'], (references / 'principles.md').read_bytes().decode('utf-8'))
        self.assertEqual(context['reviewSession'], (references / 'review-session.md').read_bytes().decode('utf-8'))
        sources = {Path(item['path']).name: item for item in context['sources']}
        self.assertEqual(sources['unicode.py']['path'], str(unicode_source.resolve()))
        self.assertEqual(sources['unicode.py']['encoding'], 'utf-8')
        self.assertEqual(sources['unicode.py']['content'], unicode_text)
        self.assertEqual(sources['legacy.py']['encoding'], 'iso-8859-1')
        self.assertEqual(sources['legacy.py']['content'], cookie_bytes.decode('latin-1'))
        self.assertEqual(len(runner_calls), 1)
        self.assertFalse(marker.exists())
        self.assertEqual(unicode_source.read_text(encoding='utf-8'), unicode_text)
        self.assertEqual(cookie_source.read_bytes(), cookie_bytes)

    def write_review_notes(self, records, name='review-notes.json'):
        path = self.root / name
        path.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
        return path

    def test_unreadable_or_changed_context_is_not_silently_replaced(self):
        with self.assertRaises(review_session.SessionError):
            review_session.decode_snapshot('source.ts', b'\xff')
        self.begin()
        metadata = json.loads((self.state / 'session.json').read_text(encoding='utf-8'))
        (self.state / metadata['files'][0]['snapshot']).write_bytes(b'CHANGED = 1\n')
        with self.assertRaisesRegex(review_session.SessionError, 'hash mismatch'):
            review_session.build_context(self.state, metadata)

    def test_blank_evidence_or_invalid_review_claims_do_not_pass(self):
        complete = [{'item': item, 'status': 'checked', 'evidence': 'source.py: purpose checked'}
                    for item in range(1, 15)]
        for changed in ({'evidence': '  '}, {'status': 'passed'}, {'item': True}, {'extra': 'ignored'}):
            records = [dict(record) for record in complete]
            records[0].update(changed)
            with self.assertRaises(review_session.SessionError):
                review_session.read_review_notes(self.write_review_notes(records))

    def test_complete_review_notes_are_preserved_as_coverage_metadata_only(self):
        self.begin()
        records = [
            {'item': item, 'status': 'not-applicable' if item == 14 else 'checked',
             'evidence': f'確認 {item}'}
            for item in range(1, 15)
        ]
        notes_path = self.write_review_notes(records)
        code, summary = self.finish(review_notes=notes_path)
        self.assertEqual(code, 0)
        self.assertEqual(summary['reviewCoverage'], 'complete')
        self.assertEqual(summary['reviewNotes'], records)
        self.assertEqual(json.loads(Path(summary['artifacts']['review-notes.json']).read_text(encoding='utf-8')), records)
        self.assertFalse(summary['behaviorProven'])

    def test_review_notes_reject_missing_and_duplicate_items_before_running_checks(self):
        self.begin()
        complete = [{'item': item, 'status': 'checked', 'evidence': f'line {item}'}
                    for item in range(1, 15)]
        for records in (complete[:-1], [*complete[:-1], dict(complete[0])]):
            notes_path = self.write_review_notes(records, f'bad-{len(records)}-{records[-1]["item"]}.json')
            with patch.object(review_session, 'invoke_runner', side_effect=AssertionError('runner must not run')):
                with patch.object(check, 'use_utf8_output'):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        code = review_session.main(['finish', '--state-dir', str(self.state),
                                                    '--review-notes', str(notes_path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())['status'], 'error')

    def test_unverified_review_note_is_visible_and_makes_finish_incomplete(self):
        self.begin()
        records = [{'item': item, 'status': 'unverified' if item == 6 else 'checked',
                    'evidence': f'uncertain point {item}'} for item in range(1, 15)]
        code, summary = self.finish(review_notes=self.write_review_notes(records))
        self.assertEqual(code, 2)
        self.assertEqual(summary['reviewCoverage'], 'incomplete')
        self.assertEqual(summary['reviewNotes'], records)
        self.assertFalse(summary['behaviorProven'])

    def test_baseline_tampering_is_detected(self):
        self.begin()
        (self.state / 'before.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(review_session.SessionError, 'baseline report was modified'):
            review_session.validate_session(self.state)

    def test_added_recognized_file_under_directory_target_is_not_comparable(self):
        self.state = self.root / 'directory review'
        args = SimpleNamespace(
            state_dir=str(self.state), targets=[str(self.target_dir)], ruff=None,
            line_length=88, quote_style='preserve', target_version='py311',
        )
        with patch.object(review_session, 'invoke_runner', side_effect=self.fake_runner):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review_session.begin(args), 0)
        (self.target_dir / 'added.py').write_text('NEW_VALUE = 1\n', encoding='utf-8')
        code, summary = self.finish()
        self.assertEqual(code, 2)
        self.assertEqual(summary['comparisonStatus'], 'not-comparable')
        self.assertEqual(summary['addedFilesUnderDirectoryTargets'], [str((self.target_dir / 'added.py').resolve())])


if __name__ == '__main__':
    unittest.main()
