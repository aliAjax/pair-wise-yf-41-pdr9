import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def station(code):
    return {"code": code, "lat": 35.0, "lon": 110.0}


def event_payload():
    return {
        "title": "Event-A",
        "origin_time": "2026-09-01T00:00:00Z",
        "location": "Region-A",
        "reports": [
            {"station": "STA-1", "time_offset": 2, "distance_km": 1.0, "amplitude": 3.0},
            {"station": "STA-2", "time_offset": -1, "distance_km": 1.5, "amplitude": 4.0},
            {"station": "STA-3", "time_offset": 1, "distance_km": 1.2, "amplitude": 2.0},
        ],
    }


class BackfillReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.operator = Actor("duty-zhang", "analyst")
        self.reviewer = Actor("reviewer-li", "reviewer")
        for code in ("STA-1", "STA-2", "STA-3"):
            self.service.create(self.admin, "station", station(code))
        self.event_id = self.service.create(
            self.admin, "event", event_payload()
        )["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _station_id(self, code):
        return self.repo.find_entities("station", "code", code)[0]["id"]

    def _set_offline(self, code, reason="instrument maintenance"):
        self.service.transition(self.admin, self._station_id(code), "offline", {"reason": reason})

    def _set_online(self, code):
        self.service.transition(self.admin, self._station_id(code), "online", {"note": "restored"})

    def _publish(self, offline_codes=()):
        for code in offline_codes:
            self._set_offline(code)
        self.service.transition(self.operator, self.event_id, "associate", {})
        self.service.transition(
            self.reviewer, self.event_id, "review",
            {"reviewer": "reviewer-li", "magnitude": 4.2},
        )
        self.service.transition(
            self.reviewer, self.event_id, "publish",
            {"communication_id": "C-1"},
        )

    def _backfill(self, magnitude=4.3, reason="补录检修台站缺报"):
        return self.service.transition(
            self.operator, self.event_id, "backfill",
            {
                "station": "STA-3",
                "time_offset": 1,
                "distance_km": 1.2,
                "amplitude": 2.5,
                "magnitude": magnitude,
                "reason": reason,
            },
        )

    def test_offline_station_excluded_from_new_association(self):
        self._set_offline("STA-3")
        associated = self.service.transition(
            self.operator, self.event_id, "associate", {}
        )
        # STA-3 已下线检修，不参与新事件关联
        self.assertEqual(associated["data"]["associated_count"], 2)
        self.assertEqual(associated["data"]["participating_count"], 2)
        self.assertEqual(
            associated["data"]["participating_stations"], ["STA-1", "STA-2"]
        )

    def test_association_requires_two_online_stations(self):
        for code in ("STA-1", "STA-2", "STA-3"):
            self._set_offline(code)
        with self.assertRaises(ValidationError):
            self.service.transition(self.operator, self.event_id, "associate", {})

    def test_backfill_while_station_still_offline_is_rejected(self):
        self._publish(offline_codes=("STA-3",))
        with self.assertRaises(InvalidTransition):
            self._backfill()

    def test_full_backfill_review_flow(self):
        # 事件在 STA-3 检修下线期间发布：仅 2 个台站参评，震级 4.2
        self._publish(offline_codes=("STA-3",))
        published = self.service.get(self.event_id)
        self.assertEqual(published["data"]["magnitude"], 4.2)
        self.assertEqual(published["data"]["participating_count"], 2)

        # 台站恢复后，值班员补录缺报，形成待审修订
        self._set_online("STA-3")
        pending_event = self._backfill()
        self.assertEqual(pending_event["status"], "revision_pending")
        # 原发布内容仍然可查、未被改动
        self.assertEqual(pending_event["data"]["magnitude"], 4.2)
        self.assertEqual(pending_event["data"]["participating_count"], 2)
        pending = pending_event["data"]["pending_revision"]
        self.assertEqual(pending["magnitude"], 4.3)
        self.assertEqual(pending["participating_count"], 3)
        self.assertEqual(pending["participating_stations"], ["STA-1", "STA-2", "STA-3"])
        self.assertEqual(pending["base_status"], "published")
        self.assertEqual(pending["base_revision_no"], 1)
        self.assertEqual(pending["added_report"]["station"], "STA-3")
        self.assertEqual(pending["median_amplitude"], 3.0)

        # 待审期间只有已对外发布的第 1 版
        versions = self.service.versions(self.event_id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version_no"], 1)
        self.assertEqual(versions[0]["data"]["magnitude"], 4.2)
        self.assertEqual(versions[0]["data"]["participating_count"], 2)
        self.assertEqual(versions[0]["published_by"], "reviewer-li")
        self.assertTrue(versions[0]["created_at"])

        # 复核员未显式确认新旧震级/台站数前，修订不能生效
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer, self.event_id, "approve_revision", {"confirmed": False}
            )
        self.assertEqual(self.service.get(self.event_id)["status"], "revision_pending")

        # 值班员无权批准自己提交的补录
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator, self.event_id, "approve_revision", {"confirmed": True}
            )

        # 复核员确认后，修订才对外发布
        approved = self.service.transition(
            self.reviewer, self.event_id, "approve_revision",
            {"confirmed": True, "communication_id": "C-2"},
        )
        self.assertEqual(approved["status"], "revised")
        self.assertEqual(approved["data"]["magnitude"], 4.3)
        self.assertEqual(approved["data"]["participating_count"], 3)
        self.assertEqual(approved["data"]["revision_no"], 2)
        self.assertNotIn("pending_revision", approved["data"])

        # 新旧发布版本都留存可查
        versions = self.service.versions(self.event_id)
        self.assertEqual([v["version_no"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["data"]["magnitude"], 4.2)
        self.assertEqual(versions[0]["data"]["participating_count"], 2)
        self.assertEqual(versions[1]["data"]["magnitude"], 4.3)
        self.assertEqual(versions[1]["data"]["participating_count"], 3)
        self.assertEqual(versions[1]["communication_id"], "C-2")

        # 审计留下每步处理人、时间和变化内容
        audit = self.service.audit_log(self.event_id)
        actions = [(row["action"], row["actor_id"], row["actor_role"]) for row in audit]
        self.assertIn(("backfill", "duty-zhang", "analyst"), actions)
        self.assertIn(("approve_revision", "reviewer-li", "reviewer"), actions)
        backfill_row = next(row for row in audit if row["action"] == "backfill")
        self.assertEqual(backfill_row["from_status"], "published")
        self.assertEqual(backfill_row["to_status"], "revision_pending")
        self.assertIn("pending_revision", backfill_row["detail"]["changes"])
        self.assertTrue(backfill_row["created_at"])
        approve_row = next(row for row in audit if row["action"] == "approve_revision")
        changes = approve_row["detail"]["changes"]
        self.assertEqual(changes["magnitude"]["from"], 4.2)
        self.assertEqual(changes["magnitude"]["to"], 4.3)
        self.assertEqual(changes["participating_count"]["from"], 2)
        self.assertEqual(changes["participating_count"]["to"], 3)
        self.assertEqual(changes["revision_no"]["to"], 2)
        self.assertIn("pending_revision", changes)
        self.assertEqual(approve_row["detail"]["published_version_no"], 2)

    def test_reject_revision_keeps_published_content(self):
        self._publish(offline_codes=("STA-3",))
        self._set_online("STA-3")
        self._backfill(magnitude=4.9)

        rejected = self.service.transition(
            self.reviewer, self.event_id, "reject_revision",
            {"reason": "补录波形不可用"},
        )
        # 驳回后回到补录前状态，原发布震级/台站数不变，不产生新版本
        self.assertEqual(rejected["status"], "published")
        self.assertEqual(rejected["data"]["magnitude"], 4.2)
        self.assertEqual(rejected["data"]["participating_count"], 2)
        self.assertNotIn("pending_revision", rejected["data"])
        self.assertEqual(len(self.service.versions(self.event_id)), 1)

        # 驳回后值班员可以重新补录
        again = self._backfill(magnitude=4.3, reason="重新补录")
        self.assertEqual(again["status"], "revision_pending")
        self.assertEqual(again["data"]["magnitude"], 4.2)

    def test_unpublished_event_cannot_be_backfilled(self):
        fresh = self.service.create(self.admin, "event", event_payload())["id"]
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.operator, fresh, "backfill",
                {
                    "station": "STA-3",
                    "time_offset": 1,
                    "distance_km": 1.2,
                    "magnitude": 4.3,
                    "reason": "补录",
                },
            )

    def test_backfill_requires_known_online_station(self):
        self._publish()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator, self.event_id, "backfill",
                {
                    "station": "STA-X",
                    "time_offset": 1,
                    "distance_km": 1.2,
                    "magnitude": 4.3,
                    "reason": "补录未知台站",
                },
            )


if __name__ == "__main__":
    unittest.main()
