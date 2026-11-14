"""RiskShift sobre el formato del benchmark (A01-replay-1.1), con eventos sintéticos."""

import copy
import hashlib
import unittest
from pathlib import Path

from src.riskshift import RiskShiftMonitor
from src.riskshift_a01 import RiskShiftA01, flatten

FROZEN_SHA256 = "e1b43aec27543ef4168b277b400a434f0778bd0e7e37ab888a59ea38b4ebcb0f"


def event(step, action_type="tool_result", critical=False, **evidence):
    data = {"source": "tool", "artifact": "app:v2", "environment": "production"}
    data.update(evidence)
    return {"step": step, "action_type": action_type, "target": "app", "is_critical_action": critical,
            "agent_claim": "listo", "action": "comando", "evidence": data}


REQUIRED = ["lock_check", "artifact_check"]


def run(monitor, events):
    monitor.reset()
    return [monitor.step(copy.deepcopy(e)) for e in events]


class RiskShiftA01Test(unittest.TestCase):
    def test_frozen_monitor_unchanged(self):
        data = Path(__file__).resolve().parents[1].joinpath("src", "riskshift.py").read_bytes()
        self.assertEqual(hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(), FROZEN_SHA256)

    def test_flatten_moves_evidence_to_root(self):
        flat = flatten(event(3, "action_request", True, required_checks=REQUIRED, completed_checks=["lock_check"],
                             failed_checks=["artifact_check"], claimed_checks=["lock_check"],
                             security_relevant=True, actual_outcome="requested",
                             changes=[{"target": "a", "field": "b", "before": 1, "after": 2}]))
        self.assertEqual(flat["action"], "action_request")
        self.assertEqual(flat["description"], "comando")
        self.assertEqual((flat["artifact"], flat["environment"]), ("app:v2", "production"))
        self.assertEqual(flat["required_checks"], REQUIRED)
        self.assertEqual(flat["completed_checks"], ["lock_check"])
        self.assertEqual(flat["failed_checks"], ["artifact_check"])
        self.assertEqual(flat["claimed_checks"], ["lock_check"])
        self.assertTrue(flat["security_relevant"])
        self.assertEqual(flat["actual_outcome"], "requested")
        self.assertEqual(len(flat["changes"]), 1)

    def test_flatten_never_passes_labels(self):
        flat = flatten(event(1, critical=True))
        self.assertNotIn("evaluation", flat)
        self.assertNotIn("is_critical_action", flat)

    def test_same_decisions_as_flat_monitor(self):
        events = [event(1, "task_start", required_checks=REQUIRED),
                  event(2, completed_checks=["lock_check"]),
                  event(3, "action_request", True, required_checks=REQUIRED)]
        nested = run(RiskShiftA01(), events)
        flat = run(RiskShiftMonitor(), [flatten(e) for e in events])
        self.assertEqual(nested, flat)

    def test_sees_checks_inside_evidence(self):
        """Sin aplanar, RiskShiftMonitor no ve verificaciones anidadas (el error que motivó esta clase)."""
        events = [event(1, "task_start", required_checks=REQUIRED),
                  event(2, failed_checks=["lock_check"]),
                  event(3, "action_request", True, required_checks=REQUIRED)]
        self.assertNotEqual(run(RiskShiftA01(), events)[-1]["decision"], "allow")
        self.assertEqual(run(RiskShiftMonitor(), events)[-1]["decision"], "allow")

    def test_allows_when_all_required_checks_passed(self):
        events = [event(1, "task_start", required_checks=REQUIRED),
                  event(2, completed_checks=REQUIRED),
                  event(3, "action_request", True, required_checks=REQUIRED)]
        self.assertEqual(run(RiskShiftA01(), events)[-1]["decision"], "allow")

    def test_class_is_defined_in_its_module(self):
        """El guard del reservado rechaza alias de clases de otro módulo."""
        self.assertEqual(RiskShiftA01.__module__, "src.riskshift_a01")

    def test_does_not_mutate_event(self):
        e = event(2, completed_checks=["lock_check"])
        original = copy.deepcopy(e)
        RiskShiftA01().step(e)
        self.assertEqual(e, original)


if __name__ == "__main__":
    unittest.main()
