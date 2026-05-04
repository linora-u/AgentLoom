import sys
import warnings


def _no_truncate_content(content: str, max_length: int = 200000) -> str:
    return content


def disable_smolagents_truncation() -> None:
    """Disable smolagents content truncation globally."""
    try:
        from smolagents import utils

        utils.truncate_content = _no_truncate_content

        patched_count = 0
        modules_to_patch = [module for name, module in sys.modules.items() if name.startswith("smolagents")]
        try:
            from smolagents import local_python_executor

            if local_python_executor not in modules_to_patch:
                modules_to_patch.append(local_python_executor)
        except ImportError:
            pass

        for module in modules_to_patch:
            if hasattr(module, "truncate_content"):
                old_val = getattr(module, "truncate_content")
                if old_val != _no_truncate_content:
                    setattr(module, "truncate_content", _no_truncate_content)
                    patched_count += 1
    except Exception as exc:
        warnings.warn(f"Error patching truncate_content: {exc}", RuntimeWarning)
