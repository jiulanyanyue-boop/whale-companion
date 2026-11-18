"""M12 主机侧持久化：固定八张表 + Run 状态机 + 幂等 effect + 审计落库。

规范来源：``docs/modules/M12-recovery-audit-product.md`` 与
``M12-recovery-audit-development.md``。

**向后兼容是第一铁律**：``plugin.py`` 与 ``onebot_lifecycle.py`` 都在调用
``add_message`` / ``message_owner`` / ``effect`` / ``save_effect``，这四个方法的
签名与语义**一个字节都没改**。所有新增能力都是**纯追加**。

固定表（M12 开发文档「SQLite 表」）：::

    owner_config  devices  inbox  runs  effects  schedules  assets  audit_events

新增的两件事（M03 点名需求）：

1. ``save_audit(...)`` —— ``audit_events`` 表此前只有建表语句、没有写入 API，
   M03 只能用 effects 伪装审计。现在有了真正的落库入口。
2. ``add_effect(...)`` + 显式 ``UNIQUE INDEX`` —— ``save_effect`` 是
   ``INSERT OR REPLACE``（覆盖写），出站幂等只在单进程内成立；
   ``add_effect`` 用 ``INSERT OR IGNORE`` 且依赖数据库级唯一约束，
   跨进程也成立。

禁止项（M12 开发文档「禁止项」）：状态不得只存内存；不得用日志文本判断任务是否
完成；不得恢复不可逆副作用而不重新确认；不得把用户媒体内容写入审计表。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Run 状态机：RECEIVED → PLANNED → RUNNING → SUCCEEDED，RUNNING 可分叉。
# ---------------------------------------------------------------------------

RUN_RECEIVED = "RECEIVED"
RUN_PLANNED = "PLANNED"
RUN_RUNNING = "RUNNING"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_WAITING_DEVICE = "WAITING_DEVICE"
RUN_NEEDS_UNLOCK = "NEEDS_UNLOCK"
RUN_FAILED = "FAILED"
RUN_DENIED = "DENIED"
RUN_CANCELLED = "CANCELLED"

RUN_STATES: frozenset[str] = frozenset({
    RUN_RECEIVED,
    RUN_PLANNED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_WAITING_DEVICE,
    RUN_NEEDS_UNLOCK,
    RUN_FAILED,
    RUN_DENIED,
    RUN_CANCELLED,
})

#: 启动时必须要扫描并恢复的三个非终态（M12 恢复算法第 1 步）。
RECOVERABLE_RUN_STATES: frozenset[str] = frozenset({
    RUN_RUNNING,
    RUN_WAITING_DEVICE,
    RUN_NEEDS_UNLOCK,
})

#: 终态不可再离开；恢复不得把终态「救活」。
TERMINAL_RUN_STATES: frozenset[str] = frozenset({
    RUN_SUCCEEDED,
    RUN_FAILED,
    RUN_DENIED,
    RUN_CANCELLED,
})

#: 合法迁移。RUNNING 的分叉正是文档给出的那张图。
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_RECEIVED: frozenset({RUN_PLANNED, RUN_CANCELLED}),
    RUN_PLANNED: frozenset({RUN_RUNNING, RUN_CANCELLED}),
    RUN_RUNNING: frozenset({
        RUN_SUCCEEDED,
        RUN_WAITING_DEVICE,
        RUN_NEEDS_UNLOCK,
        RUN_FAILED,
        RUN_DENIED,
        RUN_CANCELLED,
    }),
    # 恢复路径：等待态可以继续推进，也可以就地终结。
    RUN_WAITING_DEVICE: frozenset({RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED}),
    RUN_NEEDS_UNLOCK: frozenset({RUN_RUNNING, RUN_FAILED, RUN_DENIED, RUN_CANCELLED}),
    RUN_SUCCEEDED: frozenset(),
    RUN_FAILED: frozenset(),
    RUN_DENIED: frozenset(),
    RUN_CANCELLED: frozenset(),
}


# ---------------------------------------------------------------------------
# 保留期（M12 产品文档「清理」）
# ---------------------------------------------------------------------------

DAY_MS = 24 * 60 * 60 * 1000
#: 任务 effect 保留 30 天。
EFFECT_TTL_MS = 30 * DAY_MS
#: 审计保留 90 天。
AUDIT_TTL_MS = 90 * DAY_MS
#: 临时媒体保留 24 小时（与 M08 的 ``TEMP_TTL_MS`` 同一数值，这里只用于统一清理）。
MEDIA_TTL_MS = 24 * 60 * 60 * 1000


class StorageError(ValueError):
    """存储层拒绝（非法状态迁移、非法参数）。"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskStore:
    """主机固定表存储。构造签名与既有行为完全不变。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS inbox (
                message_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                body TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owner_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                owner_user_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                public_key TEXT NOT NULL DEFAULT '',
                connection_json TEXT NOT NULL DEFAULT '{}',
                capabilities_json TEXT NOT NULL DEFAULT '{}',
                last_seen_at INTEGER NOT NULL DEFAULT 0,
                last_counter INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                current_step INTEGER NOT NULL DEFAULT 0,
                lease_until INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                error_code TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS effects (
                idempotency_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        self._migrate()
        self._db.commit()

    # -- 迁移：老库补齐新列 + 数据库级唯一约束 ---------------------------------
    def _migrate(self) -> None:
        """对已存在的老库补齐 ``runs.error_code`` 并补建唯一索引。"""
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
        if "error_code" not in existing:
            self._db.execute("ALTER TABLE runs ADD COLUMN error_code TEXT NOT NULL DEFAULT ''")
        device_columns = {row[1] for row in self._db.execute("PRAGMA table_info(devices)")}
        if "last_counter" not in device_columns:
            # 设备计数器是 M12 恢复算法第 4 步的持久载体：它必须**只能前进**，
            # 所以存在固定表 devices 里、由 SQL 的 WHERE 条件保证单调，
            # 而不是放在任何内存字典里。
            self._db.execute("ALTER TABLE devices ADD COLUMN last_counter INTEGER NOT NULL DEFAULT 0")
        # 主键已是 UNIQUE，这里再补一条显式唯一索引：
        # 「出站幂等在单进程外也成立」要有数据库级的约束兜底，
        # 不能只靠 INSERT OR IGNORE 的语义自证。
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_effects_idempotency_key ON effects(idempotency_key)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_audit_events_type_time ON audit_events(event_type, created_at)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status)")

    # ---------------------------------------------------------------------
    # 既有 API（签名与语义一字未改；plugin.py / onebot_lifecycle.py 依赖）
    # ---------------------------------------------------------------------
    def add_message(self, message_id: str, user_id: str, body: str, created_at: int) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO inbox(message_id,user_id,body,created_at) VALUES(?,?,?,?)",
                (message_id, user_id, body, created_at),
            )
            self._db.commit()
            return cur.rowcount == 1

    def effect(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT result_json FROM effects WHERE idempotency_key=?", (key,)).fetchone()
            return None if row is None else str(row[0])

    def message_owner(self, message_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT user_id FROM inbox WHERE message_id=?", (message_id,)).fetchone()
            return None if row is None else str(row[0])

    def save_effect(self, key: str, result_json: str, created_at: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO effects(idempotency_key,result_json,created_at) VALUES(?,?,?)",
                (key, result_json, created_at),
            )
            self._db.commit()

    # ---------------------------------------------------------------------
    # 新增：原子「不存在才写」的 effect（M03 需求 2）
    # ---------------------------------------------------------------------
    def add_effect(self, key: str, result_json: str, created_at: int) -> bool:
        """不存在才写。返回 ``True`` 表示本次确实落库，``False`` 表示已有同键 effect。

        与 ``save_effect`` 的差别只有一处、但很关键：``save_effect`` 覆盖写，
        重试时会用后到的结果盖掉先进的结果；``add_effect`` 永远保留先写的那一份，
        因此「同一幂等键只产生一次副作用」在**跨进程**场景也成立。
        """
        if not isinstance(key, str) or not key:
            raise StorageError("idempotency key is required")
        if not isinstance(result_json, str):
            raise StorageError("result payload must be serialized text")
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO effects(idempotency_key,result_json,created_at) VALUES(?,?,?)",
                (key, result_json, int(created_at)),
            )
            self._db.commit()
            return cur.rowcount == 1

    def has_effect(self, key: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM effects WHERE idempotency_key=?", (key,)
            ).fetchone() is not None

    # ---------------------------------------------------------------------
    # 新增：审计落库（M03 需求 1）
    # ---------------------------------------------------------------------
    def save_audit(
        self,
        event_type: str,
        actor: str,
        message_id: str,
        detail: Any,
        created_at: int,
    ) -> str:
        """把一条**已脱敏**的审计事件写进 ``audit_events``，返回 ``event_id``。

        脱敏是 :mod:`astrbot_whale_device.audit` 的职责，本方法只负责落库；
        调用方不得把未脱敏的 detail 传进来。
        """
        if not isinstance(event_type, str) or not event_type.strip():
            raise StorageError("audit event_type is required")
        event_id = uuid.uuid4().hex
        payload = detail
        if not isinstance(payload, str):
            payload = _json(payload)
        with self._lock:
            self._db.execute(
                "INSERT INTO audit_events(event_id,event_type,actor_user_id,message_id,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (event_id, event_type, str(actor or ""), str(message_id or ""), payload, int(created_at)),
            )
            self._db.commit()
        return event_id

    def audit_events(
        self,
        event_type: str | None = None,
        *,
        message_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type=?")
            params.append(event_type)
        if message_id is not None:
            clauses.append("message_id=?")
            params.append(message_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT event_id,event_type,actor_user_id,message_id,detail_json,created_at"
                f" FROM audit_events{where} ORDER BY created_at ASC, event_id ASC LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"])
            except (json.JSONDecodeError, TypeError):
                detail = {}
            out.append({
                "event_id": str(row["event_id"]),
                "event_type": str(row["event_type"]),
                "actor_user_id": str(row["actor_user_id"]),
                "message_id": str(row["message_id"]),
                "detail": detail,
                "created_at": int(row["created_at"]),
            })
        return out

    # ---------------------------------------------------------------------
    # 新增：Run 状态机
    # ---------------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        message_id: str,
        plan: Any,
        updated_at: int,
        *,
        status: str = RUN_RECEIVED,
    ) -> dict[str, Any]:
        """建一条 run。``RECEIVED → PLANNED → RUNNING → SUCCEEDED`` 的起点是 RECEIVED。"""
        if not isinstance(run_id, str) or not run_id:
            raise StorageError("run_id is required")
        if not isinstance(message_id, str) or not message_id:
            raise StorageError("message_id is required")
        if status not in RUN_STATES:
            raise StorageError(f"unknown run status: {status!r}")
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO runs(run_id,message_id,status,plan_json,current_step,lease_until,updated_at,error_code)"
                " VALUES(?,?,?,?,0,0,?, '')",
                (run_id, message_id, status, _json(plan), int(updated_at)),
            )
            self._db.commit()
        run = self.run(run_id)
        if run is None:
            raise StorageError("run could not be created")
        return run

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id,message_id,status,plan_json,current_step,lease_until,updated_at,error_code"
                " FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return None if row is None else _run_row(row)

    def runs_in_status(self, statuses: frozenset[str] | set[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """按状态扫描 run（恢复算法第 1 步的入口）。"""
        wanted = sorted({str(item) for item in statuses})
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id,message_id,status,plan_json,current_step,lease_until,updated_at,error_code"
                f" FROM runs WHERE status IN ({placeholders}) ORDER BY updated_at ASC, run_id ASC",
                tuple(wanted),
            ).fetchall()
        return [_run_row(row) for row in rows]

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        step_index: int | None = None,
        error_code: str = "",
        updated_at: int,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """状态迁移。每次迁移都带 run_id / message_id / step_index / updated_at / 错误码。

        - 非法目标状态或非法迁移 → :class:`StorageError`；
        - 终态不可离开；
        - ``step_index`` **只前进不回退**（恢复不得重跑已完成的 step）；
        - 迁移本身写一条 ``run.transition`` 审计事件，不新增表。
        """
        if status not in RUN_STATES:
            raise StorageError(f"unknown run status: {status!r}")
        if isinstance(step_index, bool) or (step_index is not None and not isinstance(step_index, int)):
            raise StorageError("step_index must be an integer")
        with self._lock:
            current = self.run(run_id)
            if current is None:
                raise StorageError(f"unknown run: {run_id!r}")
            previous = str(current["status"])
            if expected_status is not None and previous != expected_status:
                raise StorageError(f"run {run_id!r} is {previous}, expected {expected_status}")
            allowed = RUN_TRANSITIONS.get(previous, frozenset())
            if status not in allowed:
                raise StorageError(f"illegal run transition: {previous} -> {status}")
            target_step = int(current["current_step"]) if step_index is None else int(step_index)
            if target_step < int(current["current_step"]):
                raise StorageError("current_step must not move backwards")
            self._db.execute(
                "UPDATE runs SET status=?, current_step=?, updated_at=?, error_code=? WHERE run_id=?",
                (status, target_step, int(updated_at), str(error_code or ""), run_id),
            )
            self._db.commit()
            message_id = str(current["message_id"])
        # 迁移留痕：固定表清单里没有额外的迁移表，就用审计表承载。
        self.save_audit(
            "run.transition",
            "",
            message_id,
            {
                "run_id": run_id,
                "step_index": target_step,
                "from": previous,
                "to": status,
                "error_code": str(error_code or ""),
                "updated_at": int(updated_at),
            },
            int(updated_at),
        )
        updated = self.run(run_id)
        if updated is None:
            raise StorageError("run disappeared during transition")
        return updated

    def claim_lease(self, run_id: str, lease_until: int, updated_at: int) -> dict[str, Any]:
        """租约只能**延长**，绝不回退（恢复算法第 4 步）。"""
        with self._lock:
            current = self.run(run_id)
            if current is None:
                raise StorageError(f"unknown run: {run_id!r}")
            kept = max(int(current["lease_until"]), int(lease_until))
            self._db.execute(
                "UPDATE runs SET lease_until=?, updated_at=? WHERE run_id=?",
                (kept, int(updated_at), run_id),
            )
            self._db.commit()
        updated = self.run(run_id)
        if updated is None:
            raise StorageError("run disappeared during lease claim")
        return updated

    def advance_step(self, run_id: str, step_index: int, updated_at: int) -> dict[str, Any]:
        """只把 ``current_step`` 推到新的已完成步；状态不变、不回退、不写审计。

        恢复算法第 2/3 步用它在**不重做副作用**的前提下记录进度。
        """
        if isinstance(step_index, bool) or not isinstance(step_index, int):
            raise StorageError("step_index must be an integer")
        with self._lock:
            current = self.run(run_id)
            if current is None:
                raise StorageError(f"unknown run: {run_id!r}")
            if step_index < int(current["current_step"]):
                raise StorageError("current_step must not move backwards")
            self._db.execute(
                "UPDATE runs SET current_step=?, updated_at=? WHERE run_id=?",
                (step_index, int(updated_at), run_id),
            )
            self._db.commit()
        updated = self.run(run_id)
        if updated is None:
            raise StorageError("run disappeared during step advance")
        return updated

    # ---------------------------------------------------------------------
    # 新增：设备计数器（只前进，不回退）
    # ---------------------------------------------------------------------
    def device_counter(self, device_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT last_counter FROM devices WHERE device_id=?", (str(device_id),)
            ).fetchone()
        return 0 if row is None else int(row[0])

    def record_device_counter(self, device_id: str, counter: int, updated_at: int, *, public_key: str = "") -> int:
        """记下一个已接受的设备计数器。``INSERT .. ON CONFLICT DO UPDATE`` 且

        ``SET last_counter = MAX(last_counter, excluded.last_counter)`` ——
        单调性由 SQL 保证，恢复时**绝不回退**（重放窗口不能因为重启而打开）。
        """
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise StorageError("device counter must be a non-negative integer")
        with self._lock:
            self._db.execute(
                "INSERT INTO devices(device_id,public_key,last_seen_at,last_counter) VALUES(?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET"
                "   last_counter = MAX(devices.last_counter, excluded.last_counter),"
                "   last_seen_at = MAX(devices.last_seen_at, excluded.last_seen_at),"
                "   public_key = CASE WHEN excluded.public_key <> '' THEN excluded.public_key ELSE devices.public_key END",
                (str(device_id), str(public_key), int(updated_at), int(counter)),
            )
            self._db.commit()
        return self.device_counter(device_id)

    def device_record(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT device_id,public_key,connection_json,capabilities_json,last_seen_at,last_counter"
                " FROM devices WHERE device_id=?",
                (str(device_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "device_id": str(row["device_id"]),
            "public_key": str(row["public_key"]),
            "connection": json.loads(row["connection_json"] or "{}"),
            "capabilities": json.loads(row["capabilities_json"] or "{}"),
            "last_seen_at": int(row["last_seen_at"]),
            "last_counter": int(row["last_counter"]),
        }

    # ---------------------------------------------------------------------
    # 新增：保留期清理（effect 30 天 / 审计 90 天 / 临时媒体 24 小时）
    # ---------------------------------------------------------------------
    def purge_expired(self, now_ms: int) -> dict[str, int]:
        """按固定保留期清理。返回各类被删行数；``now_ms`` 必须由调用方注入（便于测试）。"""
        now = int(now_ms)
        with self._lock:
            effect_cutoff = now - EFFECT_TTL_MS
            audit_cutoff = now - AUDIT_TTL_MS
            media_cutoff = now - MEDIA_TTL_MS
            cur_effects = self._db.execute(
                "DELETE FROM effects WHERE created_at <= ?", (effect_cutoff,)
            )
            cur_audit = self._db.execute(
                "DELETE FROM audit_events WHERE created_at <= ?", (audit_cutoff,)
            )
            cur_media = self._db.execute(
                "DELETE FROM assets WHERE created_at <= ?", (media_cutoff,)
            )
            self._db.commit()
            return {
                "effects": max(0, int(cur_effects.rowcount)),
                "audit_events": max(0, int(cur_audit.rowcount)),
                "assets": max(0, int(cur_media.rowcount)),
            }

    def purge_message(self, message_id: str) -> dict[str, int]:
        """用户可立即清理本地会话（M12 产品文档「清理」）：删 inbox 与该消息的审计。"""
        with self._lock:
            inbox = self._db.execute("DELETE FROM inbox WHERE message_id=?", (message_id,))
            audit = self._db.execute("DELETE FROM audit_events WHERE message_id=?", (message_id,))
            self._db.commit()
            return {
                "inbox": max(0, int(inbox.rowcount)),
                "audit_events": max(0, int(audit.rowcount)),
            }

    # ---------------------------------------------------------------------
    # 只读辅助
    # ---------------------------------------------------------------------
    def owner_user_id(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT owner_user_id FROM owner_config WHERE id=1").fetchone()
            return None if row is None else str(row[0])

    def set_owner_user_id(self, owner_user_id: str, updated_at: int) -> None:
        if not isinstance(owner_user_id, str) or not owner_user_id:
            raise StorageError("owner_user_id is required")
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO owner_config(id,owner_user_id,updated_at) VALUES(1,?,?)",
                (owner_user_id, int(updated_at)),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        plan = json.loads(row["plan_json"])
    except (json.JSONDecodeError, TypeError):
        plan = {}
    return {
        "run_id": str(row["run_id"]),
        "message_id": str(row["message_id"]),
        "status": str(row["status"]),
        "plan": plan,
        "current_step": int(row["current_step"]),
        "lease_until": int(row["lease_until"]),
        "updated_at": int(row["updated_at"]),
        "error_code": str(row["error_code"]),
    }
