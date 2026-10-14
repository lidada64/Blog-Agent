"""EXP-3 offline shared budget and parent evidence verification."""

import tempfile
import unittest
from pathlib import Path

from blog_assistant.subagent_experiment import SharedBudget, run_fixed_evidence, verify_findings


def evidence(chunk_id, text, visibility="local_article"):
    return {"chunk_id": chunk_id, "article_id": chunk_id, "version": chunk_id + "-v1",
            "visibility": visibility, "text": text}


class SubagentExperimentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.items = [evidence("a", "Alpha claim."), evidence("b", "Beta claim.")]

    def budget(self, run_id="run", total=8):
        return SharedBudget(self.root / f"{run_id}.json", run_id, total=total)

    def test_same_evidence_single_parent_and_children_share_one_budget(self):
        counts = {}
        for mode in ("A0", "A1", "A2"):
            result = run_fixed_evidence(mode, self.items, self.budget(mode))
            counts[mode] = result["request_count"]
            self.assertEqual(result["citations"], ["a", "b"])
            self.assertEqual(result["answer"], "Alpha claim.\nBeta claim.")
            self.assertEqual(result["rejected"], 0)
        self.assertEqual(counts, {"A0": 1, "A1": 2, "A2": 3})

    def test_stale_quote_or_draft_cannot_become_parent_citation(self):
        private = evidence("secret", "Secret draft marker.", "draft")
        findings = [{"claim": "Forged", "quote": "Forged", "chunk_id": "a", "version": "a-v1"},
                    {"claim": "Secret draft marker.", "quote": "Secret draft marker.",
                     "chunk_id": "secret", "version": "secret-v1"},
                    {"claim": "Alpha claim.", "quote": "Alpha claim.", "chunk_id": "a", "version": "stale"}]
        accepted, rejected = verify_findings(findings, self.items + [private])
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 3)
        result = run_fixed_evidence("A2", self.items, self.budget(), inject={"bad_version": True})
        self.assertEqual(result["rejected"], 1)
        self.assertNotIn("a", result["citations"])

    def test_child_timeout_is_charged_and_parent_only_uses_verified_survivor(self):
        result = run_fixed_evidence("A2", self.items, self.budget(),
                                    inject={"timeout_child": "researcher_2"})
        self.assertEqual(result["request_count"], 3)
        self.assertEqual(result["citations"], ["a"])
        self.assertEqual(result["answer"], "Alpha claim.")
        self.assertEqual(result["children"][1]["status"], "timeout")

    def test_budget_resume_cannot_reset_or_steal_final_reserve(self):
        budget = self.budget(total=4)
        budget.reserve("researcher_1", "exploration")
        budget = self.budget(total=4)
        budget.reserve("researcher_2", "exploration")
        with self.assertRaises(RuntimeError):
            budget.reserve("researcher_3", "exploration")
        budget.reserve("parent_answer", "final")
        self.assertEqual(self.budget(total=4).state["used"], 3)
        with self.assertRaises(ValueError):
            SharedBudget(self.root / "run.json", "different", total=4)
