"""业务用例编排、权限检查与审计。

决定版本制：
- propose 冻结税期/证据/金额形成待确认版；
- review 确认后替换当前决定（或退回）；
- register_appeal 绑定当时决定版本，按送达日起算期限，逾期留痕不予受理；
- withdraw_appeal / close 同步更新待办。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import REVIEW_OUTCOMES, DomainRules


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
        if not isinstance(expected_version, int):
            raise PermissionDenied("expected_version必须是整数")
        record = self.repository.get(record_id)
        data = data or {}
        if action == "propose":
            return self._propose(actor, record, expected_version, data)
        if action == "review":
            return self._review(actor, record, expected_version, data)
        if action == "withdraw_appeal":
            result = self.repository.withdraw_appeal(record_id, expected_version, actor.user_id, text(data, "note"))
            return result["record"]
        if action == "close":
            return self.repository.close_case(record_id, expected_version, actor.user_id, text(data, "final_decision"))
        # investigate / supplement 走通用状态机
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )

    def _propose(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        self.rules.require_transition(record, "propose")
        if self.repository.open_appeal(record["id"]) is not None:
            raise Conflict("复议审理中，不能形成新的处理建议")
        changes, snapshot = self.rules.build_proposal(record["payload"], data)
        new_payload = dict(record["payload"])
        new_payload.update(changes)
        result = self.repository.save_proposal(
            record["id"], expected_version, new_payload, snapshot, actor.user_id,
        )
        return result["record"]

    def _review(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        from .domain import choice
        outcome = choice(data or {}, "outcome", REVIEW_OUTCOMES)
        review_note = text(data or {}, "review_note")
        pending = self._pending_version(record["id"])
        confirmed_amounts = self.rules.confirm_amounts(pending["snapshot"], outcome, data or {})
        result = self.repository.confirm_review(
            record["id"], expected_version, outcome, review_note, confirmed_amounts, actor.user_id,
        )
        return result["record"]

    def _pending_version(self, record_id: int) -> Dict[str, Any]:
        versions = self.repository.list_versions(record_id)
        pending = [item for item in versions if item["status"] == "pending"]
        if not pending:
            raise Conflict("没有待确认版本")
        return pending[-1]

    # -- 决定版本查询 -------------------------------------------------------

    def list_versions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_versions(record_id)

    def get_version(self, actor: Actor, record_id: int, version_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_version(record_id, version_id)

    # -- 复议登记 -----------------------------------------------------------

    def register_appeal(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_appeal(actor.role):
            raise PermissionDenied("角色无权登记复议")
        data = data or {}
        applicant = text(data, "applicant")
        reason = text(data, "reason")
        record = self.repository.get(record_id)
        if record["state"] != "reviewed":
            raise Conflict("决定书尚未生效（或案件已在复议/结案），不能登记复议")
        if self.repository.open_appeal(record_id) is not None:
            raise Conflict("已有在审或已登记的复议申请")
        version_id = data.get("decision_version_id")
        if version_id is not None:
            if not isinstance(version_id, int):
                from .domain import ValidationError
                raise ValidationError("decision_version_id必须是整数")
            decision_version = self.repository.get_version(record_id, int(version_id))
        else:
            current = [item for item in self.repository.list_versions(record_id) if item["status"] == "current"]
            if not current:
                raise Conflict("尚未确认任何决定版本，不能登记复议")
            decision_version = current[-1]
            version_id = decision_version["id"]
        window_days = int(record["payload"].get("appeal_deadline_day", 60))
        timing = self.rules.evaluate_appeal(data.get("served_at"), data.get("applied_at"), window_days)
        expected_version = data.get("expected_version")
        if expected_version is None:
            expected_version = int(record["version"])
        elif not isinstance(expected_version, int):
            from .domain import ValidationError
            raise ValidationError("expected_version必须是整数")
        reject_note = ""
        result = self.repository.register_appeal(
            record_id, int(expected_version), int(version_id), timing,
            applicant, reason, actor.user_id,
            within_window=bool(timing["within_window"]),
            reject_note=self.rules.overdue_note(timing) if not timing["within_window"] else "",
        )
        return {"record": result["record"], "appeal": result["appeal"], "accepted": timing["within_window"],
                "within_window": timing["within_window"]}

    def list_appeals(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_appeals(record_id)

    # -- 待办与总览 ---------------------------------------------------------

    def list_todos(self, actor: Actor, record_id: int, only_open: bool = True) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_todos(record_id, only_open=only_open)

    def overview(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        versions = self.repository.list_versions(record_id)
        current = [item for item in versions if item["status"] == "current"]
        pending = [item for item in versions if item["status"] == "pending"]
        appeals = self.repository.list_appeals(record_id)
        open_appeal = self.repository.open_appeal(record_id)
        return {
            "record": record,
            "current_version": current[-1] if current else None,
            "pending_version": pending[-1] if pending else None,
            "versions": versions,
            "open_todos": self.repository.list_todos(record_id, only_open=True),
            "todos": self.repository.list_todos(record_id, only_open=False),
            "appeals": appeals,
            "open_appeal": open_appeal,
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
