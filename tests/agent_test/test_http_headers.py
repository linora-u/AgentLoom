from __future__ import annotations

import pytest
from agentloom.configuration.http_headers import normalize_http_headers


def test_explicit_model_headers_do_not_add_identity_or_system_defaults() -> None:
    assert normalize_http_headers(None) == {}
    assert normalize_http_headers({"X-Route": "first", "x-route": "second"}) == {
        "x-route": "second"
    }


@pytest.mark.parametrize(
    "headers",
    [
        {"Bad:Name": "value"},
        {"X-Name\nInjected": "value"},
        {"X-Name": "value\r\nInjected"},
    ],
)
def test_explicit_model_headers_reject_invalid_http_lines(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="HTTP header"):
        normalize_http_headers(headers)
