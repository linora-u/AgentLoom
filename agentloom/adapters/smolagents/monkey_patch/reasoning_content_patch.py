"""
Patch litellm's _extract_reasoning_content to handle non-string reasoning_content.

Some LLM gateway proxies (e.g. LiFE) return `reasoning_content: []` (empty list)
instead of `null` or a string for AWS Bedrock Claude models.  LiteLLM's Message
pydantic model expects `Optional[str]`, so the raw list causes a ValidationError.

This patch normalises the value **before** it reaches the Message constructor:
  - empty list / falsy non-string  →  None
  - non-empty list                 →  joined string
  - string                         →  unchanged
"""

import warnings


def patch_litellm_reasoning_content() -> None:
    """Monkey-patch ``_extract_reasoning_content`` to sanitise non-string values."""
    try:
        from litellm.litellm_core_utils.prompt_templates import common_utils
        from litellm.litellm_core_utils.llm_response_utils import (
            convert_dict_to_response,
        )

        _original = common_utils._extract_reasoning_content

        # Guard against double-patching.
        if getattr(_original, "_agentloom_patched", False):
            return

        def _safe_extract_reasoning_content(message: dict):
            reasoning, content = _original(message)

            # Normalise reasoning_content to Optional[str].
            if reasoning is not None and not isinstance(reasoning, str):
                if isinstance(reasoning, list):
                    # Empty list → None; non-empty → join text elements.
                    texts = [str(item) for item in reasoning]
                    reasoning = " ".join(texts) if texts else None
                else:
                    reasoning = str(reasoning) if reasoning else None

            return reasoning, content

        _safe_extract_reasoning_content._agentloom_patched = True  # type: ignore[attr-defined]

        # Patch both the source module AND any module that already imported
        # the function via ``from ... import _extract_reasoning_content``.
        common_utils._extract_reasoning_content = _safe_extract_reasoning_content
        convert_dict_to_response._extract_reasoning_content = (
            _safe_extract_reasoning_content
        )

    except Exception as exc:
        warnings.warn(
            f"Failed to patch litellm reasoning_content handling: {exc}",
            RuntimeWarning,
        )
