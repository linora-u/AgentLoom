"""Shell tests conftest — shared fixtures for shell tool tests."""

import pytest


@pytest.fixture(autouse=True)
def _clear_shell_detection_cache():
    """Clear the find_suitable_shell() lru_cache before each test.

    Tests that monkeypatch $SHELL or mock shutil.which need a fresh
    detection run; the per-process cache must not leak across tests.
    """
    from src.tools.shell.process import find_suitable_shell
    find_suitable_shell.cache_clear()
    yield
    find_suitable_shell.cache_clear()


@pytest.fixture
def bypass_shell_security(monkeypatch):
    """Bypass security and path validation checks.

    Use this fixture in tests that verify shell behavior (cwd persistence,
    env isolation, etc.) and do NOT need the security/path validation layer.
    """
    monkeypatch.setattr(
        "src.tools.shell.validator.validate_command_security",
        lambda cmd: None,
    )
    monkeypatch.setattr(
        "src.tools.shell.validator.check_path_constraints",
        lambda cmd, **kwargs: None,
    )
