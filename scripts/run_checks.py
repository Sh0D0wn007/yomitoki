import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

from check import DEFAULT_LIMITS, build_report, load_accepted, positive_int, print_report, use_utf8_output

RULES = 'F401,F841,SIM,RET'
# ponytail: fixed count; very long paths can exceed Windows' 32,767-character command line,
# and that chunk then falls back to one file at a time. Split by total length if that gets slow.
CHUNK_SIZE = 100
RUFF_ERRORS = (OSError, ValueError, LookupError, TypeError, UnicodeError, SyntaxError, subprocess.TimeoutExpired)


def find_ruff(explicit):
    if explicit:
        return explicit
    environment = Path(__file__).resolve().parents[1] / '.venv'
    # Windows virtual environments keep executables in Scripts\ as .exe files.
    bundled = environment / 'Scripts' / 'ruff.exe' if os.name == 'nt' else environment / 'bin' / 'ruff'
    return str(bundled) if bundled.is_file() else shutil.which('ruff')


def execute(command, source=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('RUFF_')}
    # Bytes, not text mode: on Windows text mode turns \n into \r\n and encodes with the locale instead of UTF-8.
    data = None if source is None else source.encode('utf-8')
    result = subprocess.run(command, input=data, capture_output=True, timeout=60, check=False, env=environment)
    result.stdout, result.stderr = result.stdout.decode('utf-8', 'replace'), result.stderr.decode('utf-8', 'replace')
    if result.returncode not in (0, 1):
        raise ValueError(f"Ruffの実行失敗 ({result.returncode}): {result.stderr.strip()}")
    return result


def shared_options(profile):
    return ['--isolated', '--no-cache', '--target-version', profile['targetVersion'], '--line-length', str(profile['lineLength'])]


def format_command(executable, profile):
    quote_style = 'format.quote-style="' + profile['quoteStyle'] + '"'
    return [executable, 'format', *shared_options(profile), '--config', quote_style]


def read_unchanged(file, message):
    raw = Path(file['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != file['sha256']:
        raise ValueError(message)
    return raw


def ruff_diagnostics(result, paths):
    diagnostics = json.loads(result.stdout)
    if not isinstance(diagnostics, list) or bool(diagnostics) != bool(result.returncode):
        raise ValueError('RuffのJSON出力と終了コードが一致しません')
    located = []
    for item in diagnostics:
        if not isinstance(item, dict) or not isinstance(item.get('code'), str):
            raise ValueError('Ruffの診断形式を解釈できません')
        if item['code'] == 'invalid-syntax':
            raise ValueError('Ruff構文解析エラー: ' + item['message'])
        location = item['location']
        if not isinstance(item.get('message'), str) or not isinstance(location, dict):
            raise ValueError('Ruffの診断内容が不正です')
        if any(type(location.get(key)) is not int or location[key] < 1 for key in ('row', 'column')):
            raise ValueError('Ruffの診断位置が不正です')
        # A file read from stdin is the only one, so its diagnostics need no lookup by name.
        path = paths[0] if len(paths) == 1 else item.get('filename')
        if path not in paths:
            raise ValueError('Ruffの診断が検査対象にないファイルを指しています')
        located.append((path, item))
    return located


def inspect_with_ruff(executable, files, profile):
    paths = [file['path'] for file in files]
    raws = [read_unchanged(file, '構文検査後にファイルが変更されました。再実行してください') for file in files]
    if len(files) == 1:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raws[0]).readline)
        targets, source = ['--stdin-filename', paths[0], '-'], raws[0].decode(encoding)
    else:
        targets, source = paths, None
    lint = execute([
        executable, 'check', *shared_options(profile), '--select', profile['rules'],
        '--ignore-noqa', '--no-fix', '--no-fix-only', '--output-format', 'json', *targets,
    ], source)
    issues = [{
        'tool': 'ruff', 'path': path, 'line': item['location']['row'],
        'column': item['location']['column'], 'rule': item['code'], 'message': item['message'],
    } for path, item in ruff_diagnostics(lint, paths)]
    if source is None:
        formatted = execute([*format_command(executable, profile), '--check', '--output-format', 'json', *targets])
        unformatted = {path for path, _ in ruff_diagnostics(formatted, paths)}
    else:
        # Ruff prints no JSON when checking the format of stdin, so the exit code tells.
        formatted = execute([*format_command(executable, profile), '--check', *targets], source)
        unformatted = set(paths) if formatted.returncode else set()
    issues += [{
        'tool': 'ruff-format', 'path': path, 'line': 1, 'column': 1,
        'rule': 'format-required', 'message': '指定の整形設定との差分があります',
    } for path in sorted(unformatted)]
    for file in files:
        read_unchanged(file, '検査中にファイルが変更されました。再実行してください')
    return issues


