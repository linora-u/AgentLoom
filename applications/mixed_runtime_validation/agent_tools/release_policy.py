"""A read-only domain tool with code-owned, narrowly scoped memory evidence."""
from __future__ import annotations

import json
from pathlib import Path

from agentloom.configuration import C
from agentloom.execution.trusted_memory_evidence import trusted_memory_evidence


def _policy_evidence(result: str):
    policy = json.loads(result)
    return [{'kind': 'durable_fact', 'scope': 'application',
             'source': 'validated-release-policy', 'text': policy['fact']}]


@trusted_memory_evidence(_policy_evidence)
def read_release_policy() -> str:
    """Read the application's validated export format from its policy fixture."""
    path = Path(C.agent_root) / 'applications/mixed_runtime_validation/fixtures/release-policy.json'
    policy = json.loads(path.read_text())
    # This tool declares trust only for a checked domain fact, never arbitrary
    # file text or instructions chosen by the model.
    if set(policy) != {'export_format'} or policy['export_format'] not in {'PIPE', 'CSV', 'TSV'}:
        raise ValueError('Unsupported release policy')
    return json.dumps({'fact': f"Release export format is {policy['export_format']}.",
                       'source': 'release-policy.json'})
