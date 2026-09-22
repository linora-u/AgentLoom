"""HTTP model boundary for Applications running real smol and Pi runtimes."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Any, Callable

import yaml


@contextmanager
def model_service(program: Callable[[dict[str, Any]], str | list[tuple[str, str, dict]]]):
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
            try:
                answer = program(request)
                message: dict[str, Any] = {'role': 'assistant', 'content': answer if isinstance(answer, str) else None}
                if isinstance(answer, list):
                    message['tool_calls'] = [
                        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
                        for call_id, name, arguments in answer
                    ]
                finish = 'tool_calls' if isinstance(answer, list) else 'stop'
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
        'lsp_servers': {'enabled': False}, 'checkpoint': {'enabled': False},
        'self_learning': {'enabled': False}, 'default_toolsets': [],
        'logging': {'console_enabled': False},
    })
    model = {'adapter': 'openai_chat', 'base_url': url, 'api_key': 'fixture-key',
             'context_window': 32768, 'max_output_tokens': 1000, 'num_retries': 0,
             'timeout': 15, 'requests_per_minute': 2000000}
    write_yaml(root / 'config/llm.yaml', {'model': {'default_model_type': 'supervisor',
        **{role: {**model, 'model': f'openai/{role}'} for role in ['supervisor', 'worker', 'summary']}}})
    workflow = root / 'applications/mixed/workflows/root.yaml'
    write_yaml(workflow, {'name': 'mixed', 'agent_runtime': supervisor, 'model_type': 'supervisor',
        'description': 'Verify repository facts with a Worker.', 'workflow': 'Ask inspect_note to read note.txt and return its token.',
        'tools': [], 'toolsets': [], 'worker_agents': [{'path': 'inspect.yaml'}], 'concurrency': 3})
    write_yaml(workflow.parent / 'worker_agents/inspect.yaml', {
        'name': 'inspect_note', 'agent_runtime': worker, 'model_type': 'worker',
        'description': 'Read one requested file.', 'workflow': 'Read the file named in query and return its exact token.',
        'tools': [{'name': 'read' if worker == 'pi' else 'read_file'}], 'toolsets': [],
        'agent_function_schema': {'description': 'Read a note in an isolated Worker.',
            'inputs': {'query': {'description': 'File to read.', 'required': True}},
            'output': {'description': 'Actual file token.'}},
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
