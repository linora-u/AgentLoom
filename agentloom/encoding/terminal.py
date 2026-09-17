import os
import sys
import io

def configure_terminal_encoding() -> None:
    """
    Configures the terminal encoding for standard streams (stdout, stderr) to UTF-8.
    It sets environment variables to notify child processes of the encoding.
    """
    if sys.platform.startswith("win") and os.environ.get("FORCE_UTF8_CONSOLE") != "1":
        pass
    else:
        # Modern Python (3.7+) way to reconfigure standard streams safely
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except Exception:
                pass
        elif hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
            
        if hasattr(sys.stderr, 'reconfigure'):
            try:
                sys.stderr.reconfigure(encoding='utf-8')
            except Exception:
                pass
        elif hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

    # Set environment variables to notify child processes/tools to use UTF-8.
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # Only set LANG on non-Windows systems, as Windows uses different locale settings
    if not sys.platform.startswith("win"):
        current_lang = os.environ.get("LANG", "")
        if "utf-8" not in current_lang.lower() and "utf8" not in current_lang.lower():
            if not current_lang:
                os.environ["LANG"] = "zh_CN.UTF-8"
            elif '.' in current_lang:
                base_lang = current_lang.split('.')[0]
                os.environ["LANG"] = f"{base_lang}.UTF-8"
            else:
                os.environ["LANG"] = f"{current_lang}.UTF-8"
