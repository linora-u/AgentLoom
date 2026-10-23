"""Subscription capabilities cross upstream threads through real Pi execution."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy
from agentloom.runtimes.pi.credentials import bind_pi_credentials, current_pi_credentials
from agentloom.runtimes.pi.install import find_node

from tests.application_test.mixed_runtime_support import project, write_yaml
from tests.application_test.test_harness_controller_application import DRIVER
from tests.application_test.test_harness_controller_application import real_isolation as real_isolation

pytestmark = pytest.mark.usefixtures("real_isolation")


def test_parallel_complete_task_preserves_private_subscription_binding(tmp_path, monkeypatch):
    # Intercept only the external provider boundary. Native SDK credential
    # resolution, tools, Applications and isolated method processes stay real.
    bootstrap = """
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  if (request.url.startsWith('https://auth.openai.com/'))
    return new Response(JSON.stringify({error:'fixture_login_denied'}), {status:401});
  if (request.url !== 'https://chatgpt.com/backend-api/codex/responses')
    throw new Error('Unexpected external fixture request');
  if (request.headers.get('authorization') !== 'Bearer ' + __EXPECTED_ACCESS__)
    throw new Error('Another task supplied this subscription capability');
  const {zstdDecompressSync} = await import('node:zlib');
  let body = Buffer.from(await request.arrayBuffer());
  if (request.headers.get('content-encoding') === 'zstd') body = zstdDecompressSync(body);
  const payload = JSON.parse(body.toString());
  if (JSON.stringify(payload).includes('fixture-private')) throw new Error('Credential in model input');
  const prompt = JSON.stringify(payload);
  const role = prompt.includes('# YOUR DIMENSION: INV') ? 'general'
    : prompt.includes('# YOUR DIMENSION: DOM') ? 'domain'
    : prompt.includes('You are composing') || prompt.includes('You are the integration judge and synthesizer.') ? 'compose'
    : prompt.includes('# THE CANDIDATES ARE ANONYMOUS ON PURPOSE') ? 'select'
    : prompt.includes('You are reading the complete record of one improvement round') ? 'reflect' : 'business';
  const outputs = payload.input.filter(item => item.type === 'function_call_output');
  let answer = role === 'general' ? '<notes>GENERAL-NOTE: inspected the frozen task.</notes>'
    : role === 'domain' ? '<notes>DOMAIN-NOTE: preserved the fixed fact.</notes>'
    : role === 'compose' ? '<notes>revised -> prompt -> observed original fact.</notes>'
    : role === 'select' ? '<why>Required frozen fact.</why>\\n```choice\\nA\\n```'
    : role === 'reflect' ? 'NO_ENTRY'
    : JSON.stringify({fact:'FACT-123',variant:prompt.includes('COMPOSED')?'COMPOSED':'BASE'});
  let call;
  if (['general','domain'].includes(role) && outputs.length === 0)
    call = {name:'read',arguments:{path:'context/task.md'}};
  if (role === 'compose' && outputs.length < 2)
    call = outputs.length === 0 ? {name:'read',arguments:{path:'context/harness/applications/mixed/prompts/business.md'}}
      : {name:'write',arguments:{path:'context/harness/applications/mixed/prompts/business.md',content:'COMPOSED'}};
  const item = call ? {type:'function_call',id:'fc_1',call_id:'fixture_call',name:call.name,arguments:JSON.stringify(call.arguments)}
    : {type:'message',id:'msg_1',role:'assistant',status:'completed',content:[{type:'output_text',text:answer,annotations:[]}]};
  const events = [
    {type:'response.created',response:{id:'resp_1',model:payload.model,status:'in_progress',output:[]}},
    {type:'response.output_item.added',output_index:0,item:call?{...item,arguments:''}:{...item,content:[]}},
    ...(call ? [{type:'response.function_call_arguments.delta',item_id:'fc_1',output_index:0,delta:item.arguments}]
      : [{type:'response.content_part.added',item_id:'msg_1',output_index:0,content_index:0,part:{type:'output_text',text:'',annotations:[]}},
         {type:'response.output_text.delta',item_id:'msg_1',output_index:0,content_index:0,delta:answer}]),
    {type:'response.output_item.done',output_index:0,item},
    {type:'response.completed',response:{id:'resp_1',model:payload.model,status:'completed',output:[item],
      usage:{input_tokens:50,output_tokens:20,total_tokens:70,input_tokens_details:{cached_tokens:0}}}},
  ];
  return new Response(events.map(event=>`event: ${event.type}\\ndata: ${JSON.stringify(event)}\\n\\n`).join(''),
    {status:200,headers:{'content-type':'text/event-stream'}});
};
"""
    from agentloom.runtimes.pi.environment import build_pi_subprocess_env
    node = find_node(build_pi_subprocess_env())
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "absent-login-store"))
    root = tmp_path / "project"
    project(root, "http://127.0.0.1:1/v1", supervisor="pi", worker="pi")
    profile = {"adapter": "openai_codex_responses", "model": "gpt-6-luna", "timeout": 10,
               "num_retries": 0, "context_window": 272000, "max_output_tokens": 256,
               "supports_structured_output": True, "web_search": "off"}
    write_yaml(root / "config/model.yaml", {"model": {
        "default_model_type": "worker", "worker": profile}})
    app = root / "applications/mixed"
    write_yaml(app / "config/model.yaml", {"model": {
        "default_model_type": "improvement", "improvement": profile}})
    (app / "driver.py").write_text(DRIVER)
    (app / "prompts").mkdir()
    (app / "prompts/business.md").write_text("BASE")
    write_yaml(app / "workflows/worker.yaml", {"name": "business", "agent_runtime": "pi",
        "model_type": "worker", "description": "Return the frozen fact.", "task": "ORIGINAL_TASK: return fact.",
        "system_prompt": {"path": "prompts/business.md"}, "tools": [], "toolsets": [],
        "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"fact": {"type": "string"}, "variant": {"type": "string"}},
                          "required": ["fact", "variant"], "additionalProperties": False}})
    fact, hidden = tmp_path / "fact.txt", tmp_path / "heldout.txt"
    fact.write_text("FACT-123")
    hidden.write_text("HIDDEN-CANARY")
    monkeypatch.syspath_prepend(str(root))
    for key in tuple(sys.modules):
        if key.startswith("applications.mixed"):
            del sys.modules[key]
    request = ApplicationRequest("mixed", root, app, "applications.mixed.driver:Driver", task="ORIGINAL_TASK",
        input_paths=(fact,), hidden_paths=(hidden,), driver_options={"delivery": str(tmp_path / "delivery.json")})
    account = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"}}).encode()).decode().rstrip("=")
    def complete(namespace):
        access = f"fixture-private-{namespace}.{account}.signature"
        private = {"openai-codex": {"type": "oauth", "access": access,
                                  "refresh": "", "expires": int(time.time() * 1000) + 3600_000}}
        launcher = tmp_path / namespace / "bin/node"
        launcher.parent.mkdir(parents=True)
        module = "data:text/javascript;base64," + base64.b64encode(
            bootstrap.replace("__EXPECTED_ACCESS__", json.dumps(access)).encode()).decode()
        launcher.write_text(f"#!{sys.executable}\nimport os,sys\nargs=sys.argv[1:]\n"
                            f"binary={node!r}\nmodule={module!r}\n"
                            "if any(arg.endswith('/pi/bridge/dist/index.js') for arg in args):\n"
                            " args=['--import',module,*args]\n"
                            "os.execv(binary,[binary,*args])\n")
        launcher.chmod(0o755)
        assert current_pi_credentials() is None
        try:
            with bind_pi_credentials(private):
                result = run_application(replace(request, driver_options={
                    "delivery": str(tmp_path / namespace / "delivery.json")}),
                    policy=HarnessingPolicy.model_validate({"enabled": True}),
                    state_root=tmp_path / "experiments" / namespace,
                    environment={"PATH": str(launcher.parent) + os.pathsep + os.environ["PATH"]})
                assert current_pi_credentials() == private
                assert result.status == "completed", result.to_dict()
                assert len(result.trials) == 4 and all(trial.accepted for trial in result.trials), result.to_dict()
                assert {role["role"] for role in result.roles} >= {"general", "domain", "compose"}
                assert all(role["status"] == "completed" for role in result.roles), result.to_dict()
                assert result.output["variant"] == "COMPOSED"
        finally:
            assert current_pi_credentials() is None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(complete, ("first", "second")))
    for path in (tmp_path / "experiments").rglob("*"):
        if path.is_file():
            assert b"fixture-private" not in path.read_bytes(), path
