import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS import_batches (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_rows (
                    batch_id TEXT NOT NULL,
                    line_no INTEGER NOT NULL,
                    animal_id TEXT,
                    status TEXT NOT NULL,
                    record TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, line_no)
                );
                CREATE TABLE IF NOT EXISTS pedigree_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    animal_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    line_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    local_parents TEXT NOT NULL,
                    external_parents TEXT NOT NULL,
                    resolution TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conflicts_animal
                    ON pedigree_conflicts(animal_id, id);
                CREATE INDEX IF NOT EXISTS idx_conflicts_status
                    ON pedigree_conflicts(status, id);
            """)

    # ---------- generic helpers ----------

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_entity(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def _insert_entity(self, connection, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )
        return self._row_to_entity(connection, entity_id)

    def begin(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def create_entity(self, entity_id, kind, status, data, actor_id, connection=None):
        if connection is not None:
            return self._insert_entity(
                connection, entity_id, kind, status, data, actor_id
            )
        with self._connect() as connection:
            return self._insert_entity(
                connection, entity_id, kind, status, data, actor_id
            )

    def get_entity(self, entity_id, connection=None):
        def _query(conn):
            return self._row_to_entity(conn, entity_id)

        if connection is not None:
            return _query(connection)
        with self._connect() as connection:
            return _query(connection)

    def list_entities(self, kind=None, status=None, connection=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _query(conn):
            rows = conn.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
            return [self._entity_from_row(row) for row in rows]

        if connection is not None:
            return _query(connection)
        with self._connect() as connection:
            return _query(connection)

    def find_entities(self, kind, field, value, connection=None):
        return [
            entity
            for entity in self.list_entities(kind=kind, connection=connection)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(
        self, entity_id, expected_version, status, data, connection=None
    ):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)

        def _update(conn):
            row = conn.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            cursor = conn.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            if cursor.rowcount == 0:
                # 并发下另一连接抢先改了版本：本次确认失效。
                raise ConflictError(
                    "pairing confirmation lost the race: %s" % entity_id
                )
            return self._row_to_entity(conn, entity_id)

        if connection is not None:
            return _update(connection)
        owned = self._connect()
        try:
            owned.execute("BEGIN IMMEDIATE")
            result = _update(owned)
            owned.commit()
            return result
        except Exception:
            owned.rollback()
            raise
        finally:
            owned.close()

    # ---------- audit ----------

    def _insert_audit(
        self,
        connection,
        entity_id,
        actor_id,
        actor_role,
        action,
        from_status,
        to_status,
        detail,
    ):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def append_audit(
        self,
        entity_id,
        actor_id,
        actor_role,
        action,
        from_status,
        to_status,
        detail,
        connection=None,
    ):
        if connection is not None:
            return self._insert_audit(
                connection,
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                detail,
            )
        with self._connect() as connection:
            self._insert_audit(
                connection,
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                detail,
            )

    def list_audit(self, entity_id=None, action_like=None, connection=None):
        def _query(conn):
            sql = "SELECT * FROM audit_log"
            clauses = []
            params = []
            if entity_id:
                clauses.append("entity_id = ?")
                params.append(entity_id)
            if action_like:
                clauses.append("action LIKE ?")
                params.append(action_like)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id"
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "id": row["id"],
                    "entity_id": row["entity_id"],
                    "actor_id": row["actor_id"],
                    "actor_role": row["actor_role"],
                    "action": row["action"],
                    "from_status": row["from_status"],
                    "to_status": row["to_status"],
                    "detail": json.loads(row["detail"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

        if connection is not None:
            return _query(connection)
        with self._connect() as connection:
            return _query(connection)

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id, connection=None):
        def _write(conn):
            conn.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

        if connection is not None:
            return _write(connection)
        with self._connect() as connection:
            _write(connection)

    # ---------- import batches ----------

    def create_import_batch(
        self, batch_id, source, total, summary, actor_id, connection=None
    ):
        now = utcnow()

        def _write(conn):
            conn.execute(
                "INSERT INTO import_batches(id, source, status, total, summary, created_by, created_at, updated_at) "
                "VALUES (?, ?, 'in_progress', ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    source,
                    total,
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    actor_id,
                    now,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO import_rows(batch_id, line_no, animal_id, status, record, error, updated_at) "
                "VALUES (?, ?, NULL, 'pending', ?, NULL, ?)",
                [
                    (batch_id, index, json.dumps(record, ensure_ascii=False), now)
                    for index, record in enumerate(_summary_records(summary), start=1)
                ],
            )
            return self.get_import_batch(batch_id, connection=conn)

        if connection is not None:
            return _write(connection)
        with self._connect() as conn:
            return _write(conn)

    def get_import_batch(self, batch_id, connection=None):
        def _query(conn):
            row = conn.execute(
                "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "source": row["source"],
                "status": row["status"],
                "total": int(row["total"]),
                "summary": json.loads(row["summary"]),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def list_import_batches(self, connection=None):
        def _query(conn):
            rows = conn.execute(
                "SELECT * FROM import_batches ORDER BY created_at, id"
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "source": row["source"],
                    "status": row["status"],
                    "total": int(row["total"]),
                    "summary": json.loads(row["summary"]),
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def mark_import_batch(self, batch_id, status, summary, connection=None):
        def _write(conn):
            conn.execute(
                "UPDATE import_batches SET status = ?, summary = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                    batch_id,
                ),
            )
            return self.get_import_batch(batch_id, connection=conn)

        if connection is not None:
            return _write(connection)
        with self._connect() as conn:
            return _write(conn)

    def list_import_rows(self, batch_id, status=None, connection=None):
        def _query(conn):
            sql = "SELECT * FROM import_rows WHERE batch_id = ?"
            params = [batch_id]
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY line_no"
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "batch_id": row["batch_id"],
                    "line_no": int(row["line_no"]),
                    "animal_id": row["animal_id"],
                    "status": row["status"],
                    "record": json.loads(row["record"]),
                    "error": row["error"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def mark_import_row(
        self,
        batch_id,
        line_no,
        status,
        animal_id=None,
        error=None,
        connection=None,
    ):
        def _write(conn):
            conn.execute(
                "UPDATE import_rows SET status = ?, animal_id = ?, error = ?, updated_at = ? "
                "WHERE batch_id = ? AND line_no = ?",
                (status, animal_id, error, utcnow(), batch_id, line_no),
            )

        if connection is not None:
            return _write(connection)
        with self._connect() as conn:
            _write(conn)

    # ---------- pedigree conflicts ----------

    def add_conflict(
        self,
        animal_id,
        batch_id,
        line_no,
        local_parents,
        external_parents,
        connection=None,
    ):
        def _write(conn):
            now = utcnow()
            cursor = conn.execute(
                "INSERT INTO pedigree_conflicts("
                "animal_id, batch_id, line_no, status, local_parents, external_parents, "
                "resolution, resolved_by, resolved_at, created_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, NULL, NULL, NULL, ?)",
                (
                    animal_id,
                    batch_id,
                    line_no,
                    json.dumps(local_parents, ensure_ascii=False, sort_keys=True),
                    json.dumps(external_parents, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            return cursor.lastrowid

        if connection is not None:
            return _write(connection)
        with self._connect() as conn:
            return _write(conn)

    def find_open_conflict(
        self, animal_id, external_parents, connection=None
    ):
        needle = json.dumps(external_parents, ensure_ascii=False, sort_keys=True)

        def _query(conn):
            rows = conn.execute(
                "SELECT * FROM pedigree_conflicts WHERE animal_id = ? AND status = 'open' ORDER BY id",
                (animal_id,),
            ).fetchall()
            for row in rows:
                if row["external_parents"] == needle:
                    return _conflict_from_row(row)
            return None

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def get_conflict(self, conflict_id, connection=None):
        def _query(conn):
            row = conn.execute(
                "SELECT * FROM pedigree_conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()
            return _conflict_from_row(row) if row else None

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def list_conflicts(self, status=None, animal_id=None, connection=None):
        clauses = []
        params = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if animal_id:
            clauses.append("animal_id = ?")
            params.append(animal_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _query(conn):
            rows = conn.execute(
                "SELECT * FROM pedigree_conflicts" + where + " ORDER BY id", params
            ).fetchall()
            return [_conflict_from_row(row) for row in rows]

        if connection is not None:
            return _query(connection)
        with self._connect() as conn:
            return _query(conn)

    def resolve_conflict(
        self,
        conflict_id,
        resolution,
        chosen_parents,
        actor_id,
        connection=None,
    ):
        def _write(conn):
            conn.execute(
                "UPDATE pedigree_conflicts SET status = 'resolved', resolution = ?, "
                "resolved_by = ?, resolved_at = ? WHERE id = ? AND status = 'open'",
                (
                    json.dumps(
                        {"decision": resolution, "parents": chosen_parents},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    actor_id,
                    utcnow(),
                    conflict_id,
                ),
            )
            return self.get_conflict(conflict_id, connection=conn)

        if connection is not None:
            return _write(connection)
        with self._connect() as conn:
            return _write(conn)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True


def _summary_records(summary):
    # summary 保留原始导入载荷（records 数组），用于初始化逐行断点。
    return summary.get("records", []) if isinstance(summary, dict) else []


def _conflict_from_row(row):
    resolution = json.loads(row["resolution"]) if row["resolution"] else None
    return {
        "id": int(row["id"]),
        "animal_id": row["animal_id"],
        "batch_id": row["batch_id"],
        "line_no": int(row["line_no"]),
        "status": row["status"],
        "local_parents": json.loads(row["local_parents"]),
        "external_parents": json.loads(row["external_parents"]),
        "resolution": resolution,
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
    }
