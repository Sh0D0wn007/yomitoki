import argparse
import ast
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import platform
import tokenize

from check import use_utf8_output


CATEGORIES = {
    'definitions': {'FunctionDef', 'AsyncFunctionDef', 'ClassDef'},
    'imports': {'Import', 'ImportFrom'},
    'control': {'If', 'IfExp', 'For', 'AsyncFor', 'While', 'Try', 'TryStar',
                'ExceptHandler', 'With', 'AsyncWith', 'Match', 'comprehension'},
    'effects': {'Call', 'Return', 'Raise', 'Yield', 'YieldFrom', 'Await',
                'Break', 'Continue', 'Assert', 'Delete'},
    'data': {'Assign', 'AnnAssign', 'AugAssign', 'NamedExpr', 'Global', 'Nonlocal'},
}
LIMITATIONS = ['Review the full source diff and tests; this report does not prove equivalent behavior.']


def fingerprint(node):
    parts = []
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, ast.AST):
            fields = list(ast.iter_fields(item))
            parts.append(('node', type(item).__name__, tuple(name for name, _ in fields)))
            pending.extend(value for _, value in reversed(fields))
        elif isinstance(item, list):
            parts.append(('list', len(item)))
            pending.extend(reversed(item))
        else:
            parts.append(('value', repr(item)))
    return tuple(parts)


def dotted_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return '.'.join(reversed(parts))


def name_of(node):
    # Only identifiers are shown; subscripts and literals in call targets would leak source values.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    if isinstance(node, ast.ImportFrom):
        return '.' * node.level + (node.module or '')
    if isinstance(node, ast.Import):
        return ', '.join(alias.name for alias in node.names)
    return None


def inspect_source(path):
    if path.suffix != '.py':
        raise ValueError('Expected a .py file')
    raw = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    source = raw.decode(encoding)
    tree = ast.parse(source, filename=str(path), type_comments=True)
    compile(source, str(path), 'exec')
    records = {category: [] for category in CATEGORIES}

    pending = [tree]
    while pending:
        node = pending.pop()
        kind = type(node).__name__
        for category, kinds in CATEGORIES.items():
            if kind in kinds:
                records[category].append((fingerprint(node), {
                    'kind': kind, 'name': name_of(node), 'line': getattr(node, 'lineno', None),
                    'column': getattr(node, 'col_offset', -1) + 1,
                }))
        pending.extend(reversed(list(ast.iter_child_nodes(node))))

    # TypeIgnore.lineno is a field, so fingerprints include it even without attributes.
    for node in ast.walk(tree):
        if isinstance(node, ast.TypeIgnore):
            node.lineno = 0
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}, tree, records


def unmatched(records, other):
    remaining = Counter(key for key, _ in other)
    result = []
    for key, location in records:
        if remaining[key]:
            remaining[key] -= 1
        else:
            result.append(location)
    return result


def compare(before, after):
    report = {'schemaVersion': 1, 'kind': 'yomitoki-source-diff',
              'pythonVersion': platform.python_version(), 'changes': [], 'errors': [],
              'limitations': LIMITATIONS.copy()}
    sources = []
    for side, path in (('before', before), ('after', after)):
        try:
            metadata, tree, records = inspect_source(path)
            report[side] = metadata
            sources.append((tree, records))
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as error:
            # SyntaxError text can contain source literals; report only its type/location.
            report['errors'].append({'side': side, 'path': str(path),
                                     'type': type(error).__name__,
                                     'line': getattr(error, 'lineno', None)})
    if report['errors']:
        report['exitCode'] = 2
        return report
    (old_tree, old), (new_tree, new) = sources
    report['textChanged'] = report['before']['sha256'] != report['after']['sha256']
    report['astChanged'] = fingerprint(old_tree) != fingerprint(new_tree)
    for category in CATEGORIES:
        old_keys = [key for key, _ in old[category]]
        new_keys = [key for key, _ in new[category]]
        if old_keys != new_keys:
            report['changes'].append({
                'category': category,
                'removed': unmatched(old[category], new[category]),
                'added': unmatched(new[category], old[category]),
                'orderChanged': Counter(old_keys) == Counter(new_keys),
            })
    report['exitCode'] = int(report['astChanged'])
    return report


def main():
    use_utf8_output()
    parser = argparse.ArgumentParser(description='Python source differences for review; not equivalence proof.')
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    report = compare(args.before, args.after)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if not report['errors']:
            print(f"textChanged={report['textChanged']} astChanged={report['astChanged']}")
        for change in report['changes']:
            print(f"{change['category']}: orderChanged={change['orderChanged']}")
            for side in ('removed', 'added'):
                for item in change[side]:
                    name = f" {item['name']}" if item['name'] else ''
                    print(f"  {side} {item['kind']}{name} {item['line']}:{item['column']}")
        for error in report['errors']:
            print(f"{error['side']}: {error['type']} {error['path']}:{error['line']}")
        for limitation in report['limitations']:
            print(limitation)
    return report['exitCode']


if __name__ == '__main__':
    raise SystemExit(main())
