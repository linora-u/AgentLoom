from smolagents.utils import RateLimiter

from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
from src.lib.smolagents.monkey_patch import install_agentloom_runtime_adapters


def test_runtime_adapters_do_not_patch_global_rate_limiter():
    original = RateLimiter.throttle

    install_agentloom_runtime_adapters()

    assert RateLimiter.throttle is original


def test_litellm_model_v2_disables_upstream_rate_limit_locally():
    model = LiteLLMModelV2(model_id="test/model", requests_per_minute=1)

    assert model._apply_rate_limit() is None
