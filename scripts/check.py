import argparse
import ast
import hashlib
import io
import json
import os
import platform
import shlex
import sys
import tokenize
from pathlib import Path

DEFAULT_LIMITS = {'max-lines': 50, 'max-params': 4, 'max-depth': 3, 'max-complexity': 10}
EXCLUDED = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.next', 'build', 'dist'}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = FUNCTIONS + (ast.ClassDef, ast.Lambda)
BLOCKS = {'If', 'For', 'AsyncFor', 'While', 'With', 'AsyncWith', 'Try', 'TryStar', 'Match'}
ACCEPTABLE_RULES = (*DEFAULT_LIMITS, 'no-nested-ternary')
ACCEPTED_KEYS = {'path', 'function', 'rule', 'actual', 'reason'}


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('閾値は1以上の整数で指定してください')
    return number


def is_accepted_entry(entry):
    if not isinstance(entry, dict) or set(entry) != ACCEPTED_KEYS:
        return False
    return (
        isinstance(entry['path'], str) and not Path(entry['path']).is_absolute()
        and isinstance(entry['function'], (str, type(None))) and entry['rule'] in ACCEPTABLE_RULES
        and type(entry['actual']) is int and entry['actual'] >= 1
        and isinstance(entry['reason'], str) and bool(entry['reason'].strip())
    )


def load_accepted(value):
    path = Path(value)
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as error:
        raise argparse.ArgumentTypeError(f'許容リストを読めません: {error}')
    if not isinstance(entries, list):
        raise argparse.ArgumentTypeError('許容リストはJSON配列である必要があります')
    for entry in entries:
        if not is_accepted_entry(entry):
            raise argparse.ArgumentTypeError(
                '許容リストの項目が不正です。path（相対パス）・function・rule（構文指標のみ）・actual（許容時の実測値）・reason（空不可）が必要です: '
                + json.dumps(entry, ensure_ascii=False)
            )
    # Paths are relative to the list file, so the result does not depend on the working directory.
    base = path.resolve().parent
    return [dict(entry, path=str((base / entry['path']).resolve())) for entry in entries]


def collect_files(targets, report):
    files = set()

    def skip(path, reason):
        report['skipped'].append({'path': str(path), 'reason': reason})

    def add(path):
        if path.is_symlink():
            skip(path, 'symbolic-link')
        elif path.suffix != '.py':
            skip(path, 'unsupported-extension')
        elif path.is_file():
            files.add(path.resolve())

    for target in targets:
        path = Path(os.path.abspath(target))
        if path.is_symlink():
            skip(path, 'symbolic-link')
        elif not path.exists():
            report['errors'].append({'path': str(path), 'message': '対象が存在しません'})
        elif path.is_dir():
            def walk_error(error):
                report['errors'].append({'path': str(error.filename), 'message': str(error)})

            for directory, dirs, names in os.walk(path, onerror=walk_error, followlinks=False):
                base = Path(directory)
                for name in sorted(dirs[:]):
                    child = base / name
                    if name in EXCLUDED or child.is_symlink():
                        dirs.remove(name)
                        skip(child, 'excluded-directory' if name in EXCLUDED else 'symbolic-link')
                dirs.sort()
                for name in sorted(names):
                    add(base / name)
        else:
            add(path)
    return sorted(files)


def code_lines(source):
    lines = set()
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in ignored:
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines


def issue(report, path, node, rule, actual, limit, function):
    report['issues'].append({
        'path': str(path), 'line': node.lineno, 'column': node.col_offset + 1,
        'rule': rule, 'actual': actual, 'limit': limit, 'function': function,
    })


def measure_function(function, path, lines, report):
    args = function.args
    parameters = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    parameters += int(args.vararg is not None) + int(args.kwarg is not None)
    metrics = {
        'path': str(path), 'line': function.lineno, 'name': function.name,
        'lines': len(lines.intersection(range(function.lineno, function.end_lineno + 1))),
        'params': parameters, 'depth': 0, 'complexity': 1,
    }

    def visit(node, depth=0):
        if isinstance(node, SCOPES):
            return
        kind = type(node).__name__
        if kind in BLOCKS:
            depth += 1
            metrics['depth'] = max(metrics['depth'], depth)
            if depth > report['limits']['max-depth']:
                issue(report, path, node, 'max-depth', depth, report['limits']['max-depth'], function.name)
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler)):
            metrics['complexity'] += 1
        elif isinstance(node, ast.BoolOp):
            metrics['complexity'] += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            metrics['complexity'] += 1 + len(node.ifs)
        elif kind == 'match_case':
            wildcard = type(node.pattern).__name__ == 'MatchAs' and node.pattern.pattern is None and node.guard is None
            metrics['complexity'] += int(not wildcard) + int(node.guard is not None)
        if isinstance(node, ast.If):
            visit(node.test, depth)
            for child in node.body:
                visit(child, depth)
            for child in node.orelse:
                is_elif = isinstance(child, ast.If) and child.col_offset == node.col_offset
                visit(child, depth - 1 if is_elif else depth)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, depth)

    for statement in function.body:
        visit(statement)
    report['functions'].append(metrics)
    for metric, rule in [('lines', 'max-lines'), ('params', 'max-params'), ('complexity', 'max-complexity')]:
        if metrics[metric] > report['limits'][rule]:
            issue(report, path, function, rule, metrics[metric], report['limits'][rule], function.name)


def check_ternaries(node, path, report, depth=0, function=None):
    if isinstance(node, SCOPES):
        depth = 0
    if isinstance(node, FUNCTIONS):
        function = node.name
    if isinstance(node, ast.IfExp):
        depth += 1
        if depth > 1:
            issue(report, path, node, 'no-nested-ternary', depth, 1, function)
    for child in ast.iter_child_nodes(node):
        check_ternaries(child, path, report, depth, function)


