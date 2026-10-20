"""Linux's actual anonymous capability descriptors stay sealed on Python 3.14."""
import os
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Validated Linux anonymous descriptor backend")
@pytest.mark.parametrize("kind", ["harness", "pi"])
def test_private_descriptors_require_real_kernel_seals_without_python_constant_names(monkeypatch, kind):
    import fcntl
    from contextlib import ExitStack

    from agentloom.config.private_bindings import bind_harness_credentials, forward_harness_bindings
    from agentloom.runtimes.pi.credentials import bind_pi_credentials, pi_credential_descriptor

    for name in ("F_ADD_SEALS", "F_GET_SEALS", "F_SEAL_WRITE", "F_SEAL_GROW", "F_SEAL_SHRINK", "F_SEAL_SEAL"):
        monkeypatch.delattr(fcntl, name, raising=False)
    with ExitStack() as stack:
        if kind == "harness":
            stack.enter_context(bind_harness_credentials({"fixture": "dummy-private"}))
            _, descriptors = stack.enter_context(forward_harness_bindings())
            descriptor, = descriptors
        else:
            stack.enter_context(bind_pi_credentials({"fixture": {"type": "oauth", "access": "dummy-private"}}))
            descriptor = stack.enter_context(pi_credential_descriptor())
        assert fcntl.fcntl(descriptor, 1034) == 0x000F  # F_GET_SEALS, Linux UAPI.
        with pytest.raises(PermissionError):
            os.write(descriptor, b"replace")
        with pytest.raises(PermissionError):
            os.ftruncate(descriptor, 0)
