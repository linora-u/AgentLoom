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
