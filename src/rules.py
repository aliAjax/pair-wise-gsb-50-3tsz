"""税务稽查案件与复议流程领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {'investigate': {'inspector'}, 'propose': {'inspector'}, 'review': {'reviewer'}, 'appeal': {'taxpayer_rep'}, 'withdraw_appeal': {'taxpayer_rep'}, 'close': {'reviewer'}}
TRANSITIONS = {'investigate': {'opened': 'investigating'}, 'propose': {'investigating': 'proposed'}, 'appeal': {'reviewed': 'appealed'}, 'withdraw_appeal': {'appealed': 'reviewed'}, 'close': {'reviewed': 'closed', 'appealed': 'closed'}}
REVIEW_OUTCOMES = ['accepted', 'reduced', 'remanded']
DECISION_STATUSES = ('pending', 'current', 'superseded')
APPEAL_STATUSES = ('accepted', 'rejected_overdue', 'withdrawn', 'closed')

# 提出建议时冻结进决定版本的字段：税期、证据和金额
SNAPSHOT_FIELDS = ('taxpayer', 'tax_period', 'declared_tax', 'assessed_tax', 'tax_difference', 'interest', 'penalty', 'total_due', 'refund_due', 'penalty_rate', 'days_late', 'evidence_count', 'appeal_deadline_day')


def _optional_integer(data: Dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("%s必须是整数" % key)
    if value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    return value


def _optional_factor(data: Dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s必须是数字" % key)
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValidationError("%s只能在0到1之间" % key)
    return value


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
        """无版本副作用的动作：立案调查与结案。"""
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查"
        elif action == "close":
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 决定版本：建议冻结 ----

    def freeze_proposal(self, payload: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """形成建议时冻结税期、证据与金额，之后不再随记录变化。"""
        if int(payload.get("evidence_count", 0)) <= 0:
            raise ValidationError("没有证据不能提出处理建议")
        snapshot = {field: payload[field] for field in SNAPSHOT_FIELDS}
        snapshot["evidence_refs"] = text_list(data, "evidence_refs")
        snapshot["proposal"] = text(data, "proposal")
        return snapshot

    def reduce_snapshot(self, snapshot: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """复核调减按比例重算补税、滞纳金和处罚，产生一份新的冻结快照。"""
        factor = _optional_factor(data, "reduction_pct", 0.5)
        reduced = dict(snapshot)
        reduced["tax_difference"] = round(float(snapshot["tax_difference"]) * factor, 2)
        reduced["interest"] = round(float(snapshot["interest"]) * factor, 2)
        reduced["penalty"] = round(float(snapshot["penalty"]) * factor, 2)
        reduced["total_due"] = round(reduced["tax_difference"] + reduced["interest"] + reduced["penalty"], 2)
        return reduced

    def review_plan(self, pending_version: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """解析复核意图；确认/调减时按送达日计算复议期限届满日。"""
        outcome = choice(data, "outcome", REVIEW_OUTCOMES)
        plan = {"outcome": outcome, "note": text(data, "review_note")}
        if outcome in ("accepted", "reduced"):
            service_day = _optional_integer(data, "service_day", 0, 0)
            plan["service_day"] = service_day
            plan["deadline_day"] = service_day + int(pending_version["snapshot"]["appeal_deadline_day"])
        if outcome == "reduced":
            plan["snapshot"] = self.reduce_snapshot(pending_version["snapshot"], data)
        return plan

    # ---- 复议登记：按送达日算期限 ----

    def decide_appeal(self, current_version: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, int, int, int, str, str]:
        """返回(状态, 申请日, 送达日, 届满日, 理由, 不予受理说明)。逾期也登记留痕。"""
        appeal_day = integer(data, "appeal_day", 0)
        reason = text(data, "appeal_reason")
        service_day = int(current_version["service_day"])
        deadline_day = int(current_version["deadline_day"])
        if appeal_day > deadline_day:
            explanation = "复议申请日为第%s天，晚于自送达日（第%s天）起算的期限届满日（第%s天），超过复议期限，不予受理" % (appeal_day, service_day, deadline_day)
            return "rejected_overdue", appeal_day, service_day, deadline_day, reason, explanation
        return "accepted", appeal_day, service_day, deadline_day, reason, ""
