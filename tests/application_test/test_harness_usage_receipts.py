from agentloom.app.harnessing.usage import runtime_usage
from agentloom.app.harnessing.store import ExperimentStore


def test_actual_runtime_attempts_keep_cumulative_counts_failed_usage_and_unknown(tmp_path):
    def event(kind, instance, attempt, sequence, **details):
        return {'kind': kind, 'run_id': 'shared-root', 'details': {
            'instance_id': instance, 'attempt_id': attempt, 'sequence': sequence, **details}}
    observations = runtime_usage([
        event('run', 'root', 'first', 0, phase='started'),
        event('usage', 'root', 'first', 1, input_tokens=10, output_tokens=4, total_tokens=14, usage_complete=True),
        event('usage', 'root', 'first', 1, input_tokens=10, output_tokens=4, total_tokens=14, usage_complete=True),
        event('terminal', 'root', 'first', 2, state='failed'),
        event('run', 'child', 'other', 0, phase='started'),
        event('usage', 'child', 'other', 1, input_tokens=5, output_tokens=3, total_tokens=8, usage_complete=True),
        event('terminal', 'child', 'other', 2, state='success'),
        event('run', 'root', 'retry', 0, phase='started'),
        event('terminal', 'root', 'retry', 1, state='failed'),
    ])
    assert len(observations) == 3
    first = next(item for item in observations if item.attempt_id == 'first')
    assert first.total_tokens == 14 and not first.complete
    retry = next(item for item in observations if item.attempt_id == 'retry')
    assert retry.total_tokens is None and not retry.complete
    store = ExperimentStore(tmp_path / 'state')
    try:
        assert store.observe_usage(observations)['total_tokens'] == 22
        assert store.observe_usage(observations)['total_tokens'] == 22
        assert store.observe_usage(observations)['unknown_invocations'] == 2
        assert not store.observe_usage(observations)['complete']
    finally:
        store.close()
