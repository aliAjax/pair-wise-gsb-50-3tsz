import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}


def make_case(service, reference="TAX-26001"):
    record = service.create(Actor("creator", "inspector"), reference, CREATE_DATA)
    record = service.act(Actor("u1", "inspector"), record["id"], record["version"], "investigate", {"plan": "核对账簿"})
    record = service.act(Actor("u1", "inspector"), record["id"], record["version"], "propose",
                         {"proposal": "补税并处罚", "evidence_refs": ["EVD-001", "EVD-002"]})
    return record


class DecisionVersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_proposal_freezes_snapshot_and_creates_review_todo(self):
        record = make_case(self.service)
        self.assertEqual(record["state"], "proposed")
        versions = self.service.list_versions(Actor("u1", "inspector"), record["id"])
        self.assertEqual(len(versions), 1)
        v1 = versions[0]
        self.assertEqual(v1["status"], "pending")
        self.assertEqual(v1["version_no"], 1)
        # 冻结税期、证据与金额
        self.assertEqual(v1["snapshot"]["tax_period"], "2025-Q4")
        self.assertEqual(v1["evidence_count"], 4)
        self.assertEqual(v1["evidence_refs"], ["EVD-001", "EVD-002"])
        self.assertEqual(v1["snapshot"]["total_due"], 323700.0)
        # 复核待办
        todos = self.service.list_todos(Actor("u1", "reviewer"), record["id"], status_only="open")
        self.assertEqual([t["kind"] for t in todos], ["review"])

    def test_cannot_propose_while_pending_exists(self):
        record = make_case(self.service)
        with self.assertRaises(Conflict):
            self.service.act(Actor("u1", "inspector"), record["id"], record["version"], "propose",
                             {"proposal": "再次建议", "evidence_refs": ["EVD-009"]})

    def test_review_accepted_replaces_current_and_old_version_kept(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "accepted", "review_note": "证据充分", "service_day": 10})
        self.assertEqual(record["state"], "reviewed")
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertIsNone(board["pending_version"])
        self.assertEqual(board["current_version"]["version_no"], 1)
        self.assertEqual(board["current_version"]["status"], "current")
        self.assertEqual(board["current_version"]["service_day"], 10)
        self.assertEqual(board["current_version"]["deadline_day"], 70)
        # 旧版仍可查（同一版转为current）
        self.assertEqual(len(board["versions"]), 1)
        # 复核待办关闭，复议窗口待办产生
        open_kinds = [t["kind"] for t in board["todos"] if t["status"] == "open"]
        self.assertEqual(open_kinds, ["appeal_window"])

    def test_review_reduced_generates_new_frozen_version(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "reduced", "review_note": "金额调减", "service_day": 10, "reduction_pct": 0.5})
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertEqual([v["version_no"] for v in board["versions"]], [1, 2])
        v1, v2 = board["versions"]
        self.assertEqual(v1["status"], "superseded")
        self.assertEqual(v2["status"], "current")
        self.assertEqual(v2["origin"], "review_reduced")
        self.assertEqual(v2["source_version_no"], 1)
        # 新金额减半，旧版金额保持不变
        self.assertEqual(v2["snapshot"]["total_due"], 161850.0)
        self.assertEqual(v1["snapshot"]["total_due"], 323700.0)

    def test_review_remand_voids_pending_and_allows_new_proposal(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "remanded", "review_note": "证据不足"})
        self.assertEqual(record["state"], "investigating")
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertIsNone(board["pending_version"])
        self.assertEqual(board["versions"][0]["status"], "superseded")
        # 重新出建议形成第2版
        record = self.service.act(Actor("u1", "inspector"), record["id"], record["version"], "propose",
                                  {"proposal": "补充证据后建议", "evidence_refs": ["EVD-010"]})
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertEqual(board["pending_version"]["version_no"], 2)

    def test_appeal_binds_current_version_and_deadline_from_service_day(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "accepted", "review_note": "ok", "service_day": 10})
        result = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "appeal",
                                  {"appeal_day": 70, "appeal_reason": "有异议"})
        self.assertEqual(result["state"], "appealed")
        appeals = self.service.list_appeals(Actor("t1", "taxpayer_rep"), record["id"])
        appeal = appeals[0]
        self.assertEqual(appeal["status"], "accepted")
        self.assertEqual(appeal["version_no"], 1)  # 绑定当时版本
        self.assertEqual(appeal["service_day"], 10)
        self.assertEqual(appeal["deadline_day"], 70)
        # 复议窗口待办关闭，答复待办产生
        board = self.service.board(Actor("t1", "taxpayer_rep"), record["id"])
        open_kinds = [t["kind"] for t in board["todos"] if t["status"] == "open"]
        self.assertEqual(open_kinds, ["appeal_response"])

    def test_overdue_appeal_is_rejected_recorded_but_not_accepted(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "accepted", "review_note": "ok", "service_day": 10})
        version_before = record["version"]
        record = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "appeal",
                                  {"appeal_day": 71, "appeal_reason": "迟交"})
        # 案件状态不变、记录版本不变
        self.assertEqual(record["state"], "reviewed")
        self.assertEqual(record["version"], version_before)
        appeals = self.service.list_appeals(Actor("t1", "taxpayer_rep"), record["id"])
        self.assertEqual(len(appeals), 1)
        self.assertEqual(appeals[0]["status"], "rejected_overdue")
        self.assertIn("第71天", appeals[0]["rejection_note"])
        # 逾期后仍可在期限内重新申请（用边界日第70天）
        record = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "appeal",
                                  {"appeal_day": 70, "appeal_reason": "补正申请"})
        self.assertEqual(record["state"], "appealed")
        self.assertEqual(len(self.service.list_appeals(Actor("t1", "taxpayer_rep"), record["id"])), 2)

    def test_withdraw_appeal_returns_to_reviewed_and_closes_todo(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "accepted", "review_note": "ok", "service_day": 10})
        record = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "appeal",
                                  {"appeal_day": 30, "appeal_reason": "异议"})
        record = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "withdraw_appeal", {})
        self.assertEqual(record["state"], "reviewed")
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertEqual(board["appeals"][0]["status"], "withdrawn")
        # 答复待办关闭；案件回到已复核，复议窗口重新打开待办
        open_kinds = [t["kind"] for t in board["todos"] if t["status"] == "open"]
        self.assertEqual(open_kinds, ["appeal_window"])
        # 撤回到reviewed后可再次申请复议
        record = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "appeal",
                                  {"appeal_day": 45, "appeal_reason": "再次申请"})
        self.assertEqual(record["state"], "appealed")
        self.assertEqual(len(self.service.list_appeals(Actor("t1", "taxpayer_rep"), record["id"])), 2)

    def test_close_completes_todos_and_closes_accepted_appeal(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "accepted", "review_note": "ok", "service_day": 10})
        record = self.service.act(Actor("t1", "taxpayer_rep"), record["id"], record["version"], "appeal",
                                  {"appeal_day": 30, "appeal_reason": "异议"})
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "close",
                                  {"final_decision": "审理后维持"})
        self.assertEqual(record["state"], "closed")
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertTrue(all(t["status"] == "done" for t in board["todos"]))
        self.assertEqual(board["appeals"][0]["status"], "closed")

    def test_appeal_requires_current_version_and_evidence(self):
        record = self.service.create(Actor("creator", "inspector"), "TAX-26002", dict(CREATE_DATA, evidence_count=0))
        record = self.service.act(Actor("u1", "inspector"), record["id"], record["version"], "investigate", {"plan": "x"})
        with self.assertRaises(ValidationError):
            self.service.act(Actor("u1", "inspector"), record["id"], record["version"], "propose",
                             {"proposal": "无证据建议", "evidence_refs": []})

    def test_versions_remain_queryable_after_second_review_cycle(self):
        record = make_case(self.service)
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "remanded", "review_note": "退回"})
        record = self.service.act(Actor("u1", "inspector"), record["id"], record["version"], "propose",
                                  {"proposal": "第二次建议", "evidence_refs": ["EVD-100"]})
        record = self.service.act(Actor("r1", "reviewer"), record["id"], record["version"], "review",
                                  {"outcome": "accepted", "review_note": "确认", "service_day": 20})
        board = self.service.board(Actor("r1", "reviewer"), record["id"])
        self.assertEqual([(v["version_no"], v["status"]) for v in board["versions"]],
                         [(1, "superseded"), (2, "current")])
        # 旧版冻结内容不变
        self.assertEqual(board["versions"][0]["evidence_refs"], ["EVD-001", "EVD-002"])
        self.assertEqual(board["current_version"]["deadline_day"], 80)


if __name__ == "__main__":
    unittest.main()
