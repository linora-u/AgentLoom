"""Controlled refresh subprocess reached through the public Application entry."""
import json
import os
import sys
from pathlib import Path


def main():
    from agentloom.app.harnessing.controller import run_application
    from agentloom.app.harnessing.models import ApplicationRequest
    from agentloom.app.harnessing.policy import HarnessingPolicy
    from agentloom.config.config import load_project_config
    from agentloom.runtimes.pi import environment, install
    from tests.application_test.mixed_runtime_support import project

    case = Path(sys.argv[1])
    root = case / "project"
    definition = project(root, "http://127.0.0.1:9/v1", supervisor="smolagents", worker="smolagents")
    login = case / "login"
    login.mkdir()
    (login / "auth.json").write_text(json.dumps({"openai-codex": {
        "type": "oauth", "access": "DUMMY-ACCESS", "refresh": "DUMMY-REFRESH", "expires": 0}}))
    os.environ["PI_CODING_AGENT_DIR"] = str(login)
    helper = case / "refresh-helper"
    helper.write_text("#!" + sys.executable + "\n" + '''
import json,os,subprocess,sys,time
from pathlib import Path
import psutil
child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'],
    start_new_session=True, env={})
Path(''' + repr(str(case / "refresh.json")) + ''').write_text(json.dumps({
    'pid':os.getpid(),'creation':psutil.Process().create_time(),
    'child_pid':child.pid,'child_creation':psutil.Process(child.pid).create_time()}))
time.sleep(120)
''')
    helper.chmod(0o700)
    environment.build_pi_subprocess_env = lambda: dict(os.environ)
    install.find_node = lambda _env: str(helper)
    install.installed_pi_entry = lambda **_kwargs: case / "package/bin/pi.mjs"
    catalog = load_project_config(root).llm.model_dump(mode="json")
    catalog["models"]["subscription"] = {"model": "unused", "adapter": "openai_codex_responses"}
    run_application(ApplicationRequest("mixed", root, definition.parents[1],
        "agentloom.app.harnessing.supervisor:SupervisorDriver",
        driver_options={"definition": "workflows/root.yaml", "file_logging": False}),
        policy=HarnessingPolicy(enabled=True), model_config=catalog, state_root=case / "experiments")


if __name__ == "__main__":
    main()
