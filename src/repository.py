"""SQLite 表结构与事务访问。

记录（records）保存案件当前状态；决定版本（decision_versions）冻结每次建议
形成时的税期、证据与金额；复议登记（appeals）绑定当时版本并按送达日算期限；
待办（todos）驱动复核、答复和结案。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    version_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    source_version_no INTEGER,
                    proposal TEXT NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    evidence_refs TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    service_day INTEGER,
                    deadline_day INTEGER,
                    review_outcome TEXT,
                    review_note TEXT,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    UNIQUE(record_id, version_no)
                );
                CREATE TABLE IF NOT EXISTS appeals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    decision_version_id INTEGER NOT NULL REFERENCES decision_versions(id),
                    version_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    appeal_day INTEGER NOT NULL,
                    service_day INTEGER NOT NULL,
                    deadline_day INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    rejection_note TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    ref_type TEXT,
                    ref_id INTEGER,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_versions_record ON decision_versions(record_id, version_no);
                CREATE INDEX IF NOT EXISTS idx_appeals_record ON appeals(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_todos_record ON todos(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _version_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["evidence_refs"] = json.loads(item["evidence_refs"])
        item["snapshot"] = json.loads(item["snapshot"])
        return item

    # ---- 案件记录 ----

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 决定版本 ----

    def next_decision_version_no(self, record_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version_no), 0) AS max_no FROM decision_versions WHERE record_id=?", (record_id,)).fetchone()
        return int(row["max_no"]) + 1

    def save_proposal(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], snapshot: Dict[str, Any], actor_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """形成建议：案件进入proposed，冻结一版pending决定，产生复核待办。整笔事务。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            pending = connection.execute("SELECT id FROM decision_versions WHERE record_id=? AND status='pending'", (record_id,)).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("已有待确认版本，请先完成复核")
            max_row = connection.execute("SELECT COALESCE(MAX(version_no), 0) AS max_no FROM decision_versions WHERE record_id=?", (record_id,)).fetchone()
            version_no = int(max_row["max_no"]) + 1
            record_version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, record_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO decision_versions(record_id,version_no,status,origin,source_version_no,proposal,evidence_count,evidence_refs,snapshot,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, version_no, "pending", "proposal", None, snapshot["proposal"], int(snapshot["evidence_count"]),
                 json.dumps(snapshot["evidence_refs"], ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor_id, now),
            )
            connection.execute(
                "INSERT INTO todos(record_id,kind,title,status,ref_type,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "review", "复核第%s版处理建议" % version_no, "open", "decision_version", now),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "propose", actor_id, record_version, json.dumps(dict(details, decision_version_no=version_no), ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def save_review(self, record_id: int, expected_version: int, payload: Dict[str, Any], actor_id: str, plan: Dict[str, Any], pending: Dict[str, Any]) -> Dict[str, Any]:
        """复核确认（accepted/reduced）：pending版下线，新版成为current，待办更新。整笔事务。"""
        now = _now()
        outcome = plan["outcome"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            record_version = int(expected_version) + 1
            if outcome == "reduced":
                max_row = connection.execute("SELECT COALESCE(MAX(version_no), 0) AS max_no FROM decision_versions WHERE record_id=?", (record_id,)).fetchone()
                new_no = int(max_row["max_no"]) + 1
                snapshot = plan["snapshot"]
                connection.execute(
                    "INSERT INTO decision_versions(record_id,version_no,status,origin,source_version_no,proposal,evidence_count,evidence_refs,snapshot,service_day,deadline_day,review_outcome,review_note,created_by,confirmed_by,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, new_no, "current", "review_reduced", int(pending["version_no"]), pending["proposal"],
                     int(snapshot["evidence_count"]), json.dumps(snapshot["evidence_refs"], ensure_ascii=False),
                     json.dumps(snapshot, ensure_ascii=False, sort_keys=True), plan["service_day"], plan["deadline_day"],
                     "reduced", plan["note"], actor_id, actor_id, now, now),
                )
                promoted_no = new_no
                connection.execute("UPDATE decision_versions SET status='superseded' WHERE id=?", (pending["id"],))
            else:
                connection.execute(
                    "UPDATE decision_versions SET status='current',service_day=?,deadline_day=?,review_outcome=?,review_note=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                    (plan["service_day"], plan["deadline_day"], "accepted", plan["note"], actor_id, now, pending["id"]),
                )
                promoted_no = int(pending["version_no"])
            # 其余版本（含此前current）一律下线，旧版仍可查询
            connection.execute(
                "UPDATE decision_versions SET status='superseded' WHERE record_id=? AND status='current' AND version_no<>?",
                (record_id, promoted_no),
            )
            connection.execute(
                "UPDATE records SET state='reviewed',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (record_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE todos SET status='done',closed_at=? WHERE record_id=? AND kind='review' AND status='open'",
                (now, record_id),
            )
            connection.execute(
                "INSERT INTO todos(record_id,kind,title,status,ref_type,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "appeal_window", "送达第%s版决定，等待复议申请（届满日第%s天）" % (promoted_no, plan["deadline_day"]), "open", "decision_version", now),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "review", actor_id, record_version,
                 json.dumps({"from": "proposed", "to": "reviewed", "summary": "复核确认，第%s版决定成为当前版" % promoted_no,
                             "outcome": outcome, "decision_version_no": promoted_no, "service_day": plan["service_day"],
                             "deadline_day": plan["deadline_day"]}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def save_remand(self, record_id: int, expected_version: int, payload: Dict[str, Any], actor_id: str, plan: Dict[str, Any], pending: Dict[str, Any]) -> Dict[str, Any]:
        """复核退回：pending版作废，案件回到investigating，重新出建议。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            record_version = int(expected_version) + 1
            connection.execute("UPDATE decision_versions SET status='superseded',review_outcome='remanded',review_note=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                               (plan["note"], actor_id, now, pending["id"]))
            connection.execute(
                "UPDATE records SET state='investigating',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (record_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute("UPDATE todos SET status='done',closed_at=? WHERE record_id=? AND kind='review' AND status='open'", (now, record_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "review", actor_id, record_version,
                 json.dumps({"from": "proposed", "to": "investigating", "summary": "复核退回，第%s版建议作废" % pending["version_no"],
                             "outcome": "remanded", "decision_version_no": pending["version_no"]}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def list_versions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM decision_versions WHERE record_id=? ORDER BY version_no", (record_id,)).fetchall()
        return [self._version_row(row) for row in rows]

    def get_version(self, version_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM decision_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFound("决定版本不存在")
        return self._version_row(row)

    def get_pending_version(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM decision_versions WHERE record_id=? AND status='pending'", (record_id,)).fetchone()
        return self._version_row(row) if row else None

    def get_current_version(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM decision_versions WHERE record_id=? AND status='current'", (record_id,)).fetchone()
        return self._version_row(row) if row else None

    # ---- 复议登记 ----

    def save_appeal(self, record_id: int, expected_version: int, status: str, version: Dict[str, Any], appeal_day: int, reason: str, rejection_note: str, actor_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """登记复议并绑定当时决定版本；受理才推进案件状态，逾期仅留痕。整笔事务。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version,state FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            cursor = connection.execute(
                "INSERT INTO appeals(record_id,decision_version_id,version_no,status,appeal_day,service_day,deadline_day,reason,rejection_note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, int(version["id"]), int(version["version_no"]), status, appeal_day,
                 int(version["service_day"]), int(version["deadline_day"]), reason, rejection_note or None, actor_id, now),
            )
            appeal_id = int(cursor.lastrowid)
            record_version = int(expected_version)
            if status == "accepted":
                record_version += 1
                connection.execute("UPDATE records SET state='appealed',version=?,updated_by=?,updated_at=? WHERE id=?",
                                   (record_version, actor_id, now, record_id))
                connection.execute("UPDATE todos SET status='done',closed_at=? WHERE record_id=? AND kind='appeal_window' AND status='open'", (now, record_id))
                connection.execute(
                    "INSERT INTO todos(record_id,kind,title,status,ref_type,ref_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, "appeal_response", "答复第%s版决定的复议申请" % version["version_no"], "open", "appeal", appeal_id, now),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "appeal", actor_id, record_version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            appeal_row = connection.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return {"appeal": dict(appeal_row), "record": self._row(result)}

    def save_appeal_withdrawal(self, record_id: int, expected_version: int, payload: Dict[str, Any], appeal: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """撤回复议：登记置为withdrawn，案件回到reviewed，答复待办关闭。整笔事务。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            record_version = int(expected_version) + 1
            current = connection.execute("SELECT * FROM decision_versions WHERE record_id=? AND status='current'", (record_id,)).fetchone()
            connection.execute("UPDATE appeals SET status='withdrawn',closed_at=? WHERE id=?", (now, appeal["id"]))
            connection.execute("UPDATE todos SET status='done',closed_at=? WHERE record_id=? AND ref_type='appeal' AND ref_id=? AND status='open'",
                               (now, record_id, appeal["id"]))
            if current is not None:
                connection.execute(
                    "INSERT INTO todos(record_id,kind,title,status,ref_type,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "appeal_window", "撤回复议后，第%s版决定仍可在届满日（第%s天）前申请复议" % (int(current["version_no"]), int(current["deadline_day"])), "open", "decision_version", now),
                )
            connection.execute(
                "UPDATE records SET state='reviewed',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (record_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "withdraw_appeal", actor_id, record_version,
                 json.dumps({"from": "appealed", "to": "reviewed", "summary": "撤回复议申请", "appeal_id": appeal["id"]}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def save_close(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """结案：推进状态并关闭全部未办待办、把已受理的在办复议置为closed。整笔事务。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            record_version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, record_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute("UPDATE todos SET status='done',closed_at=? WHERE record_id=? AND status='open'", (now, record_id))
            connection.execute("UPDATE appeals SET status='closed',closed_at=? WHERE record_id=? AND status='accepted'", (now, record_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "close", actor_id, record_version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def list_appeals(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM appeals WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_open_appeal(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM appeals WHERE record_id=? AND status='accepted'", (record_id,)).fetchone()
        return dict(row) if row else None

    # ---- 待办 ----

    def list_todos(self, record_id: int, status_only: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            if status_only:
                rows = connection.execute("SELECT * FROM todos WHERE record_id=? AND status=? ORDER BY id", (record_id, status_only)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM todos WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def board(self, record_id: int) -> Dict[str, Any]:
        """聚合页面所需：当前版、待确认版、版本列表、复议登记与待办。"""
        record = self.get(record_id)
        return {
            "record": record,
            "current_version": self.get_current_version(record_id),
            "pending_version": self.get_pending_version(record_id),
            "versions": self.list_versions(record_id),
            "appeals": self.list_appeals(record_id),
            "todos": self.list_todos(record_id),
        }
