import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import build_service, create_server, BASE_DIR
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60, 'evidence': ['账簿凭证', '银行流水']}
INSPECTOR = Actor("i1", "inspector")
REVIEWER = Actor("r1", "reviewer")
REP = Actor("t1", "taxpayer_rep")


def advance(service, record, action, actor, data):
    return service.act(actor, record["id"], record["version"], action, data)


def reach_reviewed(service):
    record = service.create(INSPECTOR, "TAX-26100", CREATE_DATA)
    record = advance(service, record, "investigate", INSPECTOR, {"plan": "核对账簿"})
    record = advance(service, record, "propose", INSPECTOR, {"proposal": "补税并处罚"})
    record = advance(service, record, "review", REVIEWER, {"outcome": "accepted", "review_note": "证据充分"})
    return record


class VersioningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_proposal_freezes_snapshot_and_pending_version(self):
        record = self.service.create(INSPECTOR, "TAX-26100", CREATE_DATA)
        record = advance(self.service, record, "investigate", INSPECTOR, {"plan": "p"})
        record = advance(self.service, record, "propose", INSPECTOR, {"proposal": "建议补税"})
        overview = self.service.overview(REVIEWER, record["id"])
        self.assertIsNone(overview["current_version"])
        pending = overview["pending_version"]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["snapshot"]["tax_period"], "2025-Q4")
        self.assertEqual(pending["snapshot"]["evidence"], ["账簿凭证", "银行流水"])
        self.assertEqual(pending["snapshot"]["total_due"], 323700.0)
        self.assertEqual(overview["record"]["payload"]["proposed_amount"], 323700.0)
        # 有复核待办
        self.assertTrue(any(t["kind"] == "review" and t["status"] == "open" for t in overview["open_todos"]))

    def test_review_replaces_current_and_old_version_kept(self):
        record = reach_reviewed(self.service)
        versions = self.service.list_versions(REVIEWER, record["id"])
        self.assertEqual(versions[0]["status"], "current")
        self.assertEqual(versions[0]["confirmed_amounts"]["total_due"], 323700.0)
        # 补充资料改变金额后再次建议
        record = advance(self.service, record, "propose", INSPECTOR,
                         {"proposal": "新增证据后追加", "assessed_tax": 860000.0, "evidence_count": 5})
        self.assertEqual(record["state"], "proposed")
        record = advance(self.service, record, "review", REVIEWER, {"outcome": "accepted", "review_note": "维持"})
        versions = self.service.list_versions(REVIEWER, record["id"])
        self.assertEqual([v["status"] for v in versions], ["superseded", "current"])
        self.assertEqual(versions[1]["snapshot"]["total_due"], 448200.0)
        self.assertEqual(self.service.get_record(REVIEWER, record["id"])["payload"]["total_due"], 448200.0)
        # 旧版仍可查
        old = self.service.get_version(REVIEWER, record["id"], versions[0]["id"])
        self.assertEqual(old["snapshot"]["total_due"], 323700.0)

    def test_review_reduced_scales_confirmed_amounts(self):
        record = self.service.create(INSPECTOR, "TAX-26100", CREATE_DATA)
        record = advance(self.service, record, "investigate", INSPECTOR, {"plan": "p"})
        record = advance(self.service, record, "propose", INSPECTOR, {"proposal": "x"})
        record = advance(self.service, record, "review", REVIEWER,
                         {"outcome": "reduced", "review_note": "部分不予认定", "reduction_pct": 0.5})
        overview = self.service.overview(REVIEWER, record["id"])
        self.assertEqual(overview["current_version"]["status"], "current")
        self.assertEqual(overview["current_version"]["confirmed_amounts"]["total_due"], 161850.0)
        self.assertEqual(record["payload"]["total_due"], 161850.0)

    def test_remand_marks_version_and_allows_reproposal(self):
        record = self.service.create(INSPECTOR, "TAX-26100", CREATE_DATA)
        record = advance(self.service, record, "investigate", INSPECTOR, {"plan": "p"})
        record = advance(self.service, record, "propose", INSPECTOR, {"proposal": "x"})
        record = advance(self.service, record, "review", REVIEWER, {"outcome": "remanded", "review_note": "证据不足"})
        self.assertEqual(record["state"], "investigating")
        versions = self.service.list_versions(INSPECTOR, record["id"])
        self.assertEqual(versions[0]["status"], "remanded")
        record = advance(self.service, record, "supplement", INSPECTOR,
                         {"assessed_tax": 700000.0, "evidence_count": 6})
        self.assertEqual(record["payload"]["total_due"], 249000.0)
        record = advance(self.service, record, "propose", INSPECTOR, {"proposal": "重新建议"})
        overview = self.service.overview(INSPECTOR, record["id"])
        self.assertIsNone(overview["current_version"])
        self.assertEqual(overview["pending_version"]["version_no"], 2)
        self.assertEqual([v["status"] for v in overview["versions"]], ["remanded", "pending"])

    def test_appeal_binds_version_and_deadline_by_served_at(self):
        record = reach_reviewed(self.service)
        current = self.service.overview(REP, record["id"])["current_version"]
        result = self.service.register_appeal(REP, record["id"], {
            "applicant": "Star Ltd", "reason": "有异议",
            "served_at": "2026-08-01", "applied_at": "2026-09-20",
        })
        self.assertTrue(result["accepted"])
        self.assertEqual(result["appeal"]["status"], "accepted")
        self.assertEqual(result["appeal"]["decision_version_id"], current["id"])
        self.assertEqual(result["appeal"]["deadline_date"], "2026-09-30")
        self.assertEqual(result["record"]["state"], "appealed")
        overview = self.service.overview(REP, record["id"])
        self.assertTrue(any(t["kind"] == "appeal" and t["status"] == "open" for t in overview["open_todos"]))
        # 复议审理中不能提出新建议
        with self.assertRaises(Conflict):
            self.service.act(INSPECTOR, record["id"], record["version"] + 1, "propose", {"proposal": "新建议"})

    def test_overdue_appeal_is_recorded_but_not_accepted(self):
        record = reach_reviewed(self.service)
        result = self.service.register_appeal(REP, record["id"], {
            "applicant": "Star Ltd", "reason": "迟交",
            "served_at": "2026-08-01", "applied_at": "2026-10-05",
        })
        self.assertFalse(result["accepted"])
        self.assertEqual(result["appeal"]["status"], "overdue")
        self.assertIn("不予受理", result["appeal"]["reject_note"])
        # 案件状态不变，仍可再次在期内登记
        self.assertEqual(self.service.get_record(REP, record["id"])["state"], "reviewed")
        result2 = self.service.register_appeal(REP, record["id"], {
            "applicant": "Star Ltd", "reason": "重新提交",
            "served_at": "2026-08-01", "applied_at": "2026-09-25",
        })
        self.assertTrue(result2["accepted"])

    def test_apply_before_served_is_rejected(self):
        record = reach_reviewed(self.service)
        with self.assertRaises(ValidationError):
            self.service.register_appeal(REP, record["id"], {
                "applicant": "Star Ltd", "reason": "x",
                "served_at": "2026-09-01", "applied_at": "2026-08-30",
            })

    def test_withdraw_appeal_returns_to_reviewed_and_updates_todos(self):
        record = reach_reviewed(self.service)
        result = self.service.register_appeal(REP, record["id"], {
            "applicant": "Star Ltd", "reason": "x",
            "served_at": "2026-08-01", "applied_at": "2026-09-20",
        })
        record = result["record"]
        record = self.service.act(REP, record["id"], record["version"], "withdraw_appeal", {"note": "自愿撤回"})
        self.assertEqual(record["state"], "reviewed")
        appeals = self.service.list_appeals(REP, record["id"])
        self.assertEqual(appeals[0]["status"], "withdrawn")
        overview = self.service.overview(REP, record["id"])
        # 审理待办完成，产生新的等待送达/复议待办
        self.assertFalse(any(t["kind"] == "appeal" and t["status"] == "open" for t in overview["todos"]))
        self.assertTrue(any(t["kind"] == "serve" and t["status"] == "open" for t in overview["open_todos"]))

    def test_close_clears_todos_and_closes_appeal(self):
        record = reach_reviewed(self.service)
        result = self.service.register_appeal(REP, record["id"], {
            "applicant": "Star Ltd", "reason": "x",
            "served_at": "2026-08-01", "applied_at": "2026-09-20",
        })
        record = result["record"]
        record = self.service.act(REVIEWER, record["id"], record["version"], "close", {"final_decision": "维持并结案"})
        self.assertEqual(record["state"], "closed")
        overview = self.service.overview(REVIEWER, record["id"])
        self.assertEqual(overview["open_todos"], [])
        self.assertEqual(overview["appeals"][0]["status"], "closed")

    def test_proposal_without_evidence_rejected(self):
        data = dict(CREATE_DATA)
        data["evidence_count"] = 0
        data["evidence"] = []
        record = self.service.create(INSPECTOR, "TAX-26100", data)
        record = advance(self.service, record, "investigate", INSPECTOR, {"plan": "p"})
        with self.assertRaises(ValidationError):
            advance(self.service, record, "propose", INSPECTOR, {"proposal": "x"})

    def test_appeal_permission(self):
        record = reach_reviewed(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.register_appeal(INSPECTOR, record["id"], {
                "applicant": "Star Ltd", "reason": "x",
                "served_at": "2026-08-01", "applied_at": "2026-09-20",
            })


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "test.db"))
        self.server = create_server("127.0.0.1", 0, service, BASE_DIR / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, body=None, role="admin"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path), data=data, method=method,
            headers={"Content-Type": "application/json", "X-User-Id": "u1", "X-Role": role},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_http_version_flow_and_overdue_appeal(self):
        status, record = self._request("POST", "/api/records",
                                       {"reference": "TAX-26200", "data": CREATE_DATA}, role="inspector")
        self.assertEqual(status, 201)
        rid = record["id"]
        for action, payload in (("investigate", {"plan": "p"}), ("propose", {"proposal": "建议"}),
                                ("review", {"outcome": "accepted", "review_note": "ok"})):
            status, record = self._request("POST", "/api/records/%s/actions/%s" % (rid, action),
                                           {"expected_version": record["version"], "data": payload},
                                           role="reviewer" if action == "review" else "inspector")
            self.assertEqual(status, 200, record)
        status, overview = self._request("GET", "/api/records/%s/overview" % rid)
        self.assertEqual(status, 200)
        self.assertEqual(overview["current_version"]["status"], "current")
        # 逾期登记：HTTP 200，accepted=false，记录可在 appeals 中查到
        status, result = self._request("POST", "/api/records/%s/appeals" % rid,
                                       {"data": {"applicant": "Star Ltd", "reason": "迟交",
                                                 "served_at": "2026-08-01", "applied_at": "2026-10-10"}},
                                       role="taxpayer_rep")
        self.assertEqual(status, 200)
        self.assertFalse(result["accepted"])
        status, appeals = self._request("GET", "/api/records/%s/appeals" % rid)
        self.assertEqual(appeals["items"][0]["status"], "overdue")


if __name__ == "__main__":
    unittest.main()
