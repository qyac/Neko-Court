"""审核网站的存储层：SQLite（标准库 sqlite3），全部写入都在事务里。

设计要点：
- 单文件数据库，方便备份与迁移（`--data` 指定目录）；
- `applications.uid` 上加"部分唯一索引"，只约束 pending/approved 状态，
  这样被拒绝的人可以换 UID 重来，同时防止同一个 UID 被两个 QQ 同时占住；
- 插件同步过来的快照（验证码、规则、绑定）存在 settings 表里，站点永远不需要反向访问 AstrBot。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

# 申请状态
STATUS_PENDING = "pending"      # 已提交，等待核验/人工
STATUS_APPROVED = "approved"    # 通过
STATUS_REJECTED = "rejected"    # 未通过
STATUS_MANUAL = "manual"        # 需要人工判断
STATUS_BLOCKED = "blocked"      # 已拉黑
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_MANUAL, STATUS_BLOCKED)

# 站点设置默认值（admin 后台可改；plugin_* 由插件同步写入）
DEFAULT_SETTINGS: dict[str, Any] = {
    "site_name": "临时审核群 · 入群审核",
    "site_subtitle": "填写 QQ 与 B站 UID，核验通过后网页直接给验证码",
    "group_line": "",
    "rules_line": "",
    "bili_enabled": True,
    "bili_min_level": 0,
    "bili_min_fans": 0,
    "bili_name_keywords": [],
    "bili_unique": True,
    "bili_on_error": "manual",   # reject 直接不通过 / manual 转人工 / pass 放行
    "use_plugin_rules": True,
    "plugin_url": "",
    "apply_per_ip": 10,          # 每个 IP 每 10 分钟最多提交次数
    "apply_per_qq": 3,           # 每个 QQ 每 10 分钟最多提交次数
    "plugin_code": "",           # ↓ 以下由插件同步写入
    "plugin_code_expire": "",
    "plugin_rules": {},
    "plugin_bindings": {},
    "plugin_synced_at": 0,
    "plugin_groups": [],
    "plugin_stats": {},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL UNIQUE,
    qq TEXT NOT NULL,
    uid TEXT NOT NULL,
    uid_name TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 0,
    fans INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'auto',
    created_at REAL NOT NULL,
    decided_at REAL,
    ip TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_qq ON applications(qq, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_app_uid_active
    ON applications(uid) WHERE status IN ('pending', 'approved');
CREATE TABLE IF NOT EXISTS blocklist (
    qq TEXT PRIMARY KEY,
    at REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    """极简存储封装：每次操作开一个连接（SQLite 开销小，线程安全）。"""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------ 设置

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, payload),
            )
            conn.commit()

    def all_settings(self) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        with self._connect() as conn:
            for row in conn.execute("SELECT key, value FROM settings"):
                try:
                    data[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    data[row["key"]] = row["value"]
        return data

    # ------------------------------------------------------------------ 申请

    def create_application(
        self,
        *,
        qq: str,
        uid: str,
        status: str,
        ticket: str = "",
        reason: str = "",
        uid_name: str = "",
        level: int = 0,
        fans: int = 0,
        note: str = "",
        code: str = "",
        source: str = "auto",
        ip: str = "",
    ) -> int:
        ticket = ticket or secrets.token_hex(16)
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO applications"
                "(ticket, qq, uid, uid_name, level, fans, status, reason, note, code, source, created_at, ip)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket,
                    qq,
                    uid,
                    uid_name,
                    int(level),
                    int(fans),
                    status,
                    reason,
                    note,
                    code,
                    source,
                    time.time(),
                    ip,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_application(self, app_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        return dict(row) if row else None

    def find_by_ticket(self, ticket: str) -> dict[str, Any] | None:
        """按 ticket 查申请（ticket 是随机 32 位十六进制，避免被枚举）。"""
        text = str(ticket or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", text):
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM applications WHERE ticket = ?", (text,)).fetchone()
        return dict(row) if row else None

    def list_applications(
        self,
        *,
        status: str = "all",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if query:
            like = f"%{query}%"
            where.append("(qq LIKE ? OR uid LIKE ? OR uid_name LIKE ? OR note LIKE ?)")
            params.extend([like, like, like, like])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM applications {clause}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM applications {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, int(limit), max(0, int(offset))],
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def public_item(self, row: dict[str, Any]) -> dict[str, Any]:
        """给申请人看的字段（不含 IP、来源等内部信息）。"""
        return {
            "ticket": str(row.get("ticket") or ""),
            "qq": str(row.get("qq") or ""),
            "uid": str(row.get("uid") or ""),
            "uid_name": str(row.get("uid_name") or ""),
            "status": str(row.get("status") or ""),
            "reason": str(row.get("reason") or ""),
            "code": str(row.get("code") or ""),
            "created_at": float(row.get("created_at") or 0),
            "decided_at": row.get("decided_at"),
        }

    def admin_item(self, row: dict[str, Any]) -> dict[str, Any]:
        """给管理后台看的字段。"""
        return {
            "id": int(row.get("id") or 0),
            "ticket": str(row.get("ticket") or ""),
            "qq": str(row.get("qq") or ""),
            "uid": str(row.get("uid") or ""),
            "uid_name": str(row.get("uid_name") or ""),
            "level": int(row.get("level") or 0),
            "fans": int(row.get("fans") or 0),
            "status": str(row.get("status") or ""),
            "reason": str(row.get("reason") or ""),
            "note": str(row.get("note") or ""),
            "code": str(row.get("code") or ""),
            "source": str(row.get("source") or ""),
            "ip": str(row.get("ip") or ""),
            "delivered": int(row.get("delivered") or 0),
            "created_at": float(row.get("created_at") or 0),
            "decided_at": row.get("decided_at"),
        }

    def decide(self, app_id: int, status: str, *, reason: str = "", actor: str = "admin", note: str = "") -> bool:
        if status not in STATUSES:
            return False
        with self._connect() as conn:
            row = conn.execute("SELECT note FROM applications WHERE id = ?", (app_id,)).fetchone()
            if row is None:
                return False
            merged_note = note or str(row["note"] or "")
            conn.execute(
                "UPDATE applications SET status = ?, reason = ?, decided_at = ?, source = ?, note = ? WHERE id = ?",
                (status, reason, time.time(), actor, merged_note, app_id),
            )
            conn.commit()
        return True

    def delete_application(self, app_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
            conn.commit()
            return cursor.rowcount > 0

    def set_code_for_qq(self, qq: str, code: str) -> int:
        """插件换码/补码后，把该 QQ 的最新验证码补到页面上。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE applications SET code = ? WHERE qq = ? AND status = 'approved'",
                (code, str(qq)),
            )
            conn.commit()
            return int(cursor.rowcount)

    def uid_taken_by(self, uid: str, exclude_qq: str = "") -> str:
        """返回占用该 UID 的 QQ（只看 pending/approved），没有则空串。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq FROM applications WHERE uid = ? AND status IN ('pending','approved') "
                "AND qq != ? ORDER BY created_at DESC LIMIT 1",
                (str(uid), str(exclude_qq)),
            ).fetchone()
        return str(row["qq"]) if row else ""

    # ------------------------------------------------- 插件同步 / 待拉取队列

    def pull_approved(self, limit: int = 50) -> list[dict[str, Any]]:
        """插件来拉取"已通过但还没通知插件"的申请（拉完由插件 ack）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status = 'approved' AND delivered = 0 "
                "ORDER BY decided_at ASC, id ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def ack_delivered(self, ids: Iterable[int]) -> int:
        id_list = [int(item) for item in ids]
        if not id_list:
            return 0
        placeholders = ",".join("?" for _ in id_list)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE applications SET delivered = 1, delivered_at = ? WHERE id IN ({placeholders})",
                [time.time(), *id_list],
            )
            conn.commit()
            return int(cursor.rowcount)

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status").fetchall()
            blocked = conn.execute("SELECT COUNT(*) AS n FROM blocklist").fetchone()["n"]
        data = {status: 0 for status in STATUSES}
        total = 0
        for row in rows:
            data[str(row["status"])] = int(row["n"])
            total += int(row["n"])
        data["total"] = total
        data["blocked_list"] = int(blocked)
        return data

    def all_approved(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, qq, uid, uid_name, code, created_at, decided_at, delivered "
                "FROM applications WHERE status = 'approved' ORDER BY id DESC LIMIT 500"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 拉黑

    def is_blocked(self, qq: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM blocklist WHERE qq = ?", (str(qq),)).fetchone()
        return row is not None

    def block(self, qq: str, note: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO blocklist(qq, at, note) VALUES(?,?,?) "
                "ON CONFLICT(qq) DO UPDATE SET note = excluded.note",
                (str(qq), time.time(), note),
            )
            conn.commit()

    def unblock(self, qq: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM blocklist WHERE qq = ?", (str(qq),))
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ 日志

    LOG_KEEP = 2000  # 只保留最近这么多条，避免被刷量撑爆磁盘

    def log(self, action: str, *, actor: str = "", detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO logs(at, actor, action, detail) VALUES(?,?,?,?)",
                (time.time(), actor, action, detail[:500]),
            )
            conn.execute(
                "DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?",
                (self.LOG_KEEP,),
            )
            conn.commit()

    def recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]
