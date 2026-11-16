"""The offline evaluation harness runs end to end on the mock model."""
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ["COLLAB_PROVIDER"] = "mock"

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)

SCENARIO = {"id": "s", "kind": "ill", "persona": "学生", "problem": "我想让社区垃圾分类做得更好"}


class EvalTests(unittest.TestCase):
    def test_every_agent_runs_and_reports(self):
        results = [run_eval.run_one(SCENARIO, agent, 2, lambda _: None)
                   for agent in run_eval.AGENTS]
        by_agent = {r["agent"]: r for r in results}
        self.assertEqual(set(by_agent), set(run_eval.AGENTS))
        for result in results:
            self.assertIn("total", result["pre"])
            self.assertIn("total", result["post"])
            self.assertEqual(set(run_eval.TRANSCRIPT_RUBRIC) | {"comment"},
                             set(result["transcript_scores"]))
        self.assertGreater(by_agent["collaborator"]["bubbles"]["shown"], 0)
        self.assertEqual(by_agent["baseline"]["bubbles"]["shown"], 0)
        report = run_eval.build_report(
            results, SimpleNamespace(agents=list(run_eval.AGENTS), turns=2))
        self.assertIn("| collaborator |", report)
        self.assertIn("Mock provider", report)


if __name__ == "__main__":
    unittest.main()