def inspect_file(path, report):
    try:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        source = raw.decode(encoding)
        tree = ast.parse(source, filename=str(path))
        # ast.parse alone accepts some invalid contexts, such as return outside a function.
        compile(tree, str(path), 'exec')
        lines = code_lines(source)
    except (OSError, SyntaxError, UnicodeError, LookupError, ValueError, tokenize.TokenError, RecursionError) as error:
        report['errors'].append({
            'path': str(path), 'line': getattr(error, 'lineno', None),
            'message': getattr(error, 'msg', str(error)),
        })
        return
    report['files'].append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS):
            measure_function(node, path, lines, report)
    check_ternaries(tree, path, report)


def accept_issues(report, accepted):
    # Matching uses the bare function name, so same-named methods in one file share an entry.
    entries = {(entry['path'], entry['function'], entry['rule']): entry for entry in accepted}
    seen = set()
    remaining = []
    report['accepted'] = []
    for item in report['issues']:
        key = (item['path'], item['function'], item['rule'])
        entry = entries.get(key)
        if entry is None:
            remaining.append(item)
            continue
        seen.add(key)
        # A finding that got worse than when it was accepted is reported again.
        if item['actual'] <= entry['actual']:
            report['accepted'].append(dict(item, acceptedActual=entry['actual'], reason=entry['reason']))
        else:
            remaining.append(dict(item, acceptedActual=entry['actual']))
    report['issues'] = remaining
    report['unusedAccepted'] = [entry for key, entry in entries.items() if key not in seen]


def build_report(targets, limits, accepted=None):
    report = {
        'schemaVersion': 1, 'kind': 'yomitoki', 'pythonVersion': platform.python_version(),
        'checkerSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'limits': limits, 'files': [], 'filesChecked': 0, 'functions': [],
        'issues': [], 'errors': [], 'skipped': [],
    }
    paths = collect_files(targets, report)
    for path in paths:
        inspect_file(path, report)
    report['filesChecked'] = len(report['files'])
    if not report['filesChecked']:
        report['errors'].append({'path': None, 'message': '検査できたPythonファイルがありません'})
    report['functions'].sort(key=lambda item: (item['path'], item['line']))
    report['issues'].sort(key=lambda item: (item['path'], item['line'], item['column'], item['rule']))
    report['skipped'].sort(key=lambda item: (item['path'], item['reason']))
    if accepted is not None:
        accept_issues(report, accepted)
    report['exitCode'] = 2 if report['errors'] else int(bool(report['issues']))
    return report


def print_accepted(report):
    print(f"許容済み: {len(report['accepted'])}件")
    for item in report['accepted']:
        location = f"{item['path']}:{item['line']}:{item['column']}"
        print(f"  {location} {item['rule']} 実測={item['actual']} 上限={item['limit']} 理由: {item['reason']}")
    for item in report['issues']:
        if 'acceptedActual' in item:
            location = f"{item['path']}:{item['line']}:{item['column']}"
            print(f"許容時より悪化: {location} {item['rule']} 実測={item['actual']} 許容時={item['acceptedActual']}")
    for entry in report['unusedAccepted']:
        print(f"不要になった許容: {entry['path']} {entry['function']} {entry['rule']}")


def print_format_command(report):
    paths = [item['path'] for item in report['issues'] if item['rule'] == 'format-required']
    if 'ruff' in report and paths:
        print('同じ設定で整形するには: ' + shlex.join(report['ruff']['formatCommand'] + paths))


def print_report(report, as_json):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"検査: {report['filesChecked']}ファイル / 指摘: {len(report['issues'])} / エラー: {len(report['errors'])}")
        if 'scope' in report:
            print('付属検査の範囲: 構文指標・Ruff lint (' + ','.join(report['scope']['ruffRules']) + ')・整形')
            print('プロジェクトの検査: 未実行（既存設定のlint・型チェック・テスト・ビルドは別途実行）')
        print('閾値: ' + json.dumps(report['limits'], ensure_ascii=False))
        if 'ruff' in report:
            print(f"Ruff: {report['ruff']['version']} / 検査完了: {report['ruff']['filesChecked']}ファイル")
        for item in report['issues']:
            detail = item.get('message') or f"実測={item['actual']} 上限={item['limit']}"
            print(f"{item['path']}:{item['line']}:{item['column']} {item['rule']} {detail}")
        print_format_command(report)
        if 'accepted' in report:
            print_accepted(report)
        for item in report['errors']:
            print(f"ERROR {item['path']}:{item.get('line', '')} {item['message']}")
        if report['skipped']:
            print(f"除外: {len(report['skipped'])}件（詳細は --json）")
        print('閾値内でも、命名・責務・仕様の正しさを保証するものではありません。')


def main():
    parser = argparse.ArgumentParser(description='Pythonコードの可読性に関する指標を構文解析で検査します。対象コードは実行・変更しません。')
    parser.add_argument('targets', nargs='+', help='Pythonファイルまたはディレクトリ')
    parser.add_argument('--json', action='store_true', help='機械処理用JSONを出力')
    parser.add_argument('--accepted', type=load_accepted, help='理由付きで許容する構文指標の指摘（JSON）')
    for rule, default in DEFAULT_LIMITS.items():
        parser.add_argument('--' + rule, type=positive_int, default=default)
    args = parser.parse_args()
    limits = {rule: getattr(args, rule.replace('-', '_')) for rule in DEFAULT_LIMITS}
    report = build_report(args.targets, limits, args.accepted)
    print_report(report, args.json)
    return report['exitCode']


if __name__ == '__main__':
    sys.exit(main())