def ruff_groups(files):
    # Ruff reads paths as UTF-8 only and skips other files with just a warning,
    # so other encodings and BOM files go one at a time through stdin.
    batch = []
    for file in files:
        try:
            with open(file['path'], 'rb') as stream:
                utf8 = tokenize.detect_encoding(stream.readline)[0] == 'utf-8'
        except (OSError, SyntaxError):
            utf8 = False  # Checked alone, so the error names this file.
        if utf8:
            batch.append(file)
        else:
            yield [file]
    for start in range(0, len(batch), CHUNK_SIZE):
        yield batch[start:start + CHUNK_SIZE]


def check_group(executable, group, profile, report):
    try:
        report['issues'] += inspect_with_ruff(executable, group, profile)
        report['ruff']['filesChecked'] += len(group)
    except RUFF_ERRORS as error:
        if len(group) == 1:
            report['errors'].append({'path': group[0]['path'], 'tool': 'ruff', 'message': str(error)})
            return
        # One file can fail a whole chunk, so each is retried alone to name the cause.
        for file in group:
            check_group(executable, [file], profile, report)


def run_ruff(report, executable, profile):
    report['ruff'] = {
        'version': None, 'profile': profile, 'filesChecked': 0,
        'runnerSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    # Ruff only covers Python; other languages get the syntax metrics alone.
    files = [file for file in report['files'] if file['path'].endswith('.py')]
    if not files:
        return
    try:
        if not executable:
            raise ValueError('Ruffがありません。専用仮想環境へ導入するか --ruff で実行ファイルを指定してください')
        version = execute([executable, '--version'])
        if version.returncode or not version.stdout.strip().startswith('ruff '):
            raise ValueError('Ruffのバージョンを取得できません')
        report['ruff']['version'] = version.stdout.strip()
        report['ruff']['formatCommand'] = format_command(os.path.abspath(shutil.which(executable) or executable), profile)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report['errors'].append({'path': None, 'tool': 'ruff', 'message': str(error)})
        return
    for group in ruff_groups(files):
        check_group(executable, group, profile, report)


def main():
    use_utf8_output()
    parser = argparse.ArgumentParser(description='構文指標と、PythonのRuff lint・整形を一括検査します（自動修正なし）。')
    parser.add_argument('targets', nargs='+')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--ruff', help='Ruff実行ファイルのパス')
    parser.add_argument('--accepted', type=load_accepted, help='理由付きで許容する構文指標の指摘（JSON）')
    parser.add_argument('--line-length', type=positive_int, default=88)
    parser.add_argument('--quote-style', choices=['preserve', 'single', 'double'], default='preserve')
    parser.add_argument('--target-version', default=f'py{sys.version_info.major}{sys.version_info.minor}')
    for rule, default in DEFAULT_LIMITS.items():
        parser.add_argument('--' + rule, type=positive_int, default=default)
    args = parser.parse_args()
    limits = {rule: getattr(args, rule.replace('-', '_')) for rule in DEFAULT_LIMITS}
    report = build_report(args.targets, limits, args.accepted)
    report['kind'] = 'yomitoki-suite'
    report['scope'] = {
        'included': ['ast-metrics', 'ruff-lint', 'ruff-format'],
        'ruffRules': RULES.split(','),
        'notRun': ['project-lint', 'type-check', 'tests', 'build'],
    }
    profile = {
        'rules': RULES, 'lineLength': args.line_length, 'quoteStyle': args.quote_style,
        'targetVersion': args.target_version, 'isolated': True, 'ignoreNoqa': True,
    }
    run_ruff(report, find_ruff(args.ruff), profile)
    report['issues'].sort(key=lambda item: (item['path'], item['line'], item['column'], item['rule']))
    report['exitCode'] = 2 if report['errors'] else int(bool(report['issues']))
    print_report(report, args.json)
    return report['exitCode']


if __name__ == '__main__':
    sys.exit(main())
