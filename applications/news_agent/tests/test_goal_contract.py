"""Regressions for the frozen user contract, independent of model outcomes."""

from decimal import Decimal

import pytest

from news_agent.evaluation.baseline import actual_level
from news_agent.settings import load_baseline_settings
from news_agent.tests.test_entry_signals import _fixture, _signal


@pytest.mark.parametrize('value, expected', [
    ('0.0200', '利好'), ('0.0199', None),
    ('-0.0150', '利空'), ('-0.0149', None),
])
def test_main_hit_thresholds_are_inclusive(value, expected):
    assert actual_level(Decimal(value), load_baseline_settings().thresholds) == expected


def test_one_event_can_have_opposite_directions_on_distinct_etfs(tmp_path, monkeypatch):
    rows = [_signal('2025-08-01', 'a', '510300.SH'),
            {**_signal('2025-08-01', 'a', '510310.SH'), 'direction': '利空'}]
    item = _fixture(tmp_path, monkeypatch, rows)
    assert item['countable_signals'] == 2
    assert item['countable_bullish_signals'] == item['countable_bearish_signals'] == 1


def test_initial_selection_requires_a_reason_for_every_rejected_id():
    from news_agent.baseline import _checked_initial_ids
    batch = {'file': 'batch.txt', 'ids': ['N1', 'N2']}
    with pytest.raises(ValueError, match='逐条'):
        _checked_initial_ids({'selected_ids': ['N1'], 'rejected': []}, batch)
    assert _checked_initial_ids({'selected_ids': ['N1'],
        'rejected': [{'reason_code': '行情复述', 'record_ids': ['N2']}]}, batch) == ['N1']
