"""业务用例编排、权限检查与审计。

决定版本制：propose冻结税期/证据/金额为待确认版；review确认后替换当前决定，
旧版保留可查；appeal绑定当时版本，按送达日算期限，逾期登记留痕但不受理；
withdraw_appeal与close更新待办。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action == "investigate":
            return self._simple_action(actor, record_id, int(expected_version), action, data or {})
        if action == "close":
            return self._close(actor, record_id, int(expected_version), data or {})
        if action == "propose":
            return self._propose(actor, record_id, int(expected_version), data or {})
        if action == "review":
            return self._review(actor, record_id, int(expected_version), data or {})
        if action == "appeal":
            return self._appeal(actor, record_id, int(expected_version), data or {})
        if action == "withdraw_appeal":
            return self._withdraw_appeal(actor, record_id, int(expected_version))
        raise PermissionDenied("未知操作")

    def _simple_action(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=expected_version,
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )

    def _propose(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        self.rules.require_transition(record, "propose")
        snapshot = self.rules.freeze_proposal(record["payload"], data)
        payload = dict(record["payload"])
        payload["proposal"] = snapshot["proposal"]
        payload["proposed_amount"] = float(snapshot["total_due"])
        version_no = self.repository.next_decision_version_no(record_id)
        return self.repository.save_proposal(
            record_id, expected_version, "proposed", payload, snapshot, actor.user_id,
            {"summary": "已提出补税和处罚建议，冻结第%s版" % version_no, "from": "investigating", "to": "proposed",
             "input": data, "tax_period": snapshot["tax_period"], "evidence_count": snapshot["evidence_count"],
             "total_due": snapshot["total_due"]},
        )

    def _review(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        pending = self.repository.get_pending_version(record_id)
        if record["state"] != "proposed" or pending is None:
            raise Conflict("当前没有待确认的决定版本")
        plan = self.rules.review_plan(pending, data)
        payload = dict(record["payload"])
        if plan["outcome"] == "remanded":
            return self.repository.save_remand(record_id, expected_version, payload, actor.user_id, plan, pending)
        snapshot = plan.get("snapshot") or pending["snapshot"]
        payload.update({
            "review_outcome": plan["outcome"],
            "review_note": plan["note"],
            "service_day": plan["service_day"],
            "appeal_deadline_day": snapshot["appeal_deadline_day"],
            "current_decision_version_no": pending["version_no"] if plan["outcome"] == "accepted" else None,
            "tax_difference": snapshot["tax_difference"],
            "interest": snapshot["interest"],
            "penalty": snapshot["penalty"],
            "total_due": snapshot["total_due"],
        })
        if plan["outcome"] == "reduced":
            payload["current_decision_version_no"] = self.repository.next_decision_version_no(record_id)
        return self.repository.save_review(record_id, expected_version, payload, actor.user_id, plan, pending)

    def _appeal(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        if record["state"] != "reviewed":
            raise Conflict("只有复核确认后的案件才能申请复议")
        current = self.repository.get_current_version(record_id)
        if current is None:
            raise Conflict("没有当前生效的决定版本，无法登记复议")
        status, appeal_day, service_day, deadline_day, reason, rejection_note = self.rules.decide_appeal(current, data)
        details = {
            "status": status,
            "appeal_day": appeal_day,
            "service_day": service_day,
            "deadline_day": deadline_day,
            "decision_version_no": current["version_no"],
            "decision_version_id": current["id"],
        }
        if status == "accepted":
            details.update({"summary": "复议申请已受理，绑定第%s版决定" % current["version_no"], "from": "reviewed", "to": "appealed"})
        else:
            details.update({"summary": "复议申请逾期，不予受理并留档", "rejection_note": rejection_note, "from": "reviewed", "to": "reviewed"})
        result = self.repository.save_appeal(
            record_id, expected_version, status, current, appeal_day, reason, rejection_note, actor.user_id, details,
        )
        return result["record"]

    def _withdraw_appeal(self, actor: Actor, record_id: int, expected_version: int) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        if record["state"] != "appealed":
            raise Conflict("当前没有已受理的复议可撤回")
        appeal = self.repository.get_open_appeal(record_id)
        if appeal is None:
            raise Conflict("没有进行中的复议登记")
        return self.repository.save_appeal_withdrawal(record_id, expected_version, dict(record["payload"]), appeal, actor.user_id)

    def _close(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(record_id)
        new_state = self.rules.require_transition(record, "close")
        payload = dict(record["payload"])
        payload["final_decision"] = text(data, "final_decision")
        return self.repository.save_close(
            record_id, expected_version, new_state, payload, actor.user_id,
            {"summary": "案件已结案", "input": data, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 决定版本制查询 ----

    def list_versions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_versions(record_id)

    def list_appeals(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_appeals(record_id)

    def list_todos(self, actor: Actor, record_id: int, status_only: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_todos(record_id, status_only=status_only)

    def board(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """页面聚合：当前版、待确认版、历史版本、复议登记与待办。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.board(record_id)
