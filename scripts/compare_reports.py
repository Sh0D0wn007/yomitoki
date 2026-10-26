import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from check import ACCEPTABLE_RULES, DEFAULT_LIMITS, use_utf8_output

METRICS = ('lines', 'params', 'depth', 'complexity')
STATUS_MESSAGES = {
    'no-increase': '比較完了: 指摘件数・最大指標の増加なし（既存の指摘が残る場合があります）。',
    'increase-notice': '増加通知: 指摘件数または最大指標が増加しました。検査の実行失敗ではありません。可読性への影響を確認してください。',
    'not-comparable': '比較不能: 比較条件が揃っていません。件数の減少を改善と判定できません。',
    'error': '比較未完了: 検査エラー・対象なし・レポートの読み込みまたは形式の問題があります。',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def is_integer(value, minimum=0):
    return type(value) is int and value >= minimum


def is_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate_accepted(report, paths):
    for key in ('accepted', 'unusedAccepted'):
        require(isinstance(report.get(key, []), list), f'{key}は配列である必要があります')
    for item in report.get('accepted', []):
        require(isinstance(item, dict) and item.get('path') in paths, '許容した指摘が検査済みファイルに属していません')
        require(item.get('tool', 'ast') == 'ast' and item.get('rule') in ACCEPTABLE_RULES, '許容できるのは構文指標の指摘だけです')
        require(is_integer(item.get('line'), 1) and is_integer(item.get('column'), 1), '許容した指摘の位置が不正です')
        require(is_integer(item.get('actual')) and is_integer(item.get('limit'), 1), '許容した指摘の実測値または閾値が不正です')
        require(is_integer(item.get('acceptedActual'), 1) and item['actual'] <= item['acceptedActual'], '許容時の実測値を超えた指摘が許容されています')
        require(isinstance(item.get('reason'), str) and item['reason'].strip(), '許容の理由がありません')


def validate_report(report):
    require(isinstance(report, dict), 'レポートはJSONオブジェクトである必要があります')
    require(report.get('schemaVersion') == 1, '非対応のschemaVersionです')
    require(report.get('kind') in ('yomitoki', 'yomitoki-suite'), '非対応のレポート種別です。現行スクリプトで再生成してください')
    require(isinstance(report.get('pythonVersion'), str), 'pythonVersionがありません')
    require(is_digest(report.get('checkerSha256')), 'checkerSha256がありません。現行スクリプトで再生成してください')
    limits = report.get('limits')
    require(isinstance(limits, dict) and set(limits) == set(DEFAULT_LIMITS), '閾値の形式が不正です')
    require(all(is_integer(value, 1) for value in limits.values()), '閾値は正の整数である必要があります')
    for key in ('files', 'functions', 'issues', 'errors', 'skipped'):
        require(isinstance(report.get(key), list), f'{key}は配列である必要があります')
        require(all(isinstance(item, dict) for item in report[key]), f'{key}の項目が不正です')
    paths = set()
    for file in report['files']:
        path = file.get('path')
        require(isinstance(path, str) and Path(path).is_absolute(), 'ファイルパスが不正です')
        require(path not in paths and is_digest(file.get('sha256')), 'ファイルの重複またはハッシュ不備があります')
        paths.add(path)
    require(is_integer(report.get('filesChecked')), 'filesCheckedが不正です')
    require(report['filesChecked'] == len(paths), 'filesCheckedとファイル一覧が一致しません')
    for item in report['issues']:
        require(item.get('path') in paths, '指摘が検査済みファイルに属していません')
        require(isinstance(item.get('rule'), str) and isinstance(item.get('tool', 'ast'), str), '指摘のルールが不正です')
        require(is_integer(item.get('line'), 1) and is_integer(item.get('column'), 1), '指摘の位置が不正です')
        if item.get('tool', 'ast') == 'ast':
            require(is_integer(item.get('actual')) and is_integer(item.get('limit'), 1), '実測値または閾値が不正です')
    for item in report['functions']:
        require(item.get('path') in paths and isinstance(item.get('name'), str), '関数情報が不正です')
        require(is_integer(item.get('line'), 1), '関数の位置が不正です')
        require(all(is_integer(item.get(metric)) for metric in METRICS), '関数の測定値が不正です')
    for item in report['skipped']:
        require(isinstance(item.get('path'), str) and isinstance(item.get('reason'), str), '除外情報が不正です')
    validate_accepted(report, paths)
    expected = 2 if report['errors'] else int(bool(report['issues']))
    require(type(report.get('exitCode')) is int and report['exitCode'] == expected, '終了コードと結果が一致しません')
    if report['kind'] == 'yomitoki-suite':
        ruff = report.get('ruff')
        require(isinstance(ruff, dict) and isinstance(ruff.get('profile'), dict), 'Ruff設定がありません')
        require(is_digest(ruff.get('runnerSha256')), 'Ruff実行スクリプトのハッシュがありません')
        require(is_integer(ruff.get('filesChecked')), 'Ruffの検査件数が不正です')
        python_paths = [path for path in paths if path.endswith('.py')]
        if not report['errors'] and python_paths:
            require(isinstance(ruff.get('version'), str) and ruff['version'].startswith('ruff '), 'Ruffのバージョンがありません')
        if not report['errors']:
            require(ruff['filesChecked'] == len(python_paths), 'Ruffで未検査のファイルがあります')


def findings(report):
    # Accepted findings still count, so accepting a finding is never reported as a reduction.
    return report['issues'] + report.get('accepted', [])


def rule_counts(report):
    return Counter((item['path'], item.get('tool', 'ast'), item['rule']) for item in findings(report))


def file_metrics(report):
    result = {file['path']: dict.fromkeys((*METRICS, 'functionCount'), 0) for file in report['files']}
    for function in report['functions']:
        metrics = result[function['path']]
        metrics['functionCount'] += 1
        for metric in METRICS:
            metrics[metric] = max(metrics[metric], function[metric])
    return result


def exclusions(report):
    return {(item['path'], item['reason']) for item in report['skipped'] if item['reason'] != 'unsupported-extension'}


def delta(before, after):
    return {'before': before, 'after': after, 'delta': after - before}


def compare(before, after):
    validate_report(before)
    validate_report(after)
    result = {
        'schemaVersion': 1, 'comparable': True, 'conditionChanges': [], 'conditionDetails': {}, 'errors': [],
        'counts': delta(len(findings(before)), len(findings(after))),
        'ruleChanges': [], 'metricChanges': [],
    }
    for key in ('kind', 'pythonVersion', 'checkerSha256', 'parsers', 'limits'):
        if before.get(key) != after.get(key):
            result['conditionChanges'].append(key)
            result['conditionDetails'][key] = {'before': before.get(key), 'after': after.get(key)}
    if before['kind'] == after['kind'] == 'yomitoki-suite':
        for key in ('version', 'profile', 'runnerSha256'):
            if before['ruff'][key] != after['ruff'][key]:
                result['conditionChanges'].append('ruff.' + key)
                result['conditionDetails']['ruff.' + key] = {'before': before['ruff'][key], 'after': after['ruff'][key]}
    before_files = {file['path'] for file in before['files']}
    after_files = {file['path'] for file in after['files']}
    result['removedFiles'] = sorted(before_files - after_files)
    result['addedFiles'] = sorted(after_files - before_files)
    if result['removedFiles'] or result['addedFiles']:
        result['conditionChanges'].append('files')
    if exclusions(before) != exclusions(after):
        result['conditionChanges'].append('exclusions')
    for label, report in [('before', before), ('after', after)]:
        if report['errors'] or not report['filesChecked']:
            result['errors'].append(f'{label}: 検査エラーまたは検査対象なし')
    result['comparable'] = not result['conditionChanges'] and not result['errors']
    if 'accepted' in before or 'accepted' in after:
        result['acceptedCounts'] = delta(len(before.get('accepted', [])), len(after.get('accepted', [])))

    old_counts, new_counts = rule_counts(before), rule_counts(after)
    for key in sorted(old_counts.keys() | new_counts.keys()):
        if old_counts[key] != new_counts[key]:
            result['ruleChanges'].append({'path': key[0], 'tool': key[1], 'rule': key[2], **delta(old_counts[key], new_counts[key])})
    old_metrics, new_metrics = file_metrics(before), file_metrics(after)
    for path in sorted(before_files & after_files):
        changes = {'path': path}
        for metric in (*METRICS, 'functionCount'):
            if old_metrics[path][metric] != new_metrics[path][metric]:
                changes[metric] = delta(old_metrics[path][metric], new_metrics[path][metric])
        if len(changes) > 1:
            result['metricChanges'].append(changes)
    increased = any(item['delta'] > 0 for item in result['ruleChanges'])
    increased |= any(item.get(metric, {}).get('delta', 0) > 0 for item in result['metricChanges'] for metric in METRICS)
    result['exitCode'] = int(increased) if result['comparable'] else 2
    if result['errors']:
        result['status'] = 'error'
    elif not result['comparable']:
        result['status'] = 'not-comparable'
    else:
        result['status'] = 'increase-notice' if increased else 'no-increase'
    result['message'] = STATUS_MESSAGES[result['status']]
    return result


def print_comparison(result):
    print(result['message'])
    if result['comparable']:
        print('同じ検査条件で比較しました（コードの挙動は検証していません）。')
    for error in result['errors']:
        print('ERROR ' + error)
    for key in result.get('conditionChanges', []):
        print('条件変更: ' + key)
        detail = result.get('conditionDetails', {}).get(key)
        if detail:
            print('  ' + json.dumps(detail['before'], ensure_ascii=False) + ' → ' + json.dumps(detail['after'], ensure_ascii=False))
    for label in ('removedFiles', 'addedFiles'):
        for path in result.get(label, []):
            print(f'{label}: {path}')
    if 'counts' in result:
        counts = result['counts']
        print(f"指摘数: {counts['before']} → {counts['after']} ({counts['delta']:+d})")
    if 'acceptedCounts' in result:
        counts = result['acceptedCounts']
        print(f"許容済み: {counts['before']} → {counts['after']} ({counts['delta']:+d})")
    for item in result.get('ruleChanges', []):
        print(f"{item['path']} {item['tool']}/{item['rule']}: {item['before']} → {item['after']} ({item['delta']:+d})")
    for item in result.get('metricChanges', []):
        for key, value in item.items():
            if key != 'path':
                print(f"{item['path']} {key}: {value['before']} → {value['after']} ({value['delta']:+d})")


def main():
    use_utf8_output()
    parser = argparse.ArgumentParser(description='検査レポートの条件・範囲・指摘数・関数指標を比較します。')
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        # Windows PowerShell 5.1 saves redirected output as UTF-16; json detects UTF-8/16/32 and BOMs from bytes.
        before = json.loads(args.before.read_bytes())
        after = json.loads(args.after.read_bytes())
        result = compare(before, after)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        result = {'schemaVersion': 1, 'comparable': False, 'errors': [str(error)], 'exitCode': 2,
                  'status': 'error', 'message': STATUS_MESSAGES['error']}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_comparison(result)
    return result['exitCode']


if __name__ == '__main__':
    sys.exit(main())
