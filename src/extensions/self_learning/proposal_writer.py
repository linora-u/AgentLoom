"""Proposal-only skill management for generated skill packages."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger

from .application_scope import current_application_scope
from .ledger import SelfLearningLedger
from .paths import active_skills_dir, skill_proposals_dir
from .redaction import (
    require_safe_identity,
    sanitize_text_fragment,
    sanitize_value_fragments,
)

logger = get_logger(__name__)

_VALID_ACTIONS = {"create", "patch", "edit", "archive", "write_file", "remove_file"}
_SAFE_FILE_ROOTS = {"references", "scripts", "assets", "templates", "evals"}


def _slug(value: str) -> str:
    raw = require_safe_identity(value, field="skill proposal path identity")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    if not cleaned:
        raise ValueError("name must contain at least one safe path character")
    if cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValueError("name must not contain path separators")
    return cleaned[:80]


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")


def _safe_relative_path(path: str) -> Path:
    safe_path = require_safe_identity(path, field="skill proposal file path")
    raw = Path(safe_path)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("proposal file path must be relative and cannot contain '..'")
    if raw.name in {"", ".", ".."}:
        raise ValueError("proposal file path is invalid")
    if raw.parts[0] != "SKILL.md" and raw.parts[0] not in _SAFE_FILE_ROOTS:
        raise ValueError(
            "proposal file path must be SKILL.md or live under references/scripts/assets/templates/evals"
        )
    return raw


class ProposalWriter:
    """Creates and promotes proposal-only skill packages."""

    def __init__(
        self,
        proposals_dir: str | Path | None = None,
        skills_dir: str | Path | None = None,
    ):
        self.proposals_dir = Path(proposals_dir).resolve() if proposals_dir else skill_proposals_dir()
        self.skills_dir = Path(skills_dir).resolve() if skills_dir else active_skills_dir()
        self.archive_dir = self.proposals_dir / ".archive"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = SelfLearningLedger()

    def _proposal_path(self, name: str, proposal_id: str | None = None) -> Path:
        safe_name = _slug(name)
        if proposal_id:
            return self.proposals_dir / _slug(proposal_id)
        return self.proposals_dir / f"{safe_name}-{_timestamp()}"

    def _latest_for_name(self, name: str) -> Path | None:
        safe_name = _slug(name)
        matches = [
            path for path in self.proposals_dir.glob(f"{safe_name}-*")
            if path.is_dir() and not path.name.startswith(".")
        ]
        return sorted(matches)[-1] if matches else None

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        safe_data = sanitize_value_fragments(data)
        path.write_text(
            json.dumps(safe_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def create(
        self,
        *,
        action: str,
        name: str,
        content: str = "",
        old_string: str = "",
        new_string: str = "",
        path: str = "",
        target: str = "",
    ) -> dict[str, Any]:
        action = (action or "").strip().lower()
        if action not in _VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(_VALID_ACTIONS)}")
        safe_name = _slug(name)
        safe_path = require_safe_identity(
            path,
            field="skill proposal file path",
            allow_empty=True,
        )
        safe_target = require_safe_identity(
            target,
            field="skill proposal target",
            allow_empty=True,
        )

        proposal_path = self._latest_for_name(safe_name) if action in {"write_file", "remove_file"} else None
        if proposal_path is None:
            proposal_path = self._proposal_path(safe_name)
            proposal_path.mkdir(parents=True, exist_ok=False)
        else:
            proposal_path.mkdir(parents=True, exist_ok=True)

        now = datetime.now().astimezone().isoformat()
        meta = {
            "name": safe_name,
            "action": action,
            "created_at": now,
            "proposal_path": str(proposal_path),
            "old_string": sanitize_text_fragment(old_string),
            "new_string": sanitize_text_fragment(new_string),
            "target": safe_target,
            "file_path": safe_path,
            "status": "proposal",
        }

        if action == "create":
            skill_body = content.strip() or f"# {safe_name}\n\nDescribe when to use this skill and the procedure here.\n"
            (proposal_path / "SKILL.md").write_text(
                sanitize_text_fragment(skill_body).rstrip() + "\n",
                encoding="utf-8",
            )
        elif action in {"patch", "edit", "archive"}:
            body = [
                f"# Skill Proposal: {safe_name}",
                "",
                f"- action: {action}",
                f"- target: {safe_target or safe_name}",
                "",
            ]
            if old_string:
                body.extend(
                    [
                        "## Old",
                        "",
                        "```text",
                        sanitize_text_fragment(old_string),
                        "```",
                        "",
                    ]
                )
            if new_string or content:
                body.extend(
                    [
                        "## New",
                        "",
                        "```text",
                        sanitize_text_fragment(new_string or content),
                        "```",
                        "",
                    ]
                )
            (proposal_path / "README.md").write_text("\n".join(body), encoding="utf-8")
        elif action == "write_file":
            rel = _safe_relative_path(safe_path or "SKILL.md")
            target_path = proposal_path / rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(
                sanitize_text_fragment(content).rstrip() + "\n",
                encoding="utf-8",
            )
        elif action == "remove_file":
            rel = _safe_relative_path(safe_path)
            meta["remove_file"] = str(rel)
            (proposal_path / "README.md").write_text(
                f"# Remove File Proposal: {safe_name}\n\nRemove `{rel}` when this proposal is promoted manually.\n",
                encoding="utf-8",
            )

        self._write_json(proposal_path / "proposal.json", meta)
        try:
            self.ledger.upsert_skill_proposal(
                proposal_id=proposal_path.name,
                name=safe_name,
                action=action,
                status="proposal",
                proposal_path=str(proposal_path),
                application_id=current_application_scope().application_id,
                manifest=meta,
            )
        except Exception:
            logger.warning("Skill proposal ledger mirror failed for %s", proposal_path.name, exc_info=True)
        return {"ok": True, "proposal_path": str(proposal_path), "name": safe_name, "action": action}

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.proposals_dir.exists():
            return items
        for path in sorted(self.proposals_dir.iterdir(), key=lambda p: p.name):
            if not path.is_dir() or path.name.startswith("."):
                continue
            meta = self._read_meta(path)
            items.append({
                "id": path.name,
                "name": meta.get("name", path.name),
                "action": meta.get("action", ""),
                "path": str(path),
                "has_skill": (path / "SKILL.md").exists(),
                "created_at": meta.get("created_at", ""),
            })
        return items

    @staticmethod
    def _read_meta(path: Path) -> dict[str, Any]:
        try:
            data = json.loads((path / "proposal.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _resolve_proposal(self, proposal_id: str) -> Path:
        safe = _slug(proposal_id)
        direct = self.proposals_dir / safe
        if direct.is_dir():
            return direct
        matches = [item for item in self.proposals_dir.glob(f"*{safe}*") if item.is_dir()]
        if not matches:
            raise FileNotFoundError(f"Skill proposal not found: {proposal_id}")
        return sorted(matches)[-1]

    def show(self, proposal_id: str) -> dict[str, Any]:
        path = self._resolve_proposal(proposal_id)
        files = []
        for child in sorted(path.rglob("*")):
            if child.is_file():
                files.append(str(child.relative_to(path)))
        preview = ""
        for preferred in ("SKILL.md", "README.md", "proposal.json"):
            file_path = path / preferred
            if file_path.exists():
                preview = sanitize_text_fragment(
                    file_path.read_text(encoding="utf-8"),
                    max_chars=20000,
                )
                break
        return {
            "ok": True,
            "id": path.name,
            "path": str(path),
            "metadata": self._read_meta(path),
            "files": files,
            "preview": preview,
        }

    def promote(self, proposal_id: str, *, destination: str = "") -> dict[str, Any]:
        proposal_path = self._resolve_proposal(proposal_id)
        skill_file = proposal_path / "SKILL.md"
        if not skill_file.exists():
            raise ValueError("Only proposals with SKILL.md can be promoted automatically")
        meta = self._read_meta(proposal_path)
        skill_name = _slug(destination or str(meta.get("name") or proposal_path.name.rsplit("-", 1)[0]))
        dest = self.skills_dir / skill_name
        if dest.exists():
            raise FileExistsError(f"Active skill already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.mkdir()
        for child in proposal_path.iterdir():
            if child.name == "proposal.json":
                continue
            target = dest / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            elif child.is_file():
                shutil.copy2(child, target)
        try:
            self.ledger.update_skill_proposal_status(proposal_path.name, "promoted")
        except Exception:
            logger.warning("Skill proposal status mirror failed for %s", proposal_path.name, exc_info=True)
        return {"ok": True, "skill": skill_name, "path": str(dest)}

    def archive(self, proposal_id: str) -> dict[str, Any]:
        proposal_path = self._resolve_proposal(proposal_id)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dest = self.archive_dir / proposal_path.name
        if dest.exists():
            dest = self.archive_dir / f"{proposal_path.name}-{_timestamp()}"
        shutil.move(str(proposal_path), str(dest))
        try:
            self.ledger.update_skill_proposal_status(proposal_path.name, "archived")
        except Exception:
            logger.warning("Skill proposal status mirror failed for %s", proposal_path.name, exc_info=True)
        return {"ok": True, "archived_path": str(dest)}

    def handle_tool_action(
        self,
        action: str,
        *,
        name: str,
        content: str = "",
        old_string: str = "",
        new_string: str = "",
        path: str = "",
        target: str = "",
    ) -> dict[str, Any]:
        """Model-facing skill management. Always writes proposals only."""
        return self.create(
            action=action,
            name=name,
            content=content,
            old_string=old_string,
            new_string=new_string,
            path=path,
            target=target,
        )
