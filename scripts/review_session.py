"""Save a check baseline and collect review artifacts without changing sources."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tokenize
import uuid

import check


SCRIPTS = Path(__file__).resolve().parent
RUNNER = SCRIPTS / 'run_checks.py'
COMPARATOR = SCRIPTS / 'compare_reports.py'
PYTHON_DIFF = SCRIPTS / 'diff_code.py'
NOT_RUN = ['project-lint', 'type-check', 'tests', 'build']


class SessionError(Exception):
    """An invalid or incomplete review session operation."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def read_json(path):
    return json.loads(path.read_bytes())


def process(args):
    environment = os.environ.copy()
    environment['PYTHONUTF8'] = '1'
    return subprocess.run(
        [sys.executable, '-B', *map(str, args)], capture_output=True,
        env=environment, check=False,
    )


def output_text(data):
    return data.decode('utf-8', 'replace')


def is_within(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def ensure_state_outside_targets(state_dir, files, targets):
    state = state_dir.resolve()
    protected = [Path(item).resolve() for item in files]
    for raw in targets:
        target = Path(raw).resolve()
        protected.append(target if target.is_dir() else target.parent)
    for directory in protected:
        if is_within(state, directory):
            raise SessionError(f'state directory must be outside target directory or file parent: {directory}')


def discover(targets):
    report = {'files': [], 'errors': [], 'skipped': []}
    paths = [str(path) for path in check.collect_files(targets, report)]
    if not paths:
        detail = {'errors': report['errors'], 'skipped': report['skipped']}
        raise SessionError('no recognized files were found: ' + json.dumps(detail, ensure_ascii=False))
    if report['errors']:
        raise SessionError('target discovery was incomplete: ' + json.dumps(report['errors'], ensure_ascii=False))
    return paths


def runner_options(ruff, line_length, quote_style, target_version, targets):
    options = ['--json']
    if ruff:
        options += ['--ruff', ruff]
    options += ['--line-length', str(line_length), '--quote-style', quote_style,
                '--target-version', target_version]
    return [*options, *targets]


def invoke_runner(options):
    return process([RUNNER, *options])


def report_is_complete(report, expected_paths):
    if not isinstance(report, dict) or report.get('errors'):
        return False
    actual = [item.get('path') for item in report.get('files', []) if isinstance(item, dict)]
    return report.get('filesChecked') == len(expected_paths) and actual == expected_paths


def report_matches_snapshot(report, entries):
    hashes = {item.get('path'): item.get('sha256') for item in report.get('files', [])}
    return all(hashes.get(item['path']) == item['sha256'] for item in entries)


def create_state_dir(args, paths, targets):
    state_root = getattr(args, 'state_root', None)
    if state_root is not None:
        root = Path(state_root).absolute()
        ensure_state_outside_targets(root, paths, targets)
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix='review-', dir=root))

    state_dir = Path(args.state_dir).absolute()
    if state_dir.exists() or state_dir.is_symlink():
        raise SessionError(f'state directory already exists: {state_dir}')
    ensure_state_outside_targets(state_dir, paths, targets)
    state_dir.mkdir(parents=True)
    return state_dir


