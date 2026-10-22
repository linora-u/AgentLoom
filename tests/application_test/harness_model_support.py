"""Controlled HTTP replies for the real upstream method and Pi file tools.

This replaces only the model service. Application, Pi, file access, snapshots,
upstream scheduling and managed process execution remain real.
"""
from __future__ import annotations

import json
import re
from collections import Counter


def text(message):
    value = message.get("content") or ""
    if isinstance(value, str):
        return value
    return "".join(item.get("text", "") for item in value if isinstance(item, dict))


def role_of(request):
    prompt = "\n".join(text(message) for message in request.get("messages", ()) if message.get("role") != "tool")
    if "# THE CANDIDATES ARE ANONYMOUS ON PURPOSE" in prompt:
        return "select"
    if "You are reading the complete record of one improvement round" in prompt:
        return "reflect"
    if "You are composing" in prompt or "You are the integration judge and synthesizer." in prompt:
        return "compose"
    if "# YOUR DIMENSION: INV" in prompt:
        return "general"
    if "# YOUR DIMENSION: DOM" in prompt:
        return "domain"
    return None


def advertised_context_paths(request):
    """Read navigation supplied to the model, including JSON-escaped task data."""
    mission = next(text(message) for message in request["messages"] if message["role"] == "user")
    heading = "Available context files (relative to this session's working directory):"
    mission = mission.split(heading, 1)[-1]
    return sorted({path for path in re.findall(r"`(context/[^`<>\\\n]+)`", mission) if not path.endswith("/")})


