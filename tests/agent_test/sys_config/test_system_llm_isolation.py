"""验证 system 配置域和 LLM 配置域的隔离不变量。

直接测试 _filter_llm_only_top_level_keys 函数和集合常量，
确保两个配置域之间不存在交叉泄露。
"""

from src.lib.config.config import (
    _LLM_ONLY_TOP_LEVEL_KEYS,
    _WORKFLOW_OVERLAY_KEYS,
    _filter_llm_only_top_level_keys,
)


# ─── 测试：_filter_llm_only_top_level_keys 边界条件 ───


def test_filter_returns_empty_on_none():
    """传入 None 时应返回空字典。"""
    result = _filter_llm_only_top_level_keys(None, source_name="test")
    assert result == {}


def test_filter_returns_empty_on_empty_dict():
    """传入空字典时应返回空字典。"""
    result = _filter_llm_only_top_level_keys({}, source_name="test")
    assert result == {}


def test_filter_removes_all_llm_only_keys():
    """所有 _LLM_ONLY_TOP_LEVEL_KEYS 中定义的键应被移除。"""
    input_data = {
        "model": {"default_model_type": "powerful"},
        "summary": {"model": "openai/test-summary"},
        "llm": {"some_config": True},
        "langfuse": {"enabled": True},
        "system": {"name": "test"},
        "logging": {"level": "INFO"},
    }
    result = _filter_llm_only_top_level_keys(input_data, source_name="test")

    # LLM 键全部被移除
    for key in _LLM_ONLY_TOP_LEVEL_KEYS:
        assert key not in result, f"LLM key '{key}' should have been filtered out"

    # 非 LLM 键全部保留
    assert result["system"] == {"name": "test"}
    assert result["logging"] == {"level": "INFO"}


def test_filter_preserves_unknown_keys():
    """不在 _LLM_ONLY_TOP_LEVEL_KEYS 中的自定义键应原样保留。"""
    input_data = {
        "model": {"ignored": True},
        "custom_key": "custom_value",
        "another_key": {"nested": True},
    }
    result = _filter_llm_only_top_level_keys(input_data, source_name="test")

    assert "model" not in result
    assert result["custom_key"] == "custom_value"
    assert result["another_key"] == {"nested": True}


# ─── 测试：集合不变量守卫 ───


def test_workflow_overlay_and_llm_only_are_disjoint():
    """_WORKFLOW_OVERLAY_KEYS 和 _LLM_ONLY_TOP_LEVEL_KEYS 不能有交集。

    这是 system/llm 隔离设计的核心不变量：
    - _WORKFLOW_OVERLAY_KEYS 定义了允许在 system.yaml / agent YAML 中出现的键
    - _LLM_ONLY_TOP_LEVEL_KEYS 定义了只能在 llm.yaml 中出现的键
    两者交叉意味着隔离被破坏。
    """
    intersection = _WORKFLOW_OVERLAY_KEYS & _LLM_ONLY_TOP_LEVEL_KEYS
    assert intersection == set(), (
        f"Isolation violation: these keys appear in both sets: {intersection}"
    )


def test_llm_only_keys_contains_expected_members():
    """_LLM_ONLY_TOP_LEVEL_KEYS 必须至少包含 model, llm, langfuse。

    作为防回归守卫，确保核心 LLM 键不会被意外移除。
    """
    expected = {"model", "llm", "langfuse"}
    assert expected.issubset(_LLM_ONLY_TOP_LEVEL_KEYS), (
        f"Missing expected LLM-only keys: {expected - _LLM_ONLY_TOP_LEVEL_KEYS}"
    )