def begin(args):
    targets = [str(Path(target).absolute()) for target in args.targets]
    paths = discover(targets)
    if any(Path(path).is_symlink() for path in paths):
        raise SessionError('symbolic link targets cannot be snapshotted')

    state_dir = create_state_dir(args, paths, targets)
    try:
        snapshot_dir = state_dir / 'snapshot'
        snapshot_dir.mkdir()
        entries = []
        for index, path in enumerate(paths):
            raw = Path(path).read_bytes()
            relative = f'{index:04d}-{Path(path).name}'
            (snapshot_dir / relative).write_bytes(raw)
            entries.append({'path': path, 'sha256': digest(raw), 'snapshot': f'snapshot/{relative}'})

        options = runner_options(args.ruff, args.line_length, args.quote_style, args.target_version, paths)
        result = invoke_runner(options)
        before = state_dir / 'before.json'
        before.write_bytes(result.stdout)
        if result.stderr:
            (state_dir / 'before.stderr.txt').write_bytes(result.stderr)
        try:
            report = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise SessionError(f'baseline check did not produce valid JSON: {output_text(result.stderr).strip() or error}')
        if (result.returncode not in (0, 1) or not report_is_complete(report, paths)
                or not report_matches_snapshot(report, entries)):
            raise SessionError('baseline checks were incomplete; see before.json and before.stderr.txt')
        metadata = {
            'schemaVersion': 1,
            'targets': paths,
            'requestedTargets': targets,
            'directoryTargets': [path for path in targets if Path(path).is_dir()],
            'runnerArgs': options,
            'files': entries,
            'beforeSha256': digest(before.read_bytes()),
        }
        write_json(state_dir / 'session.json', metadata)
        summary = {
            'status': 'ready', 'stateDir': str(state_dir), 'beforeReport': str(before),
            'reviewNotesPath': str(state_dir / 'review-notes.json'),
            'finishCommand': [sys.executable, '-B', str(SCRIPTS / 'review_session.py'),
                              'finish', '--state-dir', str(state_dir), '--include-diffs',
                              '--review-notes', str(state_dir / 'review-notes.json')],
            'files': paths, 'issueCount': len(report.get('issues', [])),
            'checkExitCode': result.returncode,
            'issues': report.get('issues', []), 'errors': report.get('errors', []),
            'scope': report.get('scope', {}),
            'formatCommand': report.get('ruff', {}).get('formatCommand'),
            'projectChecksNotRun': NOT_RUN,
            'behaviorProven': False,
        }
        if getattr(args, 'context', False):
            summary['context'] = build_context(state_dir, metadata)
        print(json.dumps(summary, ensure_ascii=False))
        # Preparing a valid baseline succeeds even when the checks found issues.
        return 0
    except Exception:
        # A failed begin is not a usable session; preserve diagnostic output and files.
        raise


def validate_session(state_dir):
    metadata_path = state_dir / 'session.json'
    before_path = state_dir / 'before.json'
    try:
        metadata = read_json(metadata_path)
        before_bytes = before_path.read_bytes()
        before = json.loads(before_bytes)
    except (OSError, ValueError, TypeError) as error:
        raise SessionError(f'baseline or session metadata is missing or invalid: {error}')
    if not isinstance(metadata, dict):
        raise SessionError('session metadata is invalid')
    if digest(before_bytes) != metadata.get('beforeSha256'):
        raise SessionError('baseline report was modified')
    files = metadata.get('files')
    targets = metadata.get('targets')
    options = metadata.get('runnerArgs')
    if not isinstance(files, list) or not files or not isinstance(targets, list) or not isinstance(options, list):
        raise SessionError('session metadata is invalid')
    if not report_is_complete(before, targets):
        raise SessionError('baseline report is incomplete')
    report_hashes = {item.get('path'): item.get('sha256') for item in before.get('files', [])}
    if len(files) != len(targets):
        raise SessionError('session metadata file list does not match the baseline')
    for item in files:
        try:
            if not isinstance(item, dict) or item.get('path') not in targets:
                raise SessionError('session metadata contains an unexpected target')
            snapshot = (state_dir / item['snapshot']).resolve()
            if not is_within(snapshot, state_dir.resolve()):
                raise SessionError('snapshot path escapes the state directory')
            raw = snapshot.read_bytes()
        except (OSError, KeyError, TypeError) as error:
            raise SessionError(f'snapshot is missing: {error}')
        if digest(raw) != item.get('sha256'):
            raise SessionError(f'snapshot was modified: {item.get("path")}')
        if report_hashes.get(item['path']) != item.get('sha256'):
            raise SessionError(f'baseline and snapshot hashes disagree: {item.get("path")}')
        if not Path(item.get('path', '')).is_file():
            raise SessionError(f'target is missing: {item.get("path")}')
    if {item.get('path') for item in files} != set(targets):
        raise SessionError('session metadata target list does not match the baseline')
    return metadata, before