class HarnessModel:
    def __init__(self, prompt_path, *, scenario="normal", composed="COMPOSED", feedback=False):
        self.prompt_path = prompt_path
        self.scenario = scenario
        self.composed = composed
        self.feedback = feedback
        self.sessions = Counter()
        self.reads = []
        self.seen_feedback = []
        self.candidate_override = None
        self.pause = None

    def __call__(self, request):
        role = role_of(request)
        if role is None:
            return None
        assert "HIDDEN-CANARY" not in json.dumps(request)
        assert "fixture-key" not in json.dumps(request)
        messages = [message for message in request["messages"] if message["role"] == "tool"]
        if not messages:
            self.sessions[role] += 1
        if self.scenario == "context_discovery":
            return self._discover_context(request, role, messages)
        if self.scenario == "failed_role_commit_recovery" and role == "general" and self.sessions[role] == 1:
            return " "  # A measured Pi completion that cannot satisfy a role reply.
        if self.scenario == "role_timeout" and role == "general":
            plan = [("read", {"path": "context/harness/" + self.prompt_path})]
            if self.sessions[role] == 1:
                plan.append(("write", {"path": "context/harness/" + self.prompt_path,
                                       "content": "FAILED-ATTEMPT-EDIT"}))
            if len(messages) < len(plan):
                name, arguments = plan[len(messages)]
                return [(f"timeout-{len(messages)}", name, arguments)]
            if self.sessions[role] == 1:
                from tests.application_test.mixed_runtime_support import ProviderPause
                return ProviderPause(self.pause)
            assert "BASE" in text(messages[0]) and "FAILED-ATTEMPT-EDIT" not in text(messages[0])
            return "<notes>GENERAL-NOTE-142: fresh baseline after a failed role.</notes>"
        if role == "reflect" and self.scenario == "reflection_error":
            from tests.application_test.mixed_runtime_support import ProviderFailure
            return ProviderFailure(400, "Controlled reflection transport failure")
        plan = [("read", {"path": "context/task.md"})]
        if self.feedback and role != "select":
            plan.append(("read", {"path": "context/development_feedback/base1.json"}))
        if self.scenario == "large_evidence" and role == "general":
            plan.append(("read", {"path": "context/harness/applications/mixed/prompts/large.md", "offset": 9001, "limit": 2}))
        if self.scenario == "resource_discovery" and role in {"general", "domain", "compose"}:
            # The randomized Skill name is known only from the actual mission,
            # never from fixture setup or the candidate_override shortcut.
            mission = next(text(message) for message in request["messages"] if message["role"] == "user")
            paths = re.findall(r"`(context/harness/[^`\n]+/SKILL\.md)`", mission)
            assert len(paths) == 1, "Application resource inventory must advertise the declared Skill"
            discovered = paths[0]
            if role != "compose":
                plan.append(("read", {"path": discovered}))
        if role == "compose":
            plan.extend([
                ("read", {"path": "context/decisions/inv1.md"}),
                ("read", {"path": "context/decisions/dom1.md"}),
                ("read", {"path": "context/pool/inv1/harness.diff"}),
            ])
            if self.scenario == "resource_discovery":
                plan.extend([
                    ("read", {"path": discovered}),
                    ("edit", {"path": discovered, "edits": [{"oldText": "BASE-SKILL", "newText": "COMPOSED-SKILL"}]}),
                ])
            elif self.scenario == "invalid_edits":
                path, content = "applications/mixed/driver.py", "forbidden fixed code"
            elif self.candidate_override is not None:
                path, content = self.candidate_override
            else:
                path, content = self.prompt_path, self.composed
            if self.scenario != "resource_discovery":
                if self.scenario != "invalid_edits":
                    plan.append(("read", {"path": "context/harness/" + path}))
                plan.append(("write", {"path": "context/harness/" + path, "content": content}))
        if role == "select":
            plan = [("read", {"path": "context/candidates/A.md"})]
            if self.feedback:
                plan.append(("read", {"path": "context/development_feedback/A.json"}))
        if role == "reflect":
            plan.append(("read", {"path": "context/decisions/inv1.md"}))
        if len(messages) < len(plan):
            name, arguments = plan[len(messages)]
            self.reads.append((role, name, arguments["path"]))
            return [(f"{role}-{len(messages)}", name, arguments)]
        evidence = "\n".join(text(message) for message in messages)
        assert not any("ENOENT" in text(message) or "Permission denied" in text(message)
                       or "policy_blocked" in text(message) for message in messages), evidence
        if self.feedback:
            if self.scenario == "unscorable":
                assert '"status": "unscorable"' in evidence and '"sample_count": 0' in evidence
            else:
                assert "DEV-CONFIGURED-SCORE" in evidence
            self.seen_feedback.append(role)
        if self.scenario == "large_evidence" and role == "general":
            assert "END-OF-COMPLETE-EVIDENCE" in evidence
        if role in {"compose", "reflect"}:
            assert "GENERAL-NOTE-142" in evidence
        if role == "compose":
            assert "DOMAIN-NOTE-142" in evidence
        if role == "general":
            return "<notes>GENERAL-NOTE-142: inspected the actual task; KEEP_BASE.</notes>"
        if role == "domain":
            return "<notes>DOMAIN-NOTE-142: no supported subject-specific edit.</notes>"
        if role == "compose":
            return "<notes>revised -> working instruction -> inspected base1 and both proposal notes.</notes>"
        if role == "select":
            choice = "BASE" if self.scenario == "selector_invalid" else "A"
            return f"<why>The output contains the required frozen fact.</why>\n```choice\n{choice}\n```"
        if self.scenario == "reflection_invalid":
            return "<experience>cue: Incomplete evidence\nlesson: Unsupported improvement</experience>"
        return "<edits>component: prompt\nobserved: one composed execution\nverdict: confounded</edits>\nNO_ENTRY"

    def _discover_context(self, request, role, messages):
        paths = advertised_context_paths(request)
        sources = [path for path in paths if path.startswith("context/source/")]
        feedback = [path for path in paths if path.startswith("context/development_feedback/")]
        notes = [path for path in paths if path.startswith("context/decisions/")]
        pool = [path for path in paths if path.startswith("context/pool/") and (
            path.endswith(("/harness.diff", "/produced.md", "/rollout.md", "/SKILL.md")))]
        candidates = [path for path in paths if re.fullmatch(r"context/candidates/[^/]+\.md", path)]
        skill = [path for path in paths if path.startswith("context/harness/") and path.endswith("/SKILL.md")]
        reads = ["context/task.md", *sources, *feedback]
        if role in {"general", "domain", "compose"}:
            assert len(skill) == 1
            reads.append(skill[0])
        if role == "compose":
            assert pool and notes and feedback, "Candidate comparison paths must be advertised to the composer"
            assert all(any(path.endswith(suffix) for path in pool)
                       for suffix in ("/harness.diff", "/produced.md", "/rollout.md", "/SKILL.md"))
            reads.extend([*pool, *notes, *(path for path in paths if path.startswith("context/deliverables/"))])
        if role == "select":
            assert len(candidates) > 1 and feedback
            assert {path.rsplit("/", 1)[-1].removesuffix(".md") for path in candidates} == {
                path.rsplit("/", 1)[-1].removesuffix(".json") for path in feedback}
            assert not any(path.startswith(("context/pool/", "context/decisions/", "context/harness/")) for path in paths)
            reads.extend(candidates)
        if role == "reflect":
            baseline = [path for path in paths if path.startswith("context/base_harness/") and path.endswith("/SKILL.md")]
            adoption = [path for path in paths if path.endswith("/adoption.json")]
            assert baseline and adoption and pool and notes and feedback
            reads.extend([*skill, *baseline, *adoption, *pool, *notes])
        reads = list(dict.fromkeys(reads))
        if not messages:
            self.reads.extend((role, "read", path) for path in reads)
            return [(f"{role}-read-{index}", "read", {"path": path}) for index, path in enumerate(reads)]
        evidence = "\n".join(text(message) for message in messages)
        if role not in {"general", "domain"} or feedback:
            assert "DEV-CONFIGURED-SCORE" in evidence and "FACT-123" in evidence
        if role in {"compose", "reflect"}:
            assert "GENERAL-NOTE-142" in evidence and "DOMAIN-NOTE-142" in evidence
            for suffix in ("/harness.diff", "/produced.md"):
                observed = [text(message) for index, path in enumerate(reads) if path.endswith(suffix)
                            for message in messages if message.get("tool_call_id") == f"{role}-read-{index}"]
                assert observed and (all("PROBE" in body for body in observed) if role == "compose"
                                     else any("PROBE" in body for body in observed)), observed
        if role in {"general", "domain", "compose"} and len(messages) == len(reads):
            assert "BASE-SKILL" in evidence
            marker = "COMPOSED" if role == "compose" else "PROBE"
            self.reads.append((role, "edit", skill[0]))
            return [(f"{role}-edit", "edit", {"path": skill[0], "edits": [
                {"oldText": "BASE-SKILL", "newText": marker + "-SKILL"}]})]
        self.seen_feedback.append(role)
        if role == "general":
            return "<notes>GENERAL-NOTE-142: inspected the task and changed the Skill for a real Probe.</notes>"
        if role == "domain":
            return "<notes>DOMAIN-NOTE-142: inspected the task and changed the Skill for a real Probe.</notes>"
        if role == "compose":
            return "<notes>revised -> Skill -> inspected actual Probe diffs, outputs, notes and development measurements.</notes>"
        if role == "select":
            choice = candidates[0].rsplit("/", 1)[-1].removesuffix(".md")
            return f"<why>Read every anonymous candidate and its development measurements.</why>\n```choice\n{choice}\n```"
        return "<edits>component: Skill\nobserved: actual Probe and composed execution\nverdict: confounded</edits>\nNO_ENTRY"
