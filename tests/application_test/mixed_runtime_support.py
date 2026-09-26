"""HTTP model boundary for Applications running real smol and Pi runtimes."""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Any, NamedTuple

import yaml


class ModelReply(NamedTuple):
    content: str
    finish_reason: str


@contextmanager
def model_service(
    program: Callable[
        [dict[str, Any]],
        str | ModelReply | list[tuple[str, str, dict]],
    ],
    *,
    fail_requests: set[int] | None = None,
):
    requests: list[dict[str, Any]] = []
    errors: list[Exception] = []
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                requests.append(request)
                number = len(requests)
            if number in (fail_requests or set()):
                self.send_error(500, 'Fixture provider failure')
                return
            try:
                answer = program(request)
                finish_override = None
                if isinstance(answer, ModelReply):
                    answer, finish_override = answer
                if self.path.endswith('/responses'):
                    output = ([
                        {'type': 'function_call', 'id': f'fc_{index}', 'call_id': call_id,
                         'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}
                        for index, (call_id, name, arguments) in enumerate(answer)
                    ] if isinstance(answer, list) else [
                        {'type': 'message', 'id': 'msg_1', 'role': 'assistant',
                         'status': 'completed', 'content': [
                             {'type': 'output_text', 'text': answer, 'annotations': []},
                         ]},
                    ])
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'id': 'resp_fixture', 'object': 'response', 'created_at': 1,
                        'model': request['model'], 'status': 'completed', 'output': output,
                        'usage': {'input_tokens': 50, 'output_tokens': 20, 'total_tokens': 70},
                    }).encode())
                    return
                message: dict[str, Any] = {'role': 'assistant', 'content': answer if isinstance(answer, str) else None}
                if isinstance(answer, list):
                    message['tool_calls'] = [
                        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
                        for call_id, name, arguments in answer
                    ]
                finish = finish_override or ('tool_calls' if isinstance(answer, list) else 'stop')
                self.send_response(200)
                if request.get('stream'):
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    delta = {**message}
                    if 'tool_calls' in delta:
                        delta['tool_calls'] = [{'index': i, **call} for i, call in enumerate(delta['tool_calls'])]
                    for chunk in [
                        {'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                         'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70}},
                    ]:
                        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'id': 'fixture-completion', 'object': 'chat.completion',
                        'created': 1, 'model': request['model'],
                        'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                        'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70}}).encode())
            except Exception as exc:
                errors.append(exc)
                self.send_error(500, 'Fixture program failed')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        if errors:
            raise AssertionError(f'Model fixture failed: {errors[0]}') from errors[0]


def write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True))


def project(root: Path, url: str, *, supervisor: str, worker: str) -> Path:
    write_yaml(root / 'config/system.yaml', {
        'runtime': {'root_dir': str(root / 'runtime')},
        'checkpoint': {'enabled': False}, 'self_learning': {'enabled': False},
        'default_toolsets': [],
        'logging': {'console_enabled': False},
    })
    model = {'adapter': 'openai_chat', 'base_url': url, 'api_key': 'fixture-key',
             'context_window': 32768, 'max_output_tokens': 1000, 'num_retries': 0,
             'timeout': 15, 'requests_per_minute': 2000000,
             'supports_structured_output': True}
    write_yaml(root / 'config/llm.yaml', {'model': {'default_model_type': 'supervisor',
        **{role: {**model, 'model': f'openai/{role}'} for role in ['supervisor', 'worker', 'summary']}}})
    workflow = root / 'applications/mixed/workflows/root.yaml'
    write_yaml(workflow, {'name': 'mixed', 'agent_runtime': supervisor, 'model_type': 'supervisor',
        'description': 'Verify repository facts with a Worker.', 'task': 'Ask inspect_note to read note.txt and return its token.',
        'tools': [], 'toolsets': [], 'worker_agents': [{'path': 'inspect.yaml'}], 'concurrency': 3})
    write_yaml(workflow.parent / 'worker_agents/inspect.yaml', {
        'name': 'inspect_note', 'agent_runtime': worker, 'model_type': 'worker',
        'description': 'Read one requested file.', 'task': 'Read the file named in query and return its exact token.',
        'tools': [{'name': 'read' if worker == 'pi' else 'read_file'}], 'toolsets': [],
        'input_schema': {
            'type': 'object',
            'properties': {'query': {'type': 'string', 'description': 'File to read.'}},
            'required': ['query'],
            'additionalProperties': False,
        },
    })
    return workflow


def finish(request: dict, answer: str):
    names = {tool['function']['name'] for tool in request.get('tools', [])}
    return [('finish', 'final_answer', {'answer': answer})] if 'final_answer' in names else answer


def tool_messages(request: dict) -> list[dict]:
    return [message for message in request['messages'] if message['role'] == 'tool']


def runtime_events(result) -> list[dict]:
    return [json.loads(line) for line in (result.run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]


parallel_gate: Barrier | None = None


def inspect_invocation(label: str, synchronize: bool = False) -> str:
    """Expose this invocation's bound identities for the isolation oracle.

    Args:
        label: File token returned by the real base tool.
        synchronize: Wait for the other concurrent Worker.
    """
    from agentloom.execution import get_current_run_context
    from agentloom.execution.trace import capture_explicit_execution_context
    context = get_current_run_context(required=True)
    assert context is not None
    execution = capture_explicit_execution_context()
    assert execution.hook_run is not None
    if synchronize:
        assert parallel_gate is not None
        parallel_gate.wait(timeout=8)
    return json.dumps({'label': label, 'run_id': context.run_id, 'task_id': execution.task_id,
        'application_id': context.application_id, 'instance_id': execution.agent_id,
        'root_run_id': execution.root_run_id, 'local_run_id': execution.local_run_id,
        'hook_run_id': execution.hook_run.local_run_id})


def read_context_fixture(file_path: str) -> str:
    """Read one text fixture for runtime-neutral ContextRef validation."""
    return Path(file_path).read_text()
