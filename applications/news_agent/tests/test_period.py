"""The same frozen list determines both coverage and primary scoring."""

from news_agent.tests.source_evidence import verified_sources
from datetime import date, timedelta
from hashlib import sha256
import json

import pytest

from news_agent import baseline
from news_agent.evaluation import period
from news_agent.settings import load_baseline_settings


def _row(entry, code='510300.SH', direction='利好', value=.02, event='e1'):
    return {'event_id': event, 'record_ids': ['N'+event], 'summary': event, 'reason': 'verified original',
        'entry_date': entry, 'exit_date': entry, 'etf_code': code, 'direction': direction,
        'return': value, 'return_decimal': str(value) if value is not None else None,
        'direction_hit': False, 'relative_return': .001 if value is not None else None,
        'benchmark_return': .002, 'exclusion': None if value is not None else 'ETF 行情缺失',
        'overnight_gap': .01, 'entry_day_return': -.005,
        'high_open_low_close': True, 'low_open_high_close': False}


def _day(root, day, rows):
    folder = root/'reports/baseline'
    folder.mkdir(parents=True, exist_ok=True)
    config = load_baseline_settings()
    (folder/f'{day}.json').write_text(json.dumps({'hold_days': 3, 'sell_at': 'open',
        'limits': {key: str(value) for key, value in config.thresholds.items()}, 'selected_records': len(rows)}))
    (folder/f'{day}-details.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    prepared = root/'days'/day
    prepared.mkdir(parents=True, exist_ok=True)
    (prepared/'manifest.json').write_text(json.dumps({'raw_count': 10, 'prepared_count': 10, 'batches': [{}]}))
    review = root/'results/reviews'/f'{day}.json'
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(json.dumps({'reviews': [], 'mapping_signals': [{k: row[k] for k in
        ('event_id','record_ids','summary','reason','etf_code','direction')} for row in rows]}))
    (root/'results'/f'{day}.jsonl').write_text(''.join(json.dumps({k: row[k] for k in
        ('event_id','record_ids','summary','reason','etf_code','direction')})+'\n' for row in rows))


def _freeze(root, entries, rows_by_day):
    days = {}
    for entry in entries:
        matching = [(day, row) for day, rows in rows_by_day.items() for row in rows if row['entry_date'] == entry]
        per_code = {}
        for day, row in matching:
            net = per_code.setdefault(row['etf_code'], {'signal_id': entry+':'+row['etf_code'],
                'entry_date': entry, 'etf_code': row['etf_code'], 'direction': row['direction'],
                'reason': 'source-backed net assessment', 'event_ids': [row['event_id']], 'mapping_refs': []})
            net['mapping_refs'].append({'day': day, 'event_id': row['event_id'],
                                       'etf_code': row['etf_code'], 'direction': row['direction']})
        nets = list(per_code.values())
        formal = nets
        days[entry] = {'formal_signals': formal, 'all_net_signals': nets,
            'countable_signals': len(formal),
            'reported_events': len(matching), 'bearish_priority_etfs': []}
    path = period._entry_audit_path(root, entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'format': period.AUDIT_FORMAT, 'entry_dates': entries,
        'prediction_hashes': {str((root/'results'/f'{day}.jsonl').resolve()): sha256((root/'results'/f'{day}.jsonl').read_bytes()).hexdigest() for day in rows_by_day},
        'method_fingerprint': baseline._model_signature(), 'counting_unit': 'entry_date_x_etf', 'days': days,
        'formal_list_sha256': period._audit_digest(days)}))


