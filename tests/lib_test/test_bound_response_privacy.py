"""Generated data, including opaque nested keys, is fenced before persistence."""
import pytest
from agentloom.config.config import bind_harness_model_config
from agentloom.config.private_bindings import bind_harness_credentials
from agentloom.execution.model_privacy import reject_private_response, safe_provider_error
from agentloom.execution.model_protocol import ModelResponseRejected
from agentloom.runtimes.pi.credentials import bind_pi_credentials


@pytest.mark.parametrize("generated", [
    {"choices": [{"message": {"content": "secret-x"}}]},
    {"choices": [{"message": {"reasoning_content": "secret-x"}}]},
    {"choices": [{"message": {"tool_calls": [{"function": {"arguments": {"id": "secret-x"}}}]}}]},
    {"content": [{"type": "tool_use", "input": {"type": "secret-x"}}]},
    {"output": [{"type": "function_call", "arguments": {"secret-x": {"status": "test"}}}]},
    {"output": [{"type": "function_call", "id": "secret-x", "arguments": "{}"}]},
    {"model": "secret-x", "choices": [{"message": {"content": "OK"}}]},
    {"type": "secret-x", "choices": [{"message": {"content": "OK"}}]},
    {"usage": {"provider_detail": "secret-x"}},
    {"choices": [{"message": {"tool_calls": [{"function": {"arguments": '{"id":"\\u0073ecret-x"}'}}]}}]},
    {"error": {"message": "secret-x"}},
])
def test_wire_envelopes_do_not_hide_generated_nested_keys(generated):
    with bind_harness_credentials({"capability": "secret-x"}):
        with pytest.raises(ModelResponseRejected) as rejected:
            reject_private_response({**generated, "usage": {"prompt_tokens": 9, "completion_tokens": 4,
                "total_tokens": 13, **generated.get("usage", {})}})
    assert rejected.value.usage.total_tokens == 13
    assert set(rejected.value.usage.details) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert "secret-x" not in str(rejected.value)


def test_private_scope_covers_short_model_keys_tools_oauth_and_restores_ordinary_mode():
    with bind_harness_credentials({"mcp": "tool-secret"}), bind_harness_model_config({
        "model": {"default": {"adapter": "openai_chat", "api_key": "x", "model": "openai/x"}}}), bind_pi_credentials({
        "provider": {"type": "oauth", "access": "access-secret", "refresh": "refresh-secret"}}):
        for secret in ("x", "tool-secret", "access-secret", "refresh-secret"):
            with pytest.raises(ModelResponseRejected):
                reject_private_response({"choices": [{"message": {"content": secret}}]})
        # A known protocol model identity isn't generated content.
        reject_private_response({"model": "x", "choices": [{"message": {"role": "assistant", "content": "OK"}}]}, model="openai/x")
        error = safe_provider_error(ValueError("provider said access-secret"))
        assert isinstance(error, ModelResponseRejected)
        assert error.__cause__ is None
    reject_private_response({"choices": [{"message": {"content": "access-secret"}}]})


def test_safe_provider_error_drops_private_nested_cause():
    with bind_harness_credentials({"capability": "nested-cause-secret"}):
        try:
            try:
                raise ValueError("nested-cause-secret")
            except ValueError as cause:
                raise RuntimeError("Provider failed") from cause
        except RuntimeError as error:
            safe = safe_provider_error(error)
    assert isinstance(safe, ModelResponseRejected)
    assert safe.__cause__ is None and safe.__context__ is None
