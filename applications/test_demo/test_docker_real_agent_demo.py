#!/usr/bin/env python3
"""Real agent docker demo with local readme-guidance fallback."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.lib.logging import resolve_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent

DEFAULT_YAML_PATH = Path(__file__).parent / "workflows" / "test_docker_real_agent.yaml"
README_GUIDE_PATH = Path(__file__).resolve().parents[2] / "tests" / "smolagents_test" / "readme.md"
CONTAINER_README_PATH = "/root/sandbox/README.md"
DEFAULT_DOCKER_IMAGE = "agentloom-smolagents-jupyter-kernel:local"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _check_docker_ready(image_name: str) -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker cli 不可用"

    info = subprocess.run(["docker", "info"], text=True, capture_output=True)
    if info.returncode != 0:
        details = info.stderr.strip() or info.stdout.strip() or "docker info failed"
        return False, f"docker daemon 不可用: {details}"

    inspect_cmd = ["docker", "image", "inspect", image_name]
    inspect_res = subprocess.run(inspect_cmd, text=True, capture_output=True)
    if inspect_res.returncode != 0:
        return False, f"docker 镜像不存在: {image_name}"

    return True, "docker ready"


def _docker_task() -> str:
    return f"""
请使用 Python 在当前执行环境里完成以下步骤：
1. 读取文件：{CONTAINER_README_PATH}
2. 获取该文件完整内容
3. 调用 final_answer 返回完整文件内容

注意：
- 最终答案只返回文件内容本身，不要额外解释。
- 不要加 markdown 代码块，不要加引号。
"""


def _readme_guidance_task(reason: str) -> str:
    return f"""
当前 Docker 环境不可用，原因：{reason}

请先读取：{README_GUIDE_PATH}
你可以使用 read_file_content 工具，或者直接使用 Python 文件读取。
然后严格基于该文档给出以下内容：
1. 在当前仓库如何构建 docker 镜像（必须给出可直接执行的命令）。
2. 构建完成后如何重试本 demo（给出完整命令）。
3. 排错顺序（docker cli / daemon / image 三步检查）。

输出要求：
- 用中文，条目化输出。
- 命令要可直接复制执行。
- 不要编造 readme 中不存在的镜像名和路径。
"""


def run_demo(task_content: str | None = None, yaml_path: Path = DEFAULT_YAML_PATH) -> None:
    if not yaml_path.exists():
        raise FileNotFoundError(f"YAML not found: {yaml_path}")

    config = YamlAgentFactory._load_config_from_file(yaml_path)
    execution_env = config.setdefault("execution_env", {})
    executor_kwargs = execution_env.setdefault("executor_kwargs", {})

    image_name = str(executor_kwargs.get("image_name") or DEFAULT_DOCKER_IMAGE)
    docker_ready, reason = _check_docker_ready(image_name)

    if docker_ready:
        port = _pick_free_port()
        execution_env["type"] = "docker"
        executor_kwargs["port"] = port
        selected_task = task_content or _docker_task()
        print(f"docker ready: true, image={image_name}, port={port}")
    else:
        config["execution_env"] = {"type": "local", "executor_kwargs": {}}
        selected_task = task_content or _readme_guidance_task(reason)
        print("docker ready: false")
        print(f"reason: {reason}")
        print(f"guide: read {README_GUIDE_PATH}")

    resolve_logger(None, __name__).info("docker real agent demo start")
    supervisor = YamlConfiguredSupervisorAgent(config=config, logger=None)

    if os.getenv("AGENT_LOOM_DEMO_NO_RUN") == "1":
        print("skip run: true")
        return

    print("skip run: false")
    result = supervisor.run(selected_task)
    print("run result:")
    print(result)


if __name__ == "__main__":
    run_demo(yaml_path=DEFAULT_YAML_PATH.resolve())
