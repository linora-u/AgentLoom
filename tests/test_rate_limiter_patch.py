from agentloom.integrations.litellm.litellm_retry import create_retry_wrapper
from agentloom.runtimes.smolagents.monkey_patch import install_agentloom_runtime_adapters
from smolagents.utils import RateLimiter


def test_runtime_adapters_do_not_patch_global_rate_limiter() -> None:
    original = RateLimiter.throttle

    install_agentloom_runtime_adapters()

    assert RateLimiter.throttle is original


def test_retry_wrapper_consumes_model_type_before_provider_call() -> None:
    observed: dict = {}

    def provider(**kwargs):
        observed.update(kwargs)
        return "ok"

    wrapped = create_retry_wrapper(provider)

    assert wrapped(
        model="opaque-model",
        _agent_loom_model_type="powerful",
        num_retries=0,
    ) == "ok"
    assert "_agent_loom_model_type" not in observed
