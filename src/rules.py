"""税务稽查案件与复议流程领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {'investigate': {'inspector'}, 'propose': {'inspector'}, 'review': {'reviewer'}, 'appeal': {'taxpayer_rep'}, 'close': {'reviewer'}}
TRANSITIONS = {'investigate': {'opened': 'investigating'}, 'propose': {'investigating': 'proposed'}, 'review': {'proposed': 'reviewed'}, 'appeal': {'reviewed': 'appealed'}, 'close': {'reviewed': 'closed', 'appealed': 'closed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "taxpayer")
        text(p, "tax_period")
        number(p, "declared_tax", 0)
        number(p, "assessed_tax", 0)
        number(p, "penalty_rate", 0, 1)
        integer(p, "evidence_count", 0)
        integer(p, "days_late", 0)
        integer(p, "appeal_deadline_day", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        difference = max(0.0, float(p["assessed_tax"]) - float(p["declared_tax"]))
        interest = difference * 0.0005 * int(p["days_late"])
        penalty = difference * float(p["penalty_rate"])
        p["tax_difference"] = round(difference, 2)
        p["interest"] = round(interest, 2)
        p["penalty"] = round(penalty, 2)
        p["total_due"] = round(difference + interest + penalty, 2)
        p["refund_due"] = round(max(0.0, float(p["declared_tax"]) - float(p["assessed_tax"])), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed"} and item["payload"].get("taxpayer") == payload.get("taxpayer") and item["payload"].get("tax_period") == payload.get("tax_period"):
                raise Conflict("同一纳税人同一税期已有未结稽查案件")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查"
        elif action == "propose":
            if int(p["evidence_count"]) <= 0:
                raise ValidationError("没有证据不能提出处理建议")
            changes["proposal"] = text(data, "proposal")
            changes["proposed_amount"] = float(p["total_due"])
            summary = "已提出补税和处罚建议"
        elif action == "review":
            outcome = choice(data, "outcome", ["accepted", "reduced", "remanded"])
            changes["review_outcome"] = outcome
            changes["review_note"] = text(data, "review_note")
            if outcome == "reduced":
                changes["total_due"] = round(float(p["total_due"]) * float(data.get("reduction_pct", 0.5)), 2)
            summary = "复核完成"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["appeal_deadline_day"]):
                raise ValidationError("复议申请超过期限")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "复议申请已受理"
        elif action == "close":
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
