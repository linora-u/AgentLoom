# smolagents Docker Local Run Guide

## English

### 1) Prerequisites

- Docker daemon is running (`docker info` works).
- Repo root is `AgentLoom/`.
- Python uses project venv: `.venv/bin/python` (run from repo root).
- `config/llm.yaml` contains a valid model config.

### 2) Build the test image (required)

Run from repo root:

```bash
docker build \
  -f tests/smolagents_test/docker/jupyter-kernel.Dockerfile \
  -t agentloom-smolagents-jupyter-kernel:local \
  tests/smolagents_test/docker
```

This creates the image used by the integration test.
Build is required before running the container manually.

### 3) Run Docker manually (optional)

Start container:

```bash
docker run --rm --name smolagents-jupyter-local -p 8888:8888 agentloom-smolagents-jupyter-kernel:local
```

If you want to keep the container in background and enter it:

```bash
docker run -d --name smolagents-jupyter-local -p 8888:8888 agentloom-smolagents-jupyter-kernel:local
docker exec -it smolagents-jupyter-local /bin/bash
```

In another terminal, health checks:

```bash
curl -s http://127.0.0.1:8888/api
curl -s -X POST http://127.0.0.1:8888/api/kernels
```

Expected:
- `/api` returns JSON.
- `/api/kernels` returns kernel JSON with `id`.

Stop with `Ctrl+C`.

### 4) Run the real LLM + Docker test

```bash
.venv/bin/python -m pytest -s tests/smolagents_test/test_codeagent_docker_real_llm.py -v -rA
```

What this test does:
- Uses real model config from `config/llm.yaml`.
- Starts `CodeAgent(executor_type="docker")`.
- Creates and reads a file inside Docker container.
- Asserts returned token is correct.
- Builds `agentloom-smolagents-jupyter-kernel:local` from `tests/smolagents_test/docker/jupyter-kernel.Dockerfile` before running the test.

### 4.1) How `executor_kwargs` is passed (from smolagents source)

`executor_kwargs` is not custom glue code in this repo. It is passed by `CodeAgent` directly to the selected executor.

- In `smolagents/agents.py`, `CodeAgent(..., executor_type=..., executor_kwargs=...)` stores this dict and forwards it in `create_python_executor()`.
- For `executor_type="docker"`, kwargs are forwarded to `DockerExecutor(...)`.
- In `smolagents/remote_executors.py`, `DockerExecutor.__init__` supports:
  - `host` (default: `127.0.0.1`)
  - `port` (default: `8888`)
  - `image_name` (default: `jupyter-kernel`)
  - `build_new_image` (default: `True`)
  - `container_run_kwargs` (extra args for `docker.containers.run`)

Important behavior:
- Docker executor always forces `ports["8888/tcp"] = (host, port)` and `detach=True`.
- If you pass unknown keys in `executor_kwargs`, Python constructor will raise `TypeError`.

Minimal example:

```python
agent = CodeAgent(
    tools=[],
    model=model,
    executor_type="docker",
    executor_kwargs={
        "host": "127.0.0.1",
        "port": 8888,
        "image_name": "agentloom-smolagents-jupyter-kernel:local",
        "build_new_image": False,
        "container_run_kwargs": {
            "name": "smolagents-jupyter-local",
            "environment": {"EXAMPLE": "1"},
        },
    },
)
```

### 5) Cleanup (optional)

```bash
docker ps -a
docker images | rg "agentloom-smolagents-jupyter-kernel|jupyter-kernel"
docker rmi agentloom-smolagents-jupyter-kernel:local
```

---

## 中文

### 1）前置条件

- Docker daemon 已启动（`docker info` 可用）。
- 当前仓库根目录是 `AgentLoom/`。
- Python 使用项目虚拟环境：`.venv/bin/python`（在仓库根目录下运行）。
- `config/llm.yaml` 中有可用模型配置。

### 2）构建测试镜像（必需）

在仓库根目录执行：

```bash
docker build \
  -f tests/smolagents_test/docker/jupyter-kernel.Dockerfile \
  -t agentloom-smolagents-jupyter-kernel:local \
  tests/smolagents_test/docker
```

这会创建该集成测试使用的本地镜像。
手动运行容器前必须先构建。

### 3）手动启动 Docker 验证（可选）

启动容器：

```bash
docker run --rm --name smolagents-jupyter-local -p 8888:8888 agentloom-smolagents-jupyter-kernel:local
```

如果你希望容器在后台运行并进入容器：

```bash
docker run -d --name smolagents-jupyter-local -p 8888:8888 agentloom-smolagents-jupyter-kernel:local
docker exec -it smolagents-jupyter-local /bin/bash
```

在另一个终端做健康检查：

```bash
curl -s http://127.0.0.1:8888/api
curl -s -X POST http://127.0.0.1:8888/api/kernels
```

期望结果：
- `/api` 返回 JSON。
- `/api/kernels` 返回包含 `id` 的 kernel JSON。

按 `Ctrl+C` 停止容器。

### 4）运行真实 LLM + Docker 用例

```bash
.venv/bin/python -m pytest -s tests/smolagents_test/test_codeagent_docker_real_llm.py -v -rA
```

该测试会：
- 读取 `config/llm.yaml` 的真实模型配置。
- 启动 `CodeAgent(executor_type="docker")`。
- 在 Docker 容器内创建并读取文件。
- 断言返回 token 正确。
- 在运行测试前，会从 `tests/smolagents_test/docker/jupyter-kernel.Dockerfile` 构建 `agentloom-smolagents-jupyter-kernel:local`。

### 4.1）`executor_kwargs` 是怎么传的（基于 smolagents 源码）

`executor_kwargs` 不是这个仓库里自己拼装的私有参数，而是 `CodeAgent` 官方透传给执行器的参数。

- 在 `smolagents/agents.py` 里，`CodeAgent(..., executor_type=..., executor_kwargs=...)` 会保存这个 dict，并在 `create_python_executor()` 里直接转发。
- 当 `executor_type="docker"` 时，这些参数会传到 `DockerExecutor(...)`。
- 在 `smolagents/remote_executors.py` 中，`DockerExecutor.__init__` 支持这些字段：
  - `host`（默认 `127.0.0.1`）
  - `port`（默认 `8888`）
  - `image_name`（默认 `jupyter-kernel`）
  - `build_new_image`（默认 `True`）
  - `container_run_kwargs`（透传到 `docker.containers.run` 的额外参数）

需要注意：
- Docker 执行器会强制设置 `ports["8888/tcp"] = (host, port)` 和 `detach=True`。
- `executor_kwargs` 里如果塞了不支持的字段，会在构造器阶段抛 `TypeError`。

最小示例：

```python
agent = CodeAgent(
    tools=[],
    model=model,
    executor_type="docker",
    executor_kwargs={
        "host": "127.0.0.1",
        "port": 8888,
        "image_name": "agentloom-smolagents-jupyter-kernel:local",
        "build_new_image": False,
        "container_run_kwargs": {
            "name": "smolagents-jupyter-local",
            "environment": {"EXAMPLE": "1"},
        },
    },
)
```

### 5）清理（可选）

```bash
docker ps -a
docker images | rg "agentloom-smolagents-jupyter-kernel|jupyter-kernel"
docker rmi agentloom-smolagents-jupyter-kernel:local
```
