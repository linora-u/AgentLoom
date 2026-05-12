from __future__ import annotations

import shlex
import signal
import shutil
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from smolagents.models import LiteLLMModel

from src.lib.logging import initialize_global_logger_once, get_global_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent

LLM_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "llm.yaml"
WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "applications" / "test_demo" / "workflows" / "test_docker_real_agent.yaml"
README_HINT_PATH = Path(__file__).resolve().parents[2] / "tests" / "smolagents_test" / "readme.md"
CONTAINER_README_PATH = "/root/sandbox/README.md"

pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:Support for class-based `config` is deprecated, use ConfigDict instead.*:"
        "pydantic.warnings.PydanticDeprecatedSince20"
    )
]

EXTERNAL_CALL_TIMEOUT_SECONDS = 90


@contextmanager
def _fail_after(seconds: int, label: str):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handle_timeout(_signum, _frame):
        raise TimeoutError(f"{label} timed out after {seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _load_model_runtime_from_yaml() -> dict[str, Any] | None:
    if not LLM_CONFIG_PATH.exists():
        return None

    raw = yaml.safe_load(LLM_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    model_section = raw.get("model")
    if not isinstance(model_section, dict):
        return None

    selected_profile = None
    selected_model_id = None
    for profile in ("powerful", "fast", "summary"):
        profile_cfg = model_section.get(profile)
        if isinstance(profile_cfg, dict) and isinstance(profile_cfg.get("model"), str) and profile_cfg["model"].strip():
            selected_profile = profile
            selected_model_id = profile_cfg["model"].strip()
            break

    if selected_model_id is None:
        for key, value in model_section.items():
            if isinstance(value, dict) and isinstance(value.get("model"), str) and value["model"].strip():
                selected_profile = str(key)
                selected_model_id = value["model"].strip()
                break

    if not selected_model_id:
        return None

    selected_cfg = model_section.get(selected_profile) if selected_profile else {}
    if not isinstance(selected_cfg, dict):
        selected_cfg = {}

    return {
        "model_id": selected_model_id,
        "api_base": selected_cfg.get("base_url"),
        "api_key": selected_cfg.get("api_key"),
        "requests_per_minute": selected_cfg.get("requests_per_minute"),
        "timeout": selected_cfg.get("timeout", 120),
        "temperature": selected_cfg.get("temperature", 0.0),
        "max_tokens": selected_cfg.get("max_tokens", 1024),
    }


def _build_live_model(runtime: dict[str, Any]) -> LiteLLMModel:
    bounded_timeout = min(int(runtime["timeout"] or EXTERNAL_CALL_TIMEOUT_SECONDS), EXTERNAL_CALL_TIMEOUT_SECONDS)
    kwargs: dict[str, Any] = {
        "model_id": runtime["model_id"],
        "timeout": bounded_timeout,
        "temperature": runtime["temperature"],
        "max_tokens": min(int(runtime["max_tokens"] or 4096), 4096),
        "num_retries": 0,
        "retry_delay": 0.0,
        "max_retry_delay": 0.0,
    }
    if runtime.get("api_base"):
        kwargs["api_base"] = runtime["api_base"]
    if runtime.get("api_key"):
        kwargs["api_key"] = runtime["api_key"]
    if runtime.get("requests_per_minute") is not None:
        kwargs["requests_per_minute"] = runtime["requests_per_minute"]
    return LiteLLMModel(**kwargs)


def _ensure_docker_and_image_or_skip(image_name: str) -> None:
    hint = f"请先阅读 {README_HINT_PATH}，构建docker后再来测试。"

    if shutil.which("docker") is None:
        pytest.skip(f"docker cli is not available in PATH; {hint}")

    info = subprocess.run(["docker", "info"], text=True, capture_output=True)
    if info.returncode != 0:
        pytest.skip(f"docker daemon is unavailable; {hint}")

    inspect_res = subprocess.run(["docker", "image", "inspect", image_name], text=True, capture_output=True)
    if inspect_res.returncode != 0:
        pytest.skip(f"docker image not found: {image_name}; {hint}")


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _read_file_from_image(image_name: str, file_path: str) -> str:
    cmd = ["docker", "run", "--rm", image_name, "/bin/bash", "-lc", f"cat {shlex.quote(file_path)}"]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"failed to read {file_path} from image {image_name}: {completed.stderr or completed.stdout}"
        )
    return completed.stdout


def _normalize_final_answer_content(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    return text


def _is_known_external_runtime_error(exc: BaseException) -> bool:
    text = str(exc)
    if isinstance(exc, TimeoutError) and "timed out after" in text:
        return True
    if "APITimeoutError" in text or "Request timed out" in text:
        return True
    if "Connection to remote host was lost" in text:
        return True
    if "reasoning_content" in text and "Input should be a valid string" in text:
        return True
    if "Failed to initialize Jupyter kernel" in text and "Connection reset by peer" in text:
        return True
    return False


def test_yaml_execution_env_docker_real_llm_roundtrip():
    runtime = _load_model_runtime_from_yaml()
    if runtime is None:
        pytest.skip("config/llm.yaml missing or no usable model config")

    if not WORKFLOW_PATH.exists():
        pytest.skip(f"workflow missing: {WORKFLOW_PATH}")

    config = YamlAgentFactory._load_config_from_file(WORKFLOW_PATH)

    # Ensure global logger is initialized before agent construction.
    if get_global_logger(create_if_missing=False) is None:
        initialize_global_logger_once(config.get("name", "docker_real_llm_test"))
    execution_env = (config.get("execution_env") or {})
    executor_kwargs = (execution_env.get("executor_kwargs") or {})
    image_name = str(executor_kwargs.get("image_name") or "agentloom-smolagents-jupyter-kernel:local")

    _ensure_docker_and_image_or_skip(image_name)

    port = _pick_free_port()
    config.setdefault("execution_env", {}).setdefault("executor_kwargs", {})["port"] = port
    expected_readme = _read_file_from_image(image_name, CONTAINER_README_PATH)

    supervisor = None
    runtime_agent = None
    try:
        model = _build_live_model(runtime)
        supervisor = YamlConfiguredSupervisorAgent(config=config, model=model)
        runtime_agent = supervisor.build_runtime_agent()

        assert getattr(runtime_agent, "executor_type", None) == "docker"
        assert getattr(runtime_agent, "executor_kwargs", {}).get("image_name") == image_name
        assert getattr(runtime_agent, "executor_kwargs", {}).get("port") == port

        task = f"""
请使用 Python 在当前执行环境里完成以下步骤：
1. 读取文件：{CONTAINER_README_PATH}
2. 获取该文件完整内容
3. 调用 final_answer 返回完整文件内容

注意：
- 最终答案只返回文件内容本身，不要额外解释。
- 不要加 markdown 代码块，不要加引号。
"""
        with _fail_after(EXTERNAL_CALL_TIMEOUT_SECONDS, "supervisor.run"):
            result = supervisor.run(task)
        normalized = _normalize_final_answer_content(result)
        assert normalized == expected_readme.strip()
    except Exception as exc:
        if _is_known_external_runtime_error(exc):
            pytest.skip(f"external runtime instability: {exc}")
        raise
    finally:
        if runtime_agent is not None:
            cleanup_fn = getattr(runtime_agent, "cleanup", None)
            if callable(cleanup_fn):
                cleanup_fn()