def test_weekend_one_formal_etf_retains_all_mappings(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    rows = {'2025-08-01': [_row('2025-08-04')], '2025-08-02': [_row('2025-08-04')]}
    for day, part in rows.items(): _day(tmp_path, day, part)
    _freeze(tmp_path, ['2025-08-04'], rows)
    result = period.score(tmp_path, date(2025,8,1), date(2025,8,2), entry_dates=['2025-08-04'])
    assert result['event_etf_rows'] == result['all_mapping_signals']['signals'] == 2
    assert result['directional_signals']['signals'] == 1
    assert result['directional_signals']['hits'] == 1
    assert result['directional_signals']['prediction_coverage_complete']
    assert result['review_status'] == 'pending'


def test_main_denominator_keeps_each_distinct_etf_in_frozen_list(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    rows = {'2025-08-01': [_row('2025-08-04', value=-.03), _row('2025-08-04', code='510310.SH', value=.04)]}
    _day(tmp_path, '2025-08-01', rows['2025-08-01'])
    _freeze(tmp_path, ['2025-08-04'], rows)
    result = period.score(tmp_path, date(2025,8,1), date(2025,8,1), entry_dates=['2025-08-04'])
    assert result['directional_signals']['signals'] == result['directional_signals']['countable_signals'] == 2
    assert result['directional_signals']['hit_rate'] == .5
    assert result['all_mapping_signals']['hit_rate'] == .5
    assert result['all_net_signals']['signals'] == 2


def test_missing_formal_list_does_not_fall_back_to_old_scoring(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    _day(tmp_path, '2025-08-01', [_row('2025-08-04')])
    result = period.score(tmp_path, date(2025,8,1), date(2025,8,1), entry_dates=['2025-08-04'])
    assert result['directional_signals']['signals'] == 0
    assert not result['directional_signals']['prediction_coverage_complete']
    assert result['directional_signals']['missing_entry_audit_dates'] == ['2025-08-04']
    assert result['directional_signals']['zero_signal_dates'] == []


def test_complete_day_without_signals_is_reported_without_quota_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    rows = {'2025-08-01': []}
    _day(tmp_path, '2025-08-01', [])
    _freeze(tmp_path, ['2025-08-04'], rows)
    result = period.score(tmp_path, date(2025,8,1), date(2025,8,1), entry_dates=['2025-08-04'])
    assert result['directional_signals']['prediction_coverage_complete']
    assert result['directional_signals']['zero_signal_dates'] == ['2025-08-04']
    assert result['directional_signals']['missing_entry_audit_dates'] == []
    assert result['review_status'] == 'pending'


def test_missing_price_remains_in_frozen_denominator(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    rows = {'2025-08-01': [_row('2025-08-04', value=None)]}
    _day(tmp_path, '2025-08-01', rows['2025-08-01'])
    _freeze(tmp_path, ['2025-08-04'], rows)
    result = period.score(tmp_path, date(2025,8,1), date(2025,8,1), entry_dates=['2025-08-04'])
    assert result['directional_signals']['signals'] == 1
    assert result['directional_signals']['hit_rate'] == 0
    assert not result['directional_signals']['scoring_complete']
    assert result['directional_signals']['unscored_cases'][0]['exclusion'] == 'ETF 行情缺失'


def test_both_directions_report_results_and_opening_reversals_are_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    start = date(2025,8,1)
    rows_by_day = {}
    entries = []
    counts = {'利好': 0, '利空': 0}
    for offset in range(21):
        day = start + timedelta(days=offset)
        entry = str(day+timedelta(days=1))
        entries.append(entry)
        rows = []
        for position, direction in enumerate(['利好','利空','利空' if offset%2==0 else '利好']):
            hit = counts[direction] < 24
            counts[direction] += 1
            value = (.02 if direction == '利好' else -.015) if hit else (.0199 if direction == '利好' else -.0149)
            rows.append(_row(entry, code=f'{500000+offset*3+position}.SH', direction=direction, value=value, event=f'e{offset}-{position}'))
        _day(tmp_path, str(day), rows)
        rows_by_day[str(day)] = rows
    _freeze(tmp_path, entries, rows_by_day)
    result = period.score(tmp_path, start, start+timedelta(days=20), entry_dates=entries)
    assert result['directional_signals']['bullish']['hit_rate'] == 24/31
    assert result['directional_signals']['bearish']['hit_rate'] == .75
    assert result['directional_signals']['scoring_complete']
    assert result['review_status'] == 'pending'
    assert result['directional_signals']['bullish']['opening_reversal_cases']
    # The inclusive bearish boundary is a hit; barely above it is a failure.
    path = tmp_path/'reports/baseline'/f'{start}-details.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1].update({'return': -.0149, 'return_decimal': '-0.0149', 'low_open_high_close': True})
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result = period.score(tmp_path, start, start+timedelta(days=20), entry_dates=entries)
    assert result['directional_signals']['bearish']['hit_rate'] == 23/32
    assert result['directional_signals']['scoring_complete']
    assert result['review_status'] == 'pending'


def test_changed_formal_list_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(period, 'verify_evaluation_artifacts', lambda *_: None)
    rows = {'2025-08-01': [_row('2025-08-04')]}
    _day(tmp_path, '2025-08-01', rows['2025-08-01'])
    _freeze(tmp_path, ['2025-08-04'], rows)
    path = period._entry_audit_path(tmp_path, ['2025-08-04'])
    audit = json.loads(path.read_text()); audit['days']['2025-08-04']['formal_signals'] = []
    path.write_text(json.dumps(audit))
    with pytest.raises(ValueError, match='正式名单'):
        period.score(tmp_path, date(2025,8,1), date(2025,8,1), entry_dates=['2025-08-04'])
