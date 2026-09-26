import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance_days(self, days):
        self.now += timedelta(days=days)
        return self.now


class EffectReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = MutableClock(datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc))
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine(self.clock))
        self.admin = Actor("admin", "admin")
        self.operator = Actor("operator-1", "operator")
        self.safety = Actor("safety-1", "safety")
        self.verifier = Actor("verifier-1", "verifier")

    def tearDown(self):
        self.tmp.cleanup()

    def _commission_change(self, risk_level="high", approvals=None):
        unit = self.service.create(
            self.admin,
            "unit",
            {"name": "Reactor-1", "location": "Plant-A"},
        )
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.admin,
            change["id"],
            "assess",
            {"risk_level": risk_level, "analyst": "E-1"},
        )
        count = {"low": 1, "medium": 2, "high": 3, "critical": 4}[risk_level]
        approvals = approvals or ["S-%d" % index for index in range(count)]
        self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": approvals, "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.admin,
            change["id"],
            "implement",
            {"procedure_version": "v2"},
        )
        item = self.service.create(
            self.admin,
            "action_item",
            {
                "change_id": change["id"],
                "description": "Train operators",
                "owner": "O-1",
            },
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": "O-1", "evidence": "training-log"},
        )
        self.service.transition(
            self.admin,
            item["id"],
            "verify",
            {"verifier": "V-1"},
        )
        change = self.service.transition(
            self.admin,
            change["id"],
            "commission",
            {"tests_passed": True},
        )
        return change

    def _reviews(self, change_id):
        reviews = self.service.list("effect_reviews")
        return [
            review
            for review in reviews
            if review["data"]["change_id"] == change_id
        ]

    def _submit_review(self, review, conclusion="within_limit"):
        return self.service.transition(
            self.operator,
            review["id"],
            "submit",
            {
                "operating_conditions": "Stable operation at design load",
                "indicators": [
                    {"name": "Temperature", "value": "82 C", "limit": "<=85 C"},
                    {"name": "Pressure", "value": "1.8 MPa", "limit": "<=2.0 MPa"},
                ],
                "deviation_conclusion": conclusion,
            },
        )

    def test_high_risk_commission_generates_30_and_90_day_reviews(self):
        change = self._commission_change("high")
        reviews = sorted(self._reviews(change["id"]), key=lambda item: item["data"]["stage_days"])

        self.assertEqual([30, 90], [item["data"]["stage_days"] for item in reviews])
        self.assertEqual(["2026-02-09", "2026-04-10"], [item["data"]["due_date"] for item in reviews])
        self.assertTrue(all(item["status"] == "pending" for item in reviews))

        with self.assertRaises(ValidationError):
            self._submit_review(reviews[0])

        self.clock.advance_days(30)
        completed = self._submit_review(reviews[0])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("within_limit", completed["data"]["deviation_result"])
        self.assertEqual("2026-02-09", completed["data"]["submitted_date"])

        with self.assertRaises(InvalidTransition):
            self._submit_review(completed)

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety,
                change["id"],
                "close",
                {"outcome": "early close"},
            )

        self.clock.advance_days(60)
        self._submit_review(reviews[1])
        closed = self.service.transition(
            self.safety,
            change["id"],
            "close",
            {"outcome": "Post-commission reviews completed"},
        )
        self.assertEqual("closed", closed["status"])

    def test_low_risk_only_has_90_day_review(self):
        change = self._commission_change("low")
        reviews = self._reviews(change["id"])

        self.assertEqual(1, len(reviews))
        self.assertEqual(90, reviews[0]["data"]["stage_days"])
        self.assertEqual("2026-04-10", reviews[0]["data"]["due_date"])

    def test_exceeded_review_creates_control_item_and_blocks_close_until_verified(self):
        change = self._commission_change("low")
        review = self._reviews(change["id"])[0]

        self.clock.advance_days(89)
        with self.assertRaises(ValidationError):
            self._submit_review(review, "exceeded")

        self.clock.advance_days(1)
        completed = self._submit_review(review, "exceeded")
        control_id = completed["data"]["action_item_id"]
        self.assertTrue(control_id)

        controls = [
            item
            for item in self.service.list("action_item")
            if item["data"].get("source_review_id") == review["id"]
        ]
        self.assertEqual(1, len(controls))
        self.assertEqual("open", controls[0]["status"])
        self.assertEqual("effect_review", controls[0]["data"]["source"])

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety,
                change["id"],
                "close",
                {"outcome": "deviation open"},
            )

        self.service.transition(
            self.admin,
            control_id,
            "complete",
            {"completed_by": "engineer-1", "evidence": "corrected operating point"},
        )
        self.service.transition(
            self.verifier,
            control_id,
            "verify",
            {"verifier": "verifier-1"},
        )
        closed = self.service.transition(
            self.safety,
            change["id"],
            "close",
            {"outcome": "deviation corrected and verified"},
        )
        self.assertEqual("closed", closed["status"])

    def test_effect_reviews_cannot_be_created_manually(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "effect_review",
                {"change_id": "any-change", "stage_days": 30},
            )

    def test_review_requires_conditions_indicators_and_valid_conclusion(self):
        change = self._commission_change("low")
        review = self._reviews(change["id"])[0]
        self.clock.advance_days(90)

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                review["id"],
                "submit",
                {
                    "operating_conditions": "",
                    "indicators": [{"name": "Temperature", "value": "82 C"}],
                    "deviation_conclusion": "within_limit",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                review["id"],
                "submit",
                {
                    "operating_conditions": "Stable",
                    "indicators": [],
                    "deviation_conclusion": "within_limit",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                review["id"],
                "submit",
                {
                    "operating_conditions": "Stable",
                    "indicators": [{"name": "Temperature", "value": "82 C"}],
                    "deviation_conclusion": "unknown",
                },
            )


if __name__ == "__main__":
    unittest.main()
