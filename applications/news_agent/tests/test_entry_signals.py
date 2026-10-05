"""Exercise entry-day fact deduplication without any market prices."""

from news_agent.tests.source_evidence import verified_sources
from dataclasses import replace
from datetime import date
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from news_agent import baseline
from news_agent.evaluation import period
from news_agent.settings import load_baseline_settings


def _fixture(tmp_path, monkeypatch, signals):
    prices = tmp_path / 'prices' / 'ts_code=510300.SH' / 'data.parquet'
    prices.parent.mkdir(parents=True)
    # A date-only file proves the coverage audit cannot read prices/returns.
    pq.write_table(pa.table({'trade_date': ['20250801', '20250804']}), prices)
    config = replace(load_baseline_settings(), prices=prices.parents[1])
    monkeypatch.setattr(period, 'load_baseline_settings', lambda: config)
    monkeypatch.setattr(baseline, '_model_signature', lambda: 'fixture')
    monkeypatch.setattr(baseline, '_call_worker', lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError('entry decisions must not call another Agent')))
    folder = tmp_path / 'results'
    folder.mkdir()
    for stem in ('2025-08-01', '2025-08-02', '2025-08-03'):
        (folder / f'{stem}.jsonl').write_text(''.join(
            json.dumps({k: v for k, v in row.items() if k != 'day'}) + '\n'
            for row in signals if row['day'] == stem))
    for stem in ('2025-08-01', '2025-08-02', '2025-08-03'):
        fact_rows = [row for row in signals if row['day'] == stem]
        review_dir = tmp_path/'results/reviews'
        review_dir.mkdir(exist_ok=True)
        reviews = [{'event_id': row['event_id'], 'etf_code': row['etf_code'],
            'record_ids': row['record_ids'], 'summary': row['summary'],
            'decision': row['direction'], 'reason': row['reason'],  'stage': 'chunk', "sources": verified_sources(stem)} for row in fact_rows]
        (review_dir/f'{stem}.json').write_text(json.dumps({'reviews': reviews, 'mapping_signals': [{k: v for k, v in row.items() if k != 'day'} for row in fact_rows]}))
        folder = tmp_path/'days'/stem
        folder.mkdir(parents=True)
        records = [{'record_id': identifier, 'record_ids': [identifier], 'text': row['summary']}
                   for row in fact_rows for identifier in row['record_ids']]
        (folder/'records.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records))
    result = period.build_entry_audit(tmp_path, date(2025, 8, 1), date(2025, 8, 3),
                                      ['2025-08-04'])
    return result['days']['2025-08-04']


def _signal(day, event, etf):
    return {'day': day, 'event_id': event, 'etf_code': etf, 'direction': '利好',
            'summary': event, 'record_ids': ['N' + event], 'reason': 'original source'}


def test_one_event_can_count_each_distinct_etf(tmp_path, monkeypatch):
    rows = [_signal('2025-08-01', 'a', code)
            for code in ('510300.SH', '510310.SH', '510320.SH')]
    item = _fixture(tmp_path, monkeypatch, rows)
    assert item['countable_signals'] == len(item['formal_signals']) == 3
    assert item['reported_events'] == 1
    assert [row['etf_code'] for row in item['formal_signals']] == [
        '510300.SH', '510310.SH', '510320.SH']


def test_weekend_facts_merge_once_per_entry_etf(tmp_path, monkeypatch):
    rows = [_signal('2025-08-01', 'a', '510300.SH'),
            _signal('2025-08-02', 'b', '510300.SH'),
            _signal('2025-08-03', 'c', '510310.SH')]
    item = _fixture(tmp_path, monkeypatch, rows)
    assert item['countable_signals'] == 2
    assert item['reported_events'] == 3


def test_opposite_directions_on_same_entry_etf_cannot_count_as_two_signals(tmp_path, monkeypatch):
    rows = [_signal('2025-08-01', 'a', '510300.SH'),
            {**_signal('2025-08-02', 'b', '510300.SH'), 'direction': '利空'},
            _signal('2025-08-03', 'c', '510310.SH')]
    item = _fixture(tmp_path, monkeypatch, rows)
    assert item['countable_signals'] == 2
    assert item['bearish_priority_etfs'] == ['510300.SH']
    assert next(row for row in item['formal_signals'] if row['etf_code'] == '510300.SH')['direction'] == '利空'


def test_direction_metrics_preserve_failed_and_missing_bearish_signals():
    rows = [dict(entry_date='2025-08-04', direction='利空', return_=-.02,
                 direction_hit=True, exclusion=None),
            dict(entry_date='2025-08-05', direction='利空', return_=None,
                 direction_hit=None, exclusion='缺行情'),
            dict(entry_date='2025-08-06', direction='利空', return_=.01,
                 direction_hit=False, exclusion=None)]
    for row in rows:
        row['return'] = row.pop('return_')
    result = period.direction_metrics(rows)
    assert result['signals'] == 3
    assert result['scored_signals'] == 2
    assert result['hits'] == 1
    assert result['hit_rate'] == 1 / 3
    assert result['mean_signed_return'] == pytest.approx(.005)
    assert result['worst_adverse_move'] == -.01
