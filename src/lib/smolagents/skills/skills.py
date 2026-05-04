"""Skills manager — loads skills, registers hooks, builds prompts.

Data classes, parsing logic, and hook executors live in sibling modules
(``parser`` and ``executors``) to keep this file focused on orchestration.
"""

import os
from typing import Dict, Any, List, Optional
from pathlib import Path

from smolagents import AgentLogger

from src.lib.logging import get_logger
from ..hooks.hook_manager import HookManager
from ..hooks.types import HookEvent

# Re-export data classes so existing ``from .skills import …`` still works.
from .parser import (  # noqa: F401
    Skill,
    SkillContent,
    SkillMetadata,
    SKILLS_PROMPT,
    parse_skill_file,
    parse_invocation_control,
    build_skills_prompt,
)
from .executors import (  # noqa: F401
    SUPPORTED_HOOK_ACTION_TYPE,
    create_hook_executor,
    validate_hook,
)

class SkillsManager:
    """Manages loading and activation of skills.

    Parsing is delegated to :mod:`.parser` and hook execution to
    :mod:`.executors`.  This class is responsible for discovery, registration,
    tools-mapping, and prompt generation.
    """

    _instance: Optional['SkillsManager'] = None

    def __init__(
        self,
        logger: Optional[AgentLogger] = None,
        hook_manager: Optional[HookManager] = None,
    ):
        self.skills: Dict[str, Skill] = {}
        self.hook_manager = hook_manager if hook_manager is not None else HookManager.get_instance()
        self._logger = get_logger(logger, __name__)
        self._tools_mapping: Dict[str, Dict[str, str]] = {}

    # -- Logger / mapping helpers -------------------------------------------

    def set_logger(self, logger: Optional[AgentLogger]):
        self._logger = get_logger(logger, __name__)

    def set_tools_mapping(self, mapping: Dict[str, Dict[str, str]]):
        self._tools_mapping = mapping

    def get_tool_mapping_for_skill(self, skill_name: str) -> Dict[str, str]:
        skill = self.skills.get(skill_name)
        if not skill:
            return {}
        platform = skill.metadata.platform or 'Claude'
        return self._tools_mapping.get(platform, {})

    def get_skill(self, skill_name: str) -> Optional[Skill]:
        return self.skills.get(skill_name)

    @classmethod
    def get_instance(cls, logger: Optional[AgentLogger] = None):
        if cls._instance is None:
            cls._instance = cls(logger=logger)
        elif logger is not None:
            cls._instance.set_logger(logger)
        return cls._instance

    # -- Discovery ----------------------------------------------------------

    def load_skills_from_directory(
        self,
        directory: str,
        platform: Optional[str] = None,
        invocation_control: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Load skills from direct subdirectories using skill.md/skills.md."""
        path = Path(directory)
        if not path.exists():
            self._logger.warning(f"Skills directory not found: {directory}")
            return []
        if not path.is_dir():
            self._logger.warning(f"Skills directory is not a directory: {directory}")
            return []

        loaded_names = []
        for file_path in path.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.name.lower() not in {"skill.md", "skills.md"}:
                continue
            skill = self.load_skill_metadata(
                str(file_path), platform=platform,
                invocation_control=invocation_control,
            )
            if skill:
                loaded_names.append(skill.metadata.name)
        return loaded_names

    def load_skill_metadata(
        self,
        file_path: str,
        platform: Optional[str] = None,
        invocation_control: Optional[Dict[str, Any]] = None,
    ) -> Optional[Skill]:
        """Load skill metadata only (no content, no hooks).

        Parameters
        ----------
        invocation_control:
            Parsed invocation-control dict from the reference site
            (Agent YAML / system.yaml).  When *None* the default
            ``{"allow-model": True, "allow-hook": True}`` is used.
        """
        try:
            metadata, _markdown_body = parse_skill_file(file_path, logger=self._logger)
            resolved_file_path = str(Path(file_path).resolve())

            if platform:
                metadata.platform = platform

            if invocation_control is not None:
                metadata.invocation_control = invocation_control

            self._apply_tools_mapping(metadata)

            existing = self.skills.get(metadata.name)
            if existing is not None:
                existing_path = str(Path(existing.file_path).resolve())
                if existing_path == resolved_file_path:
                    return existing
                self._logger.warning(
                    f"Skill '{metadata.name}' already loaded from '{existing_path}', "
                    f"overriding with '{resolved_file_path}'"
                )

            skill = Skill(metadata=metadata, content=None, file_path=resolved_file_path)
            self.skills[metadata.name] = skill

            # Eagerly register all hooks so they are available before
            # the first agent.run() call.
            self._register_eager_hooks(skill)

            self._logger.info(f"Loaded skill metadata: {metadata.name} (Platform: {metadata.platform})")
            return skill
        except ValueError:
            raise
        except Exception as e:
            self._logger.error(f"Error loading skill from {file_path}: {e}")
            return None

    # -- Content loading ----------------------------------------------------

    def get_skill_content(self, name: str) -> Optional[SkillContent]:
        """Load full skill content on demand and register hooks lazily."""
        skill = self.skills.get(name)
        if skill is None:
            return None

        if skill.content is None:
            try:
                _metadata, markdown_body = parse_skill_file(skill.file_path, logger=self._logger)
                skill.content = markdown_body
                self._logger.info("Loaded skill body: %s", name)
            except Exception as e:
                self._logger.error(f"Error loading skill content for {name}: {e}")
                return None

        if not skill.hooks_registered:
            self._register_eager_hooks(skill)
            self._logger.info("Registered skill hooks (lazily): %s", name)

        return SkillContent(metadata=skill.metadata, instructions=skill.content)

    # -- Force-inject support -----------------------------------------------

    def get_force_injected_names(self) -> set:
        """Return the set of skill names with ``allow-model: "force-inject"``."""
        return {
            name for name, skill in self.skills.items()
            if skill.metadata.invocation_control.get("allow-model") == "force-inject"
        }

    def get_force_injected_prompt(self) -> str:
        """Build the system-prompt section for force-injected skills.

        For each skill with ``allow-model: "force-inject"`` the full
        instructions are eagerly loaded and embedded in the prompt so the
        LLM does **not** need to call ``load_skill`` at runtime.
        """
        injected_names = self.get_force_injected_names()
        if not injected_names:
            return ""

        parts: list[str] = [
            "",
            "====",
            "",
            "FORCE-INJECTED SKILLS",
            "",
            "<force_injected_skills>",
            "<!-- The following skills have been pre-loaded into your context. -->",
            "<!-- Do NOT call load_skill for these skills; their instructions are already here. -->",
        ]

        for name in sorted(injected_names):
            content = self.get_skill_content(name)
            if content is None:
                self._logger.warning("Force-inject: failed to load content for skill '%s'", name)
                continue

            parts.append(f"\n<force_injected_skill name=\"{name}\">")
            if content.metadata.description:
                parts.append(f"<description>{content.metadata.description}</description>")
            if content.metadata.allowed_tools:
                parts.append("<allowed_tools>")
                for tool in content.metadata.allowed_tools:
                    parts.append(f"  <tool>{tool}</tool>")
                parts.append("</allowed_tools>")
            parts.append("<instructions>")
            parts.append(content.instructions)
            parts.append("</instructions>")
            parts.append("</force_injected_skill>")

            self._logger.info("Force-injected skill into system prompt: %s", name)

        parts.append("\n</force_injected_skills>")
        return "\n".join(parts)

    # -- Prompt generation --------------------------------------------------

    def get_skills_prompt(self) -> str:
        return build_skills_prompt(self.skills)

    # -- Tools mapping ------------------------------------------------------

    def _apply_tools_mapping(self, metadata: SkillMetadata) -> None:
        """Remap tool names / hook matchers for the target platform."""
        target_platform = metadata.platform or 'Claude'
        if target_platform not in self._tools_mapping:
            return

        mapping = self._tools_mapping[target_platform]

        if metadata.allowed_tools:
            metadata.allowed_tools = [
                mapping.get(tool, tool) for tool in metadata.allowed_tools
            ]

        if metadata.hooks:
            for _event, hooks_list in metadata.hooks.items():
                if isinstance(hooks_list, list):
                    for hook_def in hooks_list:
                        if isinstance(hook_def, dict) and 'matcher' in hook_def:
                            matcher = hook_def['matcher']
                            parts = [p.strip() for p in matcher.split('|')]
                            mapped_parts = [str(mapping.get(p, p)) for p in parts]
                            hook_def['matcher'] = '|'.join(mapped_parts)
                            self._logger.debug(
                                f"Mapped hook matcher '{matcher}' -> '{hook_def['matcher']}' "
                                f"for skill {metadata.name}"
                            )

    # -- Hook registration --------------------------------------------------

    def _register_eager_hooks(self, skill: Skill):
        """Register all hooks eagerly during metadata loading."""
        if skill.hooks_registered:
            return
        if not skill.metadata.hooks:
            skill.hooks_registered = True
            return
        if skill.metadata.invocation_control.get("allow-hook", True) is False:
            skill.hooks_registered = True
            self._logger.info(
                "Skipped hook registration for skill '%s' because invocation-control.allow-hook=false",
                skill.metadata.name,
            )
            return

        registered_any = False
        for event_name, hooks_config in skill.metadata.hooks.items():
            try:
                event = HookEvent(event_name)
            except ValueError:
                self._logger.warning(
                    "Unsupported hook event '%s' in skill %s; skipping registration",
                    event_name,
                    skill.metadata.name,
                )
                continue
            if isinstance(hooks_config, list):
                for hook_def in hooks_config:
                    self._register_single_hook(skill, event, hook_def)
                    registered_any = True

        skill.hooks_registered = True
        if registered_any:
            self._logger.info(
                "Eagerly registered all hooks for skill: %s",
                skill.metadata.name,
            )

    def _register_single_hook(self, skill: Skill, event: HookEvent, hook_def: Dict[str, Any]):
        matcher = hook_def.get('matcher', '*')
        if matcher is None:
            matcher = '*'
        if not isinstance(matcher, str):
            raise ValueError(
                f"Skill '{skill.metadata.name}' hook {event.value} has a non-string matcher: {matcher!r}"
            )
        actions = hook_def.get('hooks', [])

        for action in actions:
            if not isinstance(action, dict):
                raise ValueError(
                    f"Skill '{skill.metadata.name}' hook {event.value} has a non-mapping action: {action!r}"
                )

            action_type = action.get('type')
            if action_type != SUPPORTED_HOOK_ACTION_TYPE:
                raise ValueError(
                    f"Skill '{skill.metadata.name}' hook {event.value} uses unsupported action type "
                    f"{action_type!r}; expected '{SUPPORTED_HOOK_ACTION_TYPE}'."
                )

            command_code = action.get('command')
            if not isinstance(command_code, str) or not command_code.strip():
                raise ValueError(
                    f"Skill '{skill.metadata.name}' hook {event.value} must provide a non-empty string command."
                )

            skill_dir = os.path.dirname(skill.file_path)
            validate_hook(command_code, skill.metadata.name, event.value, logger=self._logger)
            hook_timeout = action.get('timeout', 20)
            hook_func = create_hook_executor(
                command_code, skill.metadata.name, skill_dir, self._logger,
                timeout=hook_timeout,
            )

            self.hook_manager.register_hook(
                event=event,
                pattern=matcher,
                func=hook_func,
            )
