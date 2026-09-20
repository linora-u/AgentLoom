"""Application acceptance through the real Pi SDK, with controlled model turns."""
import json

import yaml

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from test_application import model_service, project


def select(app, **values):
    config = yaml.safe_load(app.read_text())
    config.update(values)
    app.write_text(yaml.safe_dump(config))


def test_selected_official_read_commits_before_model_continues(tmp_path):
    (tmp_path / "note.txt").write_text("Native read evidence: saffron-19\n")
    with model_service(turns=[[('read-1', 'read', {'path': 'note.txt'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "read"}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    assert len(requests) == 2
    assert [tool['function']['name'] for tool in requests[0][1]['tools']] == ['read']
    tool_messages = [message for message in requests[1][1]['messages'] if message['role'] == 'tool']
    assert len(tool_messages) == 1
    assert 'saffron-19' in tool_messages[0]['content']
    entries = [json.loads(path.read_text()) for path in (result.run.run_dir / 'native-tools').rglob('*.json')]
    receipts = [entry for entry in entries if entry.get('state') == 'committed']
    assert len(receipts) == 1
    assert receipts[0]['request']['tool']['provider'] == 'pi'
    assert receipts[0]['request']['identity']['call_id'] == 'read-1'
