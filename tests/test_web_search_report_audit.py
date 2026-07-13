import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "applications"
    / "web_search"
    / "scripts"
    / "audit_us_after_close_reports.py"
)
SPEC = importlib.util.spec_from_file_location("audit_us_after_close_reports", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

_hosts_from_url = MODULE._hosts_from_url
_is_example_host = MODULE._is_example_host


@pytest.mark.parametrize("host", ["example.com", "news.example.com"])
def test_is_example_host_accepts_only_example_domain(host: str) -> None:
    assert _is_example_host(host)


@pytest.mark.parametrize("host", ["notexample.com", "example.com.evil.test"])
def test_is_example_host_rejects_substring_matches(host: str) -> None:
    assert not _is_example_host(host)


def test_hosts_from_url_normalizes_hostname() -> None:
    value = "[T1](https://user:password@News.Example.com.:8443/article)"

    assert _hosts_from_url(value) == ["news.example.com"]
