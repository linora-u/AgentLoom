"""Environment policy shared by Pi installation and bridge processes."""
from __future__ import annotations

from agentloom.execution.subprocess_env import build_subprocess_env
from agentloom.execution.process_scope import PI_PROCESS_SCOPE_ENV, managed_process_group


_INHERITED_NODE_ENV = frozenset({"NODE_USE_ENV_PROXY", "NODE_EXTRA_CA_CERTS"})


def build_pi_subprocess_env() -> dict[str, str]:
    """Use configured HTTP proxies unless Node proxy handling is explicitly set."""
    env = {
        name: value
        for name, value in build_subprocess_env().items()
        if name in _INHERITED_NODE_ENV or not name.startswith(("PI_", "NODE_"))
    }
    # This flag comes from the trusted host binding, never an inherited user
    # environment value. SDK native bash can then join the same managed group.
    env.pop(PI_PROCESS_SCOPE_ENV, None)
    group = managed_process_group()
    if group is not None:
        env[PI_PROCESS_SCOPE_ENV] = str(group)
    if "NODE_USE_ENV_PROXY" not in env and any(
        env.get(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
    ):
        env["NODE_USE_ENV_PROXY"] = "1"
    return env
