import sys
import warnings


def _no_truncate_content(content: str, max_length: int = 200000) -> str:
    return content


def disable_smolagents_truncation() -> None:
    """Disable smolagents content truncation globally."""
    try:
        from smolagents import utils

        utils.truncate_content = _no_truncate_content

        for name, module in tuple(sys.modules.items()):
            if not name.startswith("smolagents"):
                continue
            if hasattr(module, "truncate_content"):
                old_val = module.truncate_content
                if old_val != _no_truncate_content:
                    module.truncate_content = _no_truncate_content
    except Exception as exc:
        warnings.warn(
            f"Error patching truncate_content: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