def decode_snapshot(path, raw):
    try:
        if path.endswith('.py'):
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        else:
            encoding = 'utf-8'
        return encoding, raw.decode(encoding, 'strict')
    except (LookupError, UnicodeError, SyntaxError) as error:
        raise SessionError(f'snapshot cannot be decoded for review context: {path}: {error}')


def read_utf8_reference(path):
    try:
        return path.read_bytes().decode('utf-8', 'strict')
    except (OSError, UnicodeError) as error:
        raise SessionError(f'review reference cannot be read as UTF-8: {path}: {error}')


def build_context(state_dir, metadata):
    references = Path(__file__).resolve().parents[1] / 'references'
    context = {
        'principles': read_utf8_reference(references / 'principles.md'),
        'reviewSession': read_utf8_reference(references / 'review-session.md'),
        'sources': [],
    }
    for item in metadata['files']:
        snapshot_path = (state_dir / item['snapshot']).resolve()
        if not is_within(snapshot_path, state_dir.resolve()):
            raise SessionError(f'snapshot path escapes the state directory: {item["path"]}')
        try:
            raw = snapshot_path.read_bytes()
        except OSError as error:
            raise SessionError(f'snapshot is missing: {item["path"]}: {error}')
        if digest(raw) != item['sha256']:
            raise SessionError(f'snapshot hash mismatch for review context: {item["path"]}')
        encoding, content = decode_snapshot(item['path'], raw)
        context['sources'].append({
            'path': str(Path(item['path']).resolve()),
            'encoding': encoding,
            'content': content,
        })
    return context


def read_review_notes(path):
    try:
        records = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, UnicodeError) as error:
        raise SessionError(f'review notes are missing or invalid JSON: {error}')
    if not isinstance(records, list) or len(records) != 14:
        raise SessionError('review notes must be an array containing exactly 14 records')
    expected_fields = {'item', 'status', 'evidence'}
    allowed_statuses = {'checked', 'not-applicable', 'unverified'}
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise SessionError('each review note must contain exactly item, status, and evidence')
        item = record['item']
        if isinstance(item, bool) or not isinstance(item, int) or item < 1 or item > 14:
            raise SessionError('review note item IDs must be integers from 1 through 14')
        if item in seen:
            raise SessionError(f'duplicate review note item ID: {item}')
        seen.add(item)
        if record['status'] not in allowed_statuses:
            raise SessionError(f'invalid review note status for item {item}')
        if not isinstance(record['evidence'], str) or not record['evidence'].strip():
            raise SessionError(f'review note evidence must be nonblank for item {item}')
    if seen != set(range(1, 15)):
        raise SessionError('review notes must include every item ID from 1 through 14')
    return sorted(records, key=lambda record: record['item'])


def find_added_directory_files(metadata):
    directories = metadata.get('directoryTargets', [])
    if not directories:
        return []
    discovery = {'files': [], 'errors': [], 'skipped': []}
    current = [str(path) for path in check.collect_files(directories, discovery)]
    if discovery['errors']:
        raise SessionError('directory target discovery failed: ' + json.dumps(discovery['errors'], ensure_ascii=False))
    baseline = set(metadata['targets'])
    return sorted(set(current) - baseline)


def unified_diff(path, old, new):
    if path.endswith('.py'):
        try:
            encoding_old, _ = tokenize.detect_encoding(io.BytesIO(old).readline)
            encoding_new, _ = tokenize.detect_encoding(io.BytesIO(new).readline)
            old_text = old.decode(encoding_old, 'replace')
            new_text = new.decode(encoding_new, 'replace')
        except (SyntaxError, LookupError, UnicodeError):
            old_text, new_text = old.decode('utf-8', 'replace'), new.decode('utf-8', 'replace')
    else:
        old_text = old.decode('utf-8', 'replace')
        new_text = new.decode('utf-8', 'replace')
    import difflib
    return ''.join(difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile='before/' + path, tofile='after/' + path,
    ))


