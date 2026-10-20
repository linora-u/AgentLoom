"""Harness archive references use the existing Curated Memory review boundary."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import fingerprint, json_value


class HarnessLibrary:
    def __init__(self, application_id: str, *, db_path: Path | None = None, agent_config=None):
        from agentloom.self_learning.persistence.memory_store import MemoryStore

        self.application_id = application_id
        self.agent_config = agent_config
        self.memory = MemoryStore(db_path, agent_config=agent_config)

    def view(self, *, feedback: str, partition: str = "", cutoff: str = "") -> tuple[dict[str, Any], ...]:
        """Only reviewed, source-compatible application lessons reach an improver."""
        out = []
        for item in self.memory.list("app", scope_id=self.application_id, states=("active_confirmed",)):
            if item["kind"] != "experience" or not item["memory_key"].startswith("harness:"):
                continue
            sources = [source for source in item["provenance"] if source.get("kind") == "harness_experiment"]
            if not sources:
                continue
            # Every source must fit this view. Merged provenance can otherwise
            # launder an evaluated lesson into an earlier contrastive request.
            if any(
                source.get("feedback") != feedback
                or source.get("partition", "") != partition
                or (cutoff and source.get("available_at", "") > cutoff)
                for source in sources
            ):
                continue
            from .snapshot import load_bundle
            from .roles import candidate_files

            archives = []
            for source in sources:
                try:
                    bundle = load_bundle(Path(source["candidate_root"]))
                except (OSError, ValueError, KeyError):
                    continue
                if bundle.candidate_id == source.get("candidate_id"):
                    archives.append({"candidate_id": bundle.candidate_id, "files": candidate_files(bundle)})
            if archives:
                out.append(
                    {"memory_id": item["id"], "payload": item["payload"], "sources": sources, "harnesses": archives}
                )
        return tuple(out)

    def submit(
        self,
        experiences,
        *,
        selected,
        bundle,
        invocation,
        experiment_id: str,
        reflection_run_id: str,
        feedback: str,
        partition: str = "",
        available_at: str = "",
    ):
        """Submit model lessons for ordinary review, never as administrator writes."""
        results = []
        for experience in experiences:
            value = json_value(experience)
            payload = {key: value.get(key, "") for key in ("trigger", "symptom", "action", "verification")}
            if not all(isinstance(text, str) and text.strip() for text in payload.values()):
                continue
            evidence = value.get("evidence_refs", [])
            if not evidence or not isinstance(evidence, list):
                continue
            # Selection proves adoption/acceptance, not comparative superiority.
            # Keep the exact executed evidence and allow the existing gate to
            # leave a lesson pending when the verifier cannot establish it.
            source = {
                "kind": "harness_experiment",
                "experiment_id": experiment_id,
                "task_snapshot_id": invocation.task_snapshot_id,
                "trial_id": selected.trial_id,
                "candidate_id": bundle.candidate_id,
                "candidate_root": str(bundle.root),
                "feedback": feedback,
                "partition": partition,
                "available_at": available_at,
                "evidence_refs": evidence,
                "root_run_id": reflection_run_id,
                "application_id": self.application_id,
            }
            results.append(
                self.memory.propose(
                    "app",
                    kind="experience",
                    memory_key="harness:" + fingerprint(payload),
                    payload=payload,
                    root_run_id=reflection_run_id,
                    scope_id=self.application_id,
                    agent_config=self.agent_config,
                    provenance=(source,),
                )
            )
        return {"submitted": len(results), "candidates": results}
