from pathlib import Path

import yaml

from src.lib.config import C
from src.lib.logging import get_logger
from src.lib.permissions.policy_summary import build_security_behavior_section
from src.trace.task_context import get_current_agent_config

logger = get_logger(__name__)

# Template remains under prompts/, while this helper now lives under agent/.
_PROMPT_TEMPLATE_PATH = (Path(__file__).parent.parent / "prompts" / "environment_prompt.yaml").resolve()

def get_agent_environment_prompt() -> str:
    try:
        # Add a log line to confirm execution.
        logger.info(f"Generating environment prompt from template: {_PROMPT_TEMPLATE_PATH}")

        # workspace root is always agent_root
        ws_root = Path(C.agent_root).resolve()

        tac_cfg = {}
        agent_cfg = get_current_agent_config()
        if isinstance(agent_cfg, dict):
            maybe_tac = agent_cfg.get("tool_access_control")
            if isinstance(maybe_tac, dict):
                tac_cfg = maybe_tac
        if not tac_cfg:
            fallback = C.get("tool_access_control", {})
            if isinstance(fallback, dict):
                tac_cfg = fallback

        # Collect exclude_paths from all path_validation entries
        exclude_paths: list[str] = []
        pv_rules = tac_cfg.get("path_validation", [])
        if isinstance(pv_rules, list):
            seen: set[str] = set()
            for rule in pv_rules:
                if isinstance(rule, dict):
                    for p in rule.get("exclude_paths", []):
                        if isinstance(p, str) and p.strip() and p not in seen:
                            seen.add(p)
                            exclude_paths.append(p)

        if exclude_paths:
            lines = []
            for p in exclude_paths:
                abs_path = str(ws_root / p)
                lines.append(f"- {abs_path}: [RESTRICTED] System/Internal directory. Access is forbidden.")
            exclude_section = "\n".join(lines)
        else:
            exclude_section = "- (none): [OPEN] No excluded directories configured."

        # Load template
        if not _PROMPT_TEMPLATE_PATH.exists():
            logger.warning(f"Environment prompt template not found at {_PROMPT_TEMPLATE_PATH}")
            return ""
            
        with open(_PROMPT_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            tpl_data = yaml.safe_load(f)
            
        template_str = tpl_data.get('template', '')
        
        # Format
        security_section = build_security_behavior_section()
        return template_str.format(
            workspace_root=str(ws_root),
            exclude_section=exclude_section,
            security_behavior_section=security_section,
        )

    except Exception as e:
        logger.warning(f"Failed to generate environment prompt: {e}")
        return ""