def finish(args):
    state_dir = Path(args.state_dir).resolve()
    metadata, _ = validate_session(state_dir)
    review_notes = read_review_notes(args.review_notes) if getattr(args, 'review_notes', None) else None
    finish_dir = state_dir / ('finish-' + uuid.uuid4().hex)
    finish_dir.mkdir()
    if review_notes is not None:
        write_json(finish_dir / 'review-notes.json', review_notes)
    after_result = invoke_runner(metadata['runnerArgs'])
    after_path = finish_dir / 'after.json'
    after_path.write_bytes(after_result.stdout)
    if after_result.stderr:
        (finish_dir / 'after.stderr.txt').write_bytes(after_result.stderr)
    try:
        after_report = json.loads(after_result.stdout)
    except (ValueError, UnicodeError) as error:
        raise SessionError(f'after check did not produce valid JSON: {error}; see {after_path}')
    if not isinstance(after_report, dict):
        after_report = {'errors': ['after check returned JSON that was not an object']}
    after_complete = (after_result.returncode in (0, 1)
                      and report_is_complete(after_report, metadata['targets']))
    if not after_complete:
        after_errors = after_report.get('errors', []) if isinstance(after_report, dict) else []
        after_errors.append('after checks were incomplete or returned an invalid status')
        after_report['errors'] = after_errors

    added_files = find_added_directory_files(metadata)
    reported_hashes = {item.get('path'): item.get('sha256') for item in after_report.get('files', [])}

    compared = process([COMPARATOR, state_dir / 'before.json', after_path, '--json'])
    comparison_path = finish_dir / 'comparison.json'
    comparison_path.write_bytes(compared.stdout)
    try:
        comparison = json.loads(compared.stdout)
    except (ValueError, UnicodeError) as error:
        raise SessionError(f'comparison did not produce valid JSON: {error}')
    if added_files:
        comparison['comparable'] = False
        comparison.setdefault('conditionChanges', []).append('directory-target-files')
        comparison.setdefault('errors', []).append(
            'recognized files were added under a directory target: ' + ', '.join(added_files)
        )
        comparison['status'] = 'not-comparable'
        comparison['exitCode'] = 2
        write_json(comparison_path, comparison)

    diffs = []
    readable = []
    captured_hashes = {}
    source_errors = []
    for item in metadata['files']:
        path = item['path']
        old = (state_dir / item['snapshot']).read_bytes()
        try:
            new = Path(path).read_bytes()
        except OSError as error:
            source_errors.append(f'target could not be read after checks: {path}: {error}')
            continue
        captured_hashes[path] = digest(new)
        if reported_hashes.get(path) != captured_hashes[path]:
            source_errors.append(f'target changed during or after checks: {path}')
        if digest(old) == digest(new):
            continue
        readable.append({'path': path, 'diff': unified_diff(path, old, new)})
        if path.endswith('.py'):
            diff_result = process([PYTHON_DIFF, '--json', state_dir / item['snapshot'], path])
            try:
                structural = json.loads(diff_result.stdout)
            except (ValueError, UnicodeError) as error:
                structural = {'errors': [{'message': str(error)}], 'exitCode': 2}
            diffs.append({'path': path, **structural})
    for path, sha256 in captured_hashes.items():
        try:
            if digest(Path(path).read_bytes()) != sha256:
                source_errors.append(f'target changed during diff collection: {path}')
        except OSError as error:
            source_errors.append(f'target disappeared during diff collection: {path}: {error}')
    write_json(finish_dir / 'python-diffs.json', diffs)
    write_json(finish_dir / 'unified-diffs.json', readable)
    structural_errors = any(item.get('exitCode') == 2 or item.get('errors') for item in diffs)
    if source_errors:
        comparison['comparable'] = False
        comparison.setdefault('conditionChanges', []).append('source-stability')
        comparison.setdefault('errors', []).extend(source_errors)
        comparison['status'] = 'error'
        comparison['exitCode'] = 2
    if not after_complete:
        comparison['comparable'] = False
        comparison['status'] = 'error'
        comparison['exitCode'] = 2
    if structural_errors:
        comparison['comparable'] = False
        comparison['status'] = 'error'
        comparison['exitCode'] = 2
        comparison.setdefault('errors', []).append('Python structural diff collection was incomplete')
    if source_errors or not after_complete or structural_errors:
        write_json(comparison_path, comparison)

    changed = [item['path'] for item in readable]
    errors = [*after_report.get('errors', []), *comparison.get('errors', [])]
    if source_errors:
        errors.extend(source_errors)
    summary = {
        'status': comparison.get('status', 'error'),
        'finishDir': str(finish_dir),
        'artifacts': {name: str(finish_dir / name) for name in (
            'after.json', 'comparison.json', 'python-diffs.json', 'unified-diffs.json',
        )},
        'issueCount': len(after_report.get('issues', [])), 'issues': after_report.get('issues', []),
        'checkExitCode': after_result.returncode,
        'errors': errors,
        'scope': after_report.get('scope', {}),
        'comparisonStatus': comparison.get('status', 'error'),
        'comparison': {key: comparison.get(key) for key in (
            'comparable', 'counts', 'metricChanges', 'ruleChanges',
        )},
        'changedFiles': changed,
        'addedFilesUnderDirectoryTargets': added_files,
        'pythonStructuralChanges': [
            {'path': item['path'], 'astChanged': item.get('astChanged'), 'errors': item.get('errors', [])}
            for item in diffs
        ],
        'projectChecksNotRun': NOT_RUN,
        'behaviorProven': False,
    }
    if review_notes is not None:
        review_coverage = 'incomplete' if any(note['status'] == 'unverified' for note in review_notes) else 'complete'
        summary['reviewCoverage'] = review_coverage
        summary['reviewNotes'] = review_notes
        summary['artifacts']['review-notes.json'] = str(finish_dir / 'review-notes.json')
    else:
        review_coverage = 'not-recorded'
        summary['reviewCoverage'] = review_coverage
    if getattr(args, 'include_diffs', False):
        summary['unifiedDiffs'] = readable
        summary['pythonDiffs'] = diffs
    print(json.dumps(summary, ensure_ascii=False))
    if (not after_complete or compared.returncode not in (0, 1) or added_files
            or source_errors or structural_errors or review_coverage == 'incomplete'):
        return 2
    return compared.returncode


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a positive integer')
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main(argv=None):
    check.use_utf8_output()
    parser = argparse.ArgumentParser(description='Capture and compare a source review session.')
    commands = parser.add_subparsers(dest='command', required=True)
    begin_parser = commands.add_parser('begin')
    destination = begin_parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--state-root', help='Create a unique session inside this outside-target parent.')
    destination.add_argument('--state-dir', help='Use this exact new directory; existing paths are refused.')
    begin_parser.add_argument('--ruff')
    begin_parser.add_argument('--line-length', type=positive_int, default=88)
    begin_parser.add_argument('--quote-style', choices=['preserve', 'single', 'double'], default='preserve')
    begin_parser.add_argument('--target-version', default=f'py{sys.version_info.major}{sys.version_info.minor}')
    begin_parser.add_argument('--context', action='store_true')
    begin_parser.add_argument('targets', nargs='+')
    begin_parser.set_defaults(run=begin)
    finish_parser = commands.add_parser('finish')
    finish_parser.add_argument('--state-dir', required=True)
    finish_parser.add_argument('--include-diffs', action='store_true')
    finish_parser.add_argument('--review-notes')
    finish_parser.set_defaults(run=finish)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (OSError, ValueError, TypeError, KeyError, SessionError) as error:
        print(json.dumps({'status': 'error', 'errors': [str(error)]}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
