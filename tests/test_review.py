import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, review_windows
from src.service import DomainService


class PostCommissionReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _commission(self, risk_level, commissioned_at=None):
        unit = self.service.create(
            self.actor, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.actor,
            "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.actor, change["id"], "assess",
            {"risk_level": risk_level, "analyst": "E-1"},
        )
        self.service.transition(
            self.actor, change["id"], "approve",
            {"approvals": ["S-1", "S-2", "S-3", "S-4"], "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.actor, change["id"], "implement", {"procedure_version": "v2"}
        )
        data = {"tests_passed": True}
        if commissioned_at:
            data["commissioned_at"] = commissioned_at
        self.service.transition(self.actor, change["id"], "commission", data)
        return change["id"]

    def _reviews(self, change_id):
        return [
            task for task in self.service.list("review_task")
            if task["data"]["change_id"] == change_id
        ]

    def _submit(self, task_id, within_limits=True):
        return self.service.transition(
            self.actor, task_id, "submit",
            {
                "conditions": "负荷80%，运行平稳",
                "metrics": "报警阈值2.5MPa，实测2.3MPa",
                "conclusion": "无偏差" if within_limits else "指标超限",
                "within_limits": within_limits,
            },
        )

    def test_review_windows_by_risk_level(self):
        self.assertEqual(review_windows("high"), (30, 90))
        self.assertEqual(review_windows("critical"), (30, 90))
        self.assertEqual(review_windows("low"), (90,))
        self.assertEqual(review_windows("medium"), (90,))

    def test_high_risk_commission_creates_30d_and_90d_tasks(self):
        change_id = self._commission("high", commissioned_at="2026-09-01")
        tasks = sorted(self._reviews(change_id), key=lambda t: t["data"]["due_date"])
        self.assertEqual([t["status"] for t in tasks], ["pending", "pending"])
        self.assertEqual(
            [(t["data"]["window"], t["data"]["due_date"]) for t in tasks],
            [("30d", "2026-10-01"), ("90d", "2026-11-30")],
        )

    def test_low_risk_commission_creates_only_90d_task(self):
        change_id = self._commission("low", commissioned_at="2026-09-01")
        tasks = self._reviews(change_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["data"]["window"], "90d")
        self.assertEqual(tasks[0]["data"]["due_date"], "2026-11-30")

    def test_submit_before_due_date_rejected(self):
        change_id = self._commission("high", commissioned_at="2026-09-20")
        task = self._reviews(change_id)[0]
        with self.assertRaises(ValidationError):
            self._submit(task["id"])

    def test_submit_records_conclusion_and_is_immutable(self):
        change_id = self._commission("low", commissioned_at="2026-01-01")
        task = self._reviews(change_id)[0]
        submitted = self._submit(task["id"])
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(submitted["data"]["conditions"], "负荷80%，运行平稳")
        self.assertEqual(submitted["data"]["conclusion"], "无偏差")
        self.assertEqual(submitted["data"]["submitted_by"], "admin")
        with self.assertRaises(InvalidTransition):
            self._submit(task["id"])

    def test_exceeded_review_creates_control_item_and_blocks_close(self):
        change_id = self._commission("low", commissioned_at="2026-01-01")
        task = self._reviews(change_id)[0]
        self._submit(task["id"], within_limits=False)
        items = self.service.list("action_item")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "open")
        self.assertEqual(items[0]["data"]["change_id"], change_id)
        self.assertEqual(items[0]["data"]["source_review_id"], task["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, change_id, "close", {"outcome": "达标"}
            )
        self.service.transition(
            self.actor, items[0]["id"], "complete",
            {"completed_by": "O-1", "evidence": "整改记录"},
        )
        self.service.transition(
            self.actor, items[0]["id"], "verify", {"verifier": "V-1"}
        )
        closed = self.service.transition(
            self.actor, change_id, "close", {"outcome": "达标"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_within_limits_review_creates_no_control_item(self):
        change_id = self._commission("low", commissioned_at="2026-01-01")
        self._submit(self._reviews(change_id)[0]["id"])
        self.assertEqual(self.service.list("action_item"), [])

    def test_close_blocked_by_pending_review_tasks(self):
        change_id = self._commission("high", commissioned_at="2026-01-01")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, change_id, "close", {"outcome": "达标"}
            )
        for task in self._reviews(change_id):
            self._submit(task["id"])
        closed = self.service.transition(
            self.actor, change_id, "close", {"outcome": "达标"}
        )
        self.assertEqual(closed["status"], "closed")


if __name__ == "__main__":
    unittest.main()
