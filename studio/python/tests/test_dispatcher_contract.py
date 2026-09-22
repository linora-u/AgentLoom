from __future__ import annotations

import json
from pathlib import Path

import pytest
from agentloom_studio_adapter.dispatcher import (
    StudioAdapterError,
    StudioDispatcher,
)
from agentloom_studio_adapter.domain_cli import main as domain_main


def test_long_lived_and_one_shot_run_detail_share_parameter_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    params = {
        "application_id": "demo",
        "run_id": "missing",
        "system_id": "",
    }

    with pytest.raises(StudioAdapterError) as long_lived_error:
        StudioDispatcher(tmp_path).dispatch("run.detail", params)

    return_code = domain_main(
        [
            "--project",
            str(tmp_path),
            "run.detail",
            json.dumps(params),
        ]
    )
    one_shot = json.loads(capsys.readouterr().out)

    assert return_code == 2
    assert one_shot["error"] == {
        "code": long_lived_error.value.code,
        "message": str(long_lived_error.value),
    }
