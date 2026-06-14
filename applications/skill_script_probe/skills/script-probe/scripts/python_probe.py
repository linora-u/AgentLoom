from pathlib import Path
import os

workspace = Path(os.environ["AGENTLOOM_SKILL_WORKSPACE"])
(workspace / "python_probe.txt").write_text("python-ok\n", encoding="utf-8")
print("python-ok")
