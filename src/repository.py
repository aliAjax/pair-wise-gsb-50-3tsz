"""SQLite 表结构与事务访问。

决定版本制：
- decision_versions 保存不可变版本（pending/current/superseded/remanded）；
- appeals 保存复议登记并绑定当时的决定版本（含逾期不予受理记录）；
- todos 保存待办，撤回/结案时更新。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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
                    snapshot TEXT NOT NULL,
                    confirmed_amounts TEXT,
                    review_note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(record_id, version_no)
                );
                CREATE TABLE IF NOT EXISTS appeals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    decision_version_id INTEGER NOT NULL REFERENCES decision_versions(id),
                    status TEXT NOT NULL,
                    applicant TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    served_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    deadline_date TEXT NOT NULL,
                    window_days INTEGER NOT NULL,
                    reject_note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL DEFAULT '',
                    result_note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ref_type TEXT NOT NULL DEFAULT '',
                    ref_id INTEGER,
                    created_at TEXT NOT NULL,
                    done_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_versions_record ON decision_versions(record_id, id);
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
        item["snapshot"] = json.loads(item["snapshot"])
        if item["confirmed_amounts"]:
            item["confirmed_amounts"] = json.loads(item["confirmed_amounts"])
        return item

    @staticmethod
    def _plain_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dumps(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dumps({"state": state}), now),
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
                (state, version, _dumps(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dumps(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # -- 决定版本事务 -------------------------------------------------------

    def save_proposal(self, record_id: int, expected_version: int, payload: Dict[str, Any], snapshot: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """冻结一个 pending 决定版本，并把案件推进到 proposed。"""
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
            version_no = int(connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM decision_versions WHERE record_id=?",
                (record_id,),
            ).fetchone()["next_no"])
            cursor = connection.execute(
                "INSERT INTO decision_versions(record_id,version_no,status,snapshot,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, version_no, "pending", _dumps(snapshot), actor_id, now),
            )
            version_id = int(cursor.lastrowid)
            record_version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='proposed',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (record_version, _dumps(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO todos(record_id,kind,title,status,ref_type,ref_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, "review", "复核第%d版处理建议" % version_no, "open", "decision_version", version_id, now),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "propose", actor_id, record_version,
                 _dumps({"summary": "已冻结第%d版补税、滞纳金和处罚建议" % version_no, "decision_version_id": version_id, "version_no": version_no, "from": "investigating", "to": "proposed", "input": {"proposal": snapshot["proposal"]}}),
                 now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            version = connection.execute("SELECT * FROM decision_versions WHERE id=?", (version_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "decision_version": self._version_row(version)}

    def confirm_review(self, record_id: int, expected_version: int, outcome: str, review_note: str, confirmed_amounts: Optional[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        """复核：accepted/reduced 时 pending 版替换 current 版；remanded 时退回调查。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version, payload FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            pending = connection.execute(
                "SELECT * FROM decision_versions WHERE record_id=? AND status='pending' ORDER BY version_no DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if pending is None:
                connection.rollback()
                raise NotFound("没有待确认版本")
            pending_id = int(pending["id"])
            if outcome in ("accepted", "reduced"):
                new_state = "reviewed"
                connection.execute(
                    "UPDATE decision_versions SET status='superseded' WHERE record_id=? AND status='current'",
                    (record_id,),
                )
                payload = json.loads(row["payload"])
                if confirmed_amounts is not None:
                    payload.update(confirmed_amounts)
                payload["review_outcome"] = outcome
                payload["review_note"] = review_note
                payload["served_version_no"] = int(pending["version_no"])
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_state, int(expected_version) + 1, _dumps(payload), actor_id, now, record_id),
                )
                connection.execute(
                    "UPDATE decision_versions SET status='current',confirmed_amounts=?,review_note=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                    (_dumps(confirmed_amounts) if confirmed_amounts is not None else None, review_note, actor_id, now, pending_id),
                )
                connection.execute(
                    "UPDATE todos SET status='done', done_at=? WHERE record_id=? AND ref_type='decision_version' AND ref_id=? AND status='open'",
                    (now, record_id, pending_id),
                )
                cursor = connection.execute(
                    "INSERT INTO todos(record_id,kind,title,status,ref_type,ref_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, "serve", "送达第%d版税务处理决定书并等待复议" % int(pending["version_no"]), "open", "decision_version", pending_id, now),
                )
                serve_todo_id = int(cursor.lastrowid)
                details = {"summary": "复核确认，第%d版成为当前决定" % int(pending["version_no"]),
                           "decision_version_id": pending_id, "version_no": int(pending["version_no"]),
                           "outcome": outcome, "review_note": review_note,
                           "confirmed_amounts": confirmed_amounts, "from": "proposed", "to": new_state}
            else:
                new_state = "investigating"
                payload = json.loads(row["payload"])
                payload["review_outcome"] = outcome
                payload["review_note"] = review_note
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_state, int(expected_version) + 1, _dumps(payload), actor_id, now, record_id),
                )
                connection.execute(
                    "UPDATE decision_versions SET status='remanded',review_note=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                    (review_note, actor_id, now, pending_id),
                )
                connection.execute(
                    "UPDATE todos SET status='cancelled', done_at=? WHERE record_id=? AND ref_type='decision_version' AND ref_id=? AND status='open'",
                    (now, record_id, pending_id),
                )
                serve_todo_id = None
                details = {"summary": "复核退回，第%d版未生效" % int(pending["version_no"]),
                           "decision_version_id": pending_id, "version_no": int(pending["version_no"]),
                           "outcome": outcome, "review_note": review_note, "from": "proposed", "to": new_state}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "review", actor_id, int(expected_version) + 1, _dumps(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            version = connection.execute("SELECT * FROM decision_versions WHERE id=?", (pending_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "decision_version": self._version_row(version), "serve_todo_id": serve_todo_id}

    def get_version(self, record_id: int, version_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decision_versions WHERE id=? AND record_id=?", (version_id, record_id)
            ).fetchone()
        if row is None:
            raise NotFound("决定版本不存在")
        return self._version_row(row)

    def list_versions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM decision_versions WHERE record_id=? ORDER BY version_no", (record_id,)
            ).fetchall()
        return [self._version_row(row) for row in rows]

    def open_appeal(self, record_id: int) -> Optional[Dict[str, Any]]:
        """当前在审（已受理）的复议；逾期不予受理记录不阻止再次登记。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM appeals WHERE record_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return self._plain_row(row) if row is not None else None

    def register_appeal(self, record_id: int, expected_version: int, version_id: int, timing: Dict[str, Any], applicant: str, reason: str, actor_id: str, within_window: bool, reject_note: str) -> Dict[str, Any]:
        """登记复议：期内受理并推进案件，逾期留痕但不予受理，案件状态不变。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if within_window and int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            status = "accepted" if within_window else "overdue"
            cursor = connection.execute(
                "INSERT INTO appeals(record_id,decision_version_id,status,applicant,reason,served_at,applied_at,deadline_date,window_days,reject_note,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, version_id, status, applicant, reason,
                 timing["served_at"], timing["applied_at"], timing["deadline_date"], timing["window_days"],
                 reject_note, actor_id, now),
            )
            appeal_id = int(cursor.lastrowid)
            audit_details = {
                "appeal_id": appeal_id,
                "decision_version_id": version_id,
                "status": status,
                "served_at": timing["served_at"],
                "applied_at": timing["applied_at"],
                "deadline_date": timing["deadline_date"],
                "window_days": timing["window_days"],
                "reason": reason,
            }
            if within_window:
                record_version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE records SET state='appealed',version=?,updated_by=?,updated_at=? WHERE id=?",
                    (record_version, actor_id, now, record_id),
                )
                connection.execute(
                    "UPDATE todos SET status='done', done_at=? WHERE record_id=? AND kind='serve' AND status='open'",
                    (now, record_id),
                )
                cursor2 = connection.execute(
                    "INSERT INTO todos(record_id,kind,title,status,ref_type,ref_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, "appeal", "审理第%d条复议申请" % appeal_id, "open", "appeal", appeal_id, now),
                )
                appeal_todo_id = int(cursor2.lastrowid)
                audit_details.update({"summary": "复议申请已受理（绑定第%d版决定）" % version_id, "from": row["state"], "to": "appealed"})
            else:
                record_version = int(row["version"])
                appeal_todo_id = None
                audit_details.update({"summary": "复议申请逾期，登记留痕并不予受理", "note": reject_note})
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "appeal_register", actor_id, record_version, _dumps(audit_details), now),
            )
            appeal = connection.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "appeal": self._plain_row(appeal), "appeal_todo_id": appeal_todo_id}

    def withdraw_appeal(self, record_id: int, expected_version: int, actor_id: str, note: str) -> Dict[str, Any]:
        """撤回复议：案件回到 reviewed，复议与待办同步更新。"""
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
            appeal_row = connection.execute(
                "SELECT * FROM appeals WHERE record_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if appeal_row is None:
                connection.rollback()
                raise NotFound("没有在审的复议申请")
            appeal_id = int(appeal_row["id"])
            record_version = int(expected_version) + 1
            connection.execute("UPDATE appeals SET status='withdrawn', closed_at=?, result_note=? WHERE id=?", (now, note, appeal_id))
            connection.execute(
                "UPDATE records SET state='reviewed',version=?,updated_by=?,updated_at=? WHERE id=?",
                (record_version, actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE todos SET status='done', done_at=? WHERE record_id=? AND ref_type='appeal' AND ref_id=? AND status='open'",
                (now, record_id, appeal_id),
            )
            cursor = connection.execute(
                "INSERT INTO todos(record_id,kind,title,status,ref_type,ref_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, "serve", "复议撤回，继续送达当前决定书并等待复议", "open", "appeal", appeal_id, now),
            )
            serve_todo_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "withdraw_appeal", actor_id, record_version,
                 _dumps({"summary": "复议申请已撤回", "appeal_id": appeal_id, "note": note, "from": "appealed", "to": "reviewed"}), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            appeal = connection.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "appeal": self._plain_row(appeal), "serve_todo_id": serve_todo_id}

    def close_case(self, record_id: int, expected_version: int, actor_id: str, final_decision: str) -> Dict[str, Any]:
        """结案：写回最终决定，结清全部待办；有在审复议时同步结案。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version, payload FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            payload = json.loads(row["payload"])
            payload["final_decision"] = final_decision
            record_version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='closed',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (record_version, _dumps(payload), actor_id, now, record_id),
            )
            appeal_row = connection.execute(
                "SELECT id FROM appeals WHERE record_id=? AND status='accepted' ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
            appeal_id = int(appeal_row["id"]) if appeal_row else None
            if appeal_id is not None:
                connection.execute(
                    "UPDATE appeals SET status='closed', closed_at=?, result_note=? WHERE id=?",
                    (now, final_decision, appeal_id),
                )
            connection.execute(
                "UPDATE todos SET status='done', done_at=? WHERE record_id=? AND status='open'",
                (now, record_id),
            )
            details = {"summary": "案件已结案", "final_decision": final_decision, "appeal_id": appeal_id, "from": row["state"], "to": "closed"}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "close", actor_id, record_version, _dumps(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def list_appeals(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM appeals WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._plain_row(row) for row in rows]

    def list_todos(self, record_id: int, only_open: bool = False) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            if only_open:
                rows = connection.execute(
                    "SELECT * FROM todos WHERE record_id=? AND status='open' ORDER BY id", (record_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM todos WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._plain_row(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), _dumps(details), _now()),
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
