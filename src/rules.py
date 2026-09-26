"""税务稽查案件与复议流程领域规则与状态转换。

决定版本制规则：
- 形成建议（propose）时冻结税期、证据清单和补税/滞纳金/处罚金额，生成待确认版本；
- 复核（review）确认后待确认版替换当前版，旧版保留可查；退回则版本标记为 remanded；
- 复议登记按决定书送达日起算期限，逾期仍登记留痕但不予受理。
"""
from datetime import datetime, timedelta, date
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {
    'investigate': {'inspector'},
    'supplement': {'inspector'},
    'propose': {'inspector'},
    'review': {'reviewer'},
    'withdraw_appeal': {'taxpayer_rep'},
    'close': {'reviewer'},
}
APPEAL_ROLES = {'taxpayer_rep'}
REVIEW_OUTCOMES = ["accepted", "reduced", "remanded"]
# 同态转换（supplement 补充资料不改变案件状态）也在表中显式列出
TRANSITIONS = {
    'investigate': {'opened': 'investigating'},
    'supplement': {'investigating': 'investigating', 'reviewed': 'reviewed'},
    'propose': {'investigating': 'proposed', 'reviewed': 'proposed'},
    'review': {'proposed': 'reviewed'},
    'withdraw_appeal': {'appealed': 'reviewed'},
    'close': {'reviewed': 'closed', 'appealed': 'closed'},
}

DAY_RATE = 0.0005


def _date(value: Any, key: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc


def calc_amounts(declared_tax: float, assessed_tax: float, penalty_rate: float, days_late: int) -> Dict[str, Any]:
    difference = max(0.0, assessed_tax - declared_tax)
    interest = difference * DAY_RATE * days_late
    penalty = difference * penalty_rate
    return {
        "tax_difference": round(difference, 2),
        "interest": round(interest, 2),
        "penalty": round(penalty, 2),
        "total_due": round(difference + interest + penalty, 2),
        "refund_due": round(max(0.0, declared_tax - assessed_tax), 2),
    }


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(APPEAL_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_appeal(self, role: str) -> bool:
        return role == "admin" or role in APPEAL_ROLES

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
        p.update(calc_amounts(float(p["declared_tax"]), float(p["assessed_tax"]), float(p["penalty_rate"]), int(p["days_late"])))
        p["evidence"] = list(p.get("evidence", []))
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

    # -- 资料补充与金额重算 ------------------------------------------------

    @staticmethod
    def _pick_number(data: Dict[str, Any], payload: Dict[str, Any], key: str, minimum: float = 0.0, maximum: Optional[float] = None) -> float:
        if key in data and data[key] is not None:
            return number(data, key, minimum, maximum)
        return float(payload[key])

    @staticmethod
    def _pick_int(data: Dict[str, Any], payload: Dict[str, Any], key: str, minimum: int = 0) -> int:
        if key in data and data[key] is not None:
            return integer(data, key, minimum)
        return int(payload[key])

    def revise_payload(self, payload: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """补充资料后重算金额，返回需要写回案件 payload 的字段。"""
        data = data or {}
        declared_tax = self._pick_number(data, payload, "declared_tax", 0)
        assessed_tax = self._pick_number(data, payload, "assessed_tax", 0)
        penalty_rate = self._pick_number(data, payload, "penalty_rate", 0, 1)
        days_late = self._pick_int(data, payload, "days_late", 0)
        evidence_count = self._pick_int(data, payload, "evidence_count", 0)
        changes: Dict[str, Any] = {
            "declared_tax": declared_tax,
            "assessed_tax": assessed_tax,
            "penalty_rate": penalty_rate,
            "days_late": days_late,
            "evidence_count": evidence_count,
        }
        changes.update(calc_amounts(declared_tax, assessed_tax, penalty_rate, days_late))
        if "evidence" in data:
            changes["evidence"] = text_list(data, "evidence")
        return changes

    # -- 决定版本：建议冻结与复核确认 --------------------------------------

    def build_proposal(self, payload: Dict[str, Any], data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """形成建议：冻结当时税期、证据和金额，返回(payload变更, 冻结快照)。"""
        data = data or {}
        proposal = text(data, "proposal")
        changes = self.revise_payload(payload, data)
        evidence_count = int(changes["evidence_count"])
        if evidence_count <= 0:
            raise ValidationError("没有证据不能提出处理建议")
        evidence = changes.get("evidence")
        if evidence is None:
            evidence = list(payload.get("evidence", []))
        changes["evidence"] = evidence
        changes["proposal"] = proposal
        changes["proposed_amount"] = changes["total_due"]
        snapshot = {
            "tax_period": payload["tax_period"],
            "proposal": proposal,
            "evidence": list(evidence),
            "evidence_count": evidence_count,
            "declared_tax": changes["declared_tax"],
            "assessed_tax": changes["assessed_tax"],
            "penalty_rate": changes["penalty_rate"],
            "days_late": changes["days_late"],
            "tax_difference": changes["tax_difference"],
            "interest": changes["interest"],
            "penalty": changes["penalty"],
            "total_due": changes["total_due"],
            "refund_due": changes["refund_due"],
        }
        return changes, snapshot

    def confirm_amounts(self, snapshot: Dict[str, Any], outcome: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """复核确认金额：accepted 维持冻结金额；reduced 按比例调减；remanded 不确认。"""
        outcome = choice({"outcome": outcome}, "outcome", REVIEW_OUTCOMES)
        if outcome == "remanded":
            return None
        amounts = {key: snapshot[key] for key in ("tax_difference", "interest", "penalty", "total_due", "refund_due")}
        if outcome == "reduced":
            if isinstance(data, dict) and data.get("reduction_pct") is not None:
                factor = number(data, "reduction_pct", 0, 1)
            else:
                factor = 0.5
            amounts["tax_difference"] = round(snapshot["tax_difference"] * factor, 2)
            amounts["interest"] = round(snapshot["interest"] * factor, 2)
            amounts["penalty"] = round(snapshot["penalty"] * factor, 2)
            amounts["total_due"] = round(amounts["tax_difference"] + amounts["interest"] + amounts["penalty"], 2)
            amounts["reduction_pct"] = factor
        return amounts

    # -- 复议期限 -----------------------------------------------------------

    def evaluate_appeal(self, served_at: Any, applied_at: Any, window_days: int) -> Dict[str, Any]:
        """按送达日起算复议期限，返回期限计算结果。"""
        served = _date(served_at, "served_at")
        applied = _date(applied_at, "applied_at")
        if applied < served:
            raise ValidationError("申请日期不能早于送达日期")
        deadline = served + timedelta(days=int(window_days))
        return {
            "served_at": str(served),
            "applied_at": str(applied),
            "deadline_date": str(deadline),
            "window_days": int(window_days),
            "within_window": applied <= deadline,
        }

    def overdue_note(self, timing: Dict[str, Any]) -> str:
        return (
            "复议申请日%(applied_at)s晚于法定复议期限届满日%(deadline_date)s"
            "（自决定书送达日%(served_at)s起%(window_days)d日），依法不予受理。"
            % timing
        )

    # -- 通用动作（调查、补充资料） ----------------------------------------

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查"
        elif action == "supplement":
            changes.update(self.revise_payload(p, data))
            if "note" in data:
                changes["supplement_note"] = text(data, "note")
            summary = "补充证据并重新计算金额"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
