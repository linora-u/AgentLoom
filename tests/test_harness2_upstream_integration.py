"""Behavior of the narrow vendored-core extensions, without model credentials."""

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness2.core import context, prompts, schedule, tree
from harness2.core.proposer import MethodInterrupted, Proposer
from harness2.core.spec import ChannelSpec
from harness2.demo import DemoAdapter, DemoProposer


class UpstreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_vendored_release_assets_match_provenance(self):
        script = (Path(__file__).resolve().parents[1]
                  / "third_party/harness2/scripts/check_provenance.py")
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pi_file_session_harvest_cache_and_interrupt(self):
        source = self.root / "evidence"
        (source / "harness").mkdir(parents=True)
        (source / "harness/prompt.md").write_text("base", encoding="utf-8")
        workspaces = []

        def transport(cwd, mission, system, timeout, *, label):
            workspaces.append(cwd)
            self.assertEqual(label, "inv1.a1")
            (cwd / "context/harness/prompt.md").write_text("改进", encoding="utf-8")
            return "<notes>observed repeated omission</notes>", 0.25, ""

        proposer = Proposer(
            ChannelSpec("pi", "configured-model"), audit_dir=self.root / "audit", transport=transport
        )
        first = proposer.call("inspect and edit", source, "inv1.a1", harvest="context/harness")
        second = proposer.call("inspect and edit", source, "inv1.a1", harvest="context/harness")
        self.assertEqual(first.workspace.joinpath("prompt.md").read_text(), "改进")
        self.assertTrue(second.cached)
        self.assertEqual(second.cost_usd, 0)
        self.assertEqual(len(workspaces), 1)
        self.assertFalse(workspaces[0].exists())
        self.assertEqual(source.joinpath("harness/prompt.md").read_text(), "base")

        def interrupted(cwd, *args, **kwargs):
            workspaces.append(cwd)
            raise MethodInterrupted("stop")

        stopper = Proposer(ChannelSpec("pi", "configured-model"), transport=interrupted)
        with self.assertRaises(MethodInterrupted):
            stopper.call("stop", source, "stopped", max_attempts=3)
        self.assertEqual(len(workspaces), 2)
        self.assertFalse(workspaces[-1].exists())

    def application_adapter(self, *, feedback_mode="contrastive"):
        adapter = DemoAdapter(self.root)
        original = adapter.substrate.surface()
        asset = adapter.base_harness() / "skills/read/assets/pattern.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x00\xff\x80\r\n")
        surface = replace(
            original,
            name="application",
            application=True,
            resource_paths=tuple(p.relative_to(adapter.base_harness()).as_posix()
                                 for p in adapter.base_harness().rglob("*") if p.is_file()),
            editable_globs=("skills/**", "scripts/*.py", "systemprompt.md"),
            surface_md="Edit the declared Application resources in context/harness; preserve its objective.",
        )
        adapter.substrate.surface = lambda: surface
        adapter.spec = replace(
            adapter.spec,
            prompts=replace(adapter.spec.prompts, feedback_mode=feedback_mode),
            channels={role: ChannelSpec("pi", "configured-model") for role in ("proposer", "selector")},
        )
        return adapter

    def test_application_resources_keep_binary_assets_and_real_prompt_surface(self):
        adapter = self.application_adapter()
        surface = adapter.substrate.surface()
        resources = tree.read_tree(surface, adapter.base_harness())
        asset = "skills/read/assets/pattern.bin"
        self.assertEqual(resources[asset], b"\x00\xff\x80\r\n")
        resources["prompts/中文.md"] = "必须保留原目标\n"
        with self.assertRaises(ValueError):
            tree.validate(surface, resources)
        del resources["prompts/中文.md"]
        resources[asset] += b"changed"
        copied = tree.write_tree(surface, resources, self.root / "copy")
        self.assertEqual((copied / asset).read_bytes(), resources[asset])
        evidence = context.build(
            self.root / "context", adapter, "ledger-1", harness_dir=adapter.base_harness(),
            pool=[("candidate", copied, None)],
        )
        self.assertEqual((evidence.root / "pool/candidate/harness" / asset).read_bytes(), resources[asset])
        self.assertIn("binary sha256=", (evidence.root / "pool/candidate/harness.diff").read_text())
        authored = prompts.propose(adapter.spec.prompts, "INV", surface)
        self.assertIn(surface.surface_md, authored)
        self.assertNotIn("plugin/<name>.ts", authored)

    def test_development_feedback_shares_schedule_and_anonymous_selection(self):
        adapter = self.application_adapter(feedback_mode="development_score")
        adapter.development_feedback = lambda task_id, run: {
            "status": "scored", "metrics": {"pass_rate": 1.0}, "sample_count": 3,
            "partition": "development", "evaluator": "local-arithmetic",
        }
        scripted = DemoProposer(adapter.spec.channels["proposer"], transport=lambda *a, **k: None)
        sessions = []

        def transport(cwd, mission, system, timeout, *, label):
            sessions.append(label)
            self.assertIn("ALLOWED DEVELOPMENT FEEDBACK", mission)
            self.assertNotIn("There is no rubric, grading criteria, answer key", mission)
            if label.startswith("select."):
                files = sorted((cwd / "context/development_feedback").glob("*.json"))
                self.assertEqual([p.stem for p in files], ["A", "B"])
                self.assertTrue(all("run_id" not in p.read_text() for p in files))
            reply = scripted.call(mission, cwd / "context", label)
            return reply.text, 0.0, ""

        proposer = Proposer(
            adapter.spec.channels["proposer"], audit_dir=self.root / "audit", transport=transport
        )
        result = schedule.run_domain(
            "accounting", adapter.tasks(), 2, adapter=adapter, prop=proposer,
            runs_root=adapter.spec.runs_root, library_root=self.root / "library", modes=("agg",),
        )
        self.assertEqual((result["done"], result["failed"]), (2, 0))
        self.assertEqual(adapter.execution_count, 16)
        self.assertTrue(any(label.startswith("select.") for label in sessions))
        next_context = adapter.spec.runs_root / "demo-oc-agg2/ledger-2/context/inv1"
        self.assertTrue((next_context / "library/harnesses/ledger-1" ).is_dir())
        self.assertEqual((next_context / "harness/skills/read/assets/pattern.bin").read_bytes(), b"\x00\xff\x80\r\n")
        feedback = next_context / "development_feedback/base1.json"
        context.assert_evidence_has_no_grader_verdicts(next_context, adapter, "ledger-2")
        self.assertEqual(json.loads(feedback.read_text())["metrics"]["pass_rate"], 1.0)
        (next_context / "source/hidden.json").write_text('{"pass_rate":1}')
        with self.assertRaises(ValueError):
            context.assert_evidence_has_no_grader_verdicts(next_context, adapter, "ledger-2")

    def test_contrastive_never_requests_feedback_and_rejects_hidden_measurements(self):
        adapter = self.application_adapter()

        def forbidden(*args):
            self.fail("contrastive called the development evaluator")

        adapter.development_feedback = forbidden
        context.build(self.root / "context", adapter, "ledger-1")
        (adapter.task_files("ledger-1") / "hidden.json").write_text('{"pass_rate":1}')
        with self.assertRaises(ValueError):
            context.build(self.root / "context", adapter, "ledger-1")

    def test_development_feedback_rejects_unstructured_identifying_payload(self):
        adapter = self.application_adapter(feedback_mode="development_score")
        adapter.development_feedback = lambda *args: {"run_id": "inv1"}
        run_dir = adapter.execute("ledger-1", None, "base1")
        from harness2.core.spec import RunView
        with self.assertRaises(ValueError):
            context.build(self.root / "context", adapter, "ledger-1", [RunView("base1", "base1", run_dir, True, True)])


if __name__ == "__main__":
    unittest.main()
