import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


REPORTS = [
    {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
    {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
]
BACKFILL = {
    "reason": "补录 STA-3 缺报",
    "magnitude": 4.4,
    "backfill_reports": [{"station": "STA-3", "time_offset": 3, "distance_km": 2.0}],
}


class BackfillReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.analyst = Actor("duty-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_station(self, code):
        return self.service.create(
            self.admin, "station", {"code": code, "lat": 35.0, "lon": 110.0}
        )

    def _create_event(self, reports):
        return self.service.create(
            self.admin,
            "event",
            {
                "title": "Event-B",
                "origin_time": "2026-02-01T00:00:00Z",
                "location": "Region-B",
                "reports": reports,
            },
        )

    def _publish(self, event, magnitude=4.2):
        self.service.transition(self.analyst, event["id"], "associate", {})
        self.service.transition(
            self.reviewer, event["id"], "review",
            {"reviewer": "R-1", "magnitude": magnitude},
        )
        return self.service.transition(
            self.reviewer, event["id"], "publish", {"communication_id": "C-1"}
        )

    def test_offline_station_excluded_from_association(self):
        self._create_station("STA-1")
        station2 = self._create_station("STA-2")
        self.service.transition(
            self.admin, station2["id"], "offline", {"reason": "maintenance"}
        )
        event = self._create_event(
            REPORTS + [{"station": "STA-3", "time_offset": 1, "distance_km": 0.5}]
        )
        associated = self.service.transition(self.analyst, event["id"], "associate", {})
        self.assertEqual(associated["data"]["associated_count"], 2)
        self.assertEqual(associated["data"]["station_count"], 2)
        self.assertEqual(associated["data"]["excluded_offline"], ["STA-2"])

    def test_association_fails_without_two_online_reports(self):
        station1 = self._create_station("STA-1")
        station2 = self._create_station("STA-2")
        for station in (station1, station2):
            self.service.transition(
                self.admin, station["id"], "offline", {"reason": "maintenance"}
            )
        event = self._create_event(REPORTS)
        with self.assertRaises(ValidationError):
            self.service.transition(self.analyst, event["id"], "associate", {})

    def test_backfill_review_publishes_revision(self):
        self._create_station("STA-1")
        station3 = self._create_station("STA-3")
        event = self._publish(self._create_event(REPORTS))

        # 台站检修下线，恢复后值班员补录缺报，形成待审修订
        self.service.transition(
            self.admin, station3["id"], "offline", {"reason": "maintenance"}
        )
        self.service.transition(self.admin, station3["id"], "online", {})
        pending = self.service.transition(
            self.analyst, event["id"], "backfill", dict(BACKFILL)
        )
        self.assertEqual(pending["status"], "revision_pending")
        # 原发布内容仍然可查，未被待审修订覆盖
        self.assertEqual(pending["data"]["magnitude"], 4.2)
        self.assertEqual(len(pending["data"]["reports"]), 2)
        revision = pending["data"]["pending_revision"]
        self.assertEqual(revision["base_magnitude"], 4.2)
        self.assertEqual(revision["magnitude"], 4.4)
        self.assertEqual(revision["base_station_count"], 2)
        self.assertEqual(revision["station_count"], 3)
        self.assertEqual(revision["submitted_by"], "duty-1")
        self.assertTrue(revision["submitted_at"])

        # 复核员确认后这次修订才对外生效
        confirmed = self.service.transition(
            self.reviewer, event["id"], "confirm_revision", {"reviewer": "reviewer-1"}
        )
        self.assertEqual(confirmed["status"], "revised")
        self.assertEqual(confirmed["data"]["magnitude"], 4.4)
        self.assertEqual(confirmed["data"]["station_count"], 3)
        self.assertEqual(len(confirmed["data"]["reports"]), 3)
        self.assertEqual(confirmed["data"]["effective_revision"], 2)
        self.assertIsNone(confirmed["data"]["pending_revision"])
        last = confirmed["data"]["last_revision"]
        self.assertEqual(last["previous_magnitude"], 4.2)
        self.assertEqual(last["magnitude"], 4.4)
        self.assertEqual(last["previous_station_count"], 2)
        self.assertEqual(last["station_count"], 3)
        self.assertEqual(last["backfilled_stations"], ["STA-3"])
        self.assertEqual(last["reviewer"], "reviewer-1")

        # 每步处理人、时间和变化内容都留在审计里
        audit = self.service.audit_log(entity_id=event["id"])
        self.assertEqual(
            [entry["action"] for entry in audit],
            ["create", "associate", "review", "publish", "backfill", "confirm_revision"],
        )
        for entry in audit:
            self.assertTrue(entry["actor_id"])
            self.assertTrue(entry["created_at"])
        backfill_entry = audit[4]
        self.assertEqual(backfill_entry["actor_id"], "duty-1")
        self.assertEqual(backfill_entry["from_status"], "published")
        self.assertEqual(backfill_entry["to_status"], "revision_pending")
        self.assertEqual(
            backfill_entry["detail"]["patch"]["pending_revision"]["magnitude"], 4.4
        )
        confirm_entry = audit[5]
        self.assertEqual(confirm_entry["actor_id"], "reviewer-1")
        detail = confirm_entry["detail"]["patch"]["last_revision"]
        self.assertEqual(detail["previous_magnitude"], 4.2)
        self.assertEqual(detail["magnitude"], 4.4)
        self.assertEqual(detail["previous_station_count"], 2)
        self.assertEqual(detail["station_count"], 3)

    def test_reject_revision_restores_previous_status(self):
        event = self._publish(self._create_event(REPORTS))
        self.service.transition(self.analyst, event["id"], "backfill", dict(BACKFILL))
        rejected = self.service.transition(
            self.reviewer, event["id"], "reject_revision", {"reason": "震级依据不足"}
        )
        self.assertEqual(rejected["status"], "published")
        self.assertIsNone(rejected["data"]["pending_revision"])
        self.assertEqual(rejected["data"]["magnitude"], 4.2)
        last = rejected["data"]["last_rejection"]
        self.assertEqual(last["rejected_magnitude"], 4.4)
        self.assertEqual(last["rejected_station_count"], 3)
        self.assertEqual(last["rejected_by"], "reviewer-1")

    def test_reject_revision_from_revised_returns_to_revised(self):
        event = self._publish(self._create_event(REPORTS))
        self.service.transition(
            self.reviewer, event["id"], "revise",
            {"reason": "new station data", "magnitude": 4.3},
        )
        self.service.transition(self.analyst, event["id"], "backfill", dict(BACKFILL))
        rejected = self.service.transition(
            self.reviewer, event["id"], "reject_revision", {"reason": "再核实"}
        )
        self.assertEqual(rejected["status"], "revised")
        self.assertEqual(rejected["data"]["magnitude"], 4.3)

    def test_backfill_requires_recovered_station(self):
        station3 = self._create_station("STA-3")
        event = self._publish(self._create_event(REPORTS))
        self.service.transition(
            self.admin, station3["id"], "offline", {"reason": "maintenance"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(self.analyst, event["id"], "backfill", dict(BACKFILL))

    def test_backfill_rejects_duplicate_station(self):
        event = self._publish(self._create_event(REPORTS))
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst,
                event["id"],
                "backfill",
                {
                    "reason": "重复补录",
                    "magnitude": 4.4,
                    "backfill_reports": [
                        {"station": "STA-1", "time_offset": 3, "distance_km": 2.0}
                    ],
                },
            )

    def test_backfill_permissions_and_status_gates(self):
        event = self._publish(self._create_event(REPORTS))
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.reviewer, event["id"], "backfill", dict(BACKFILL))
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.reviewer, event["id"], "confirm_revision", {"reviewer": "R-2"}
            )
        candidate = self._create_event(REPORTS)
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.analyst, candidate["id"], "backfill", dict(BACKFILL))
        self.service.transition(self.analyst, event["id"], "backfill", dict(BACKFILL))
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst, event["id"], "confirm_revision", {"reviewer": "R-2"}
            )


if __name__ == "__main__":
    unittest.main()
