import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .agent_protocol import utc_now


class AgentStore:
    def __init__(self, root: str):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.db_path = self.root / "agent.sqlite3"
        self.credentials_path = self.root / "credentials.json"
        self._initialize()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(str(self.db_path), timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self):
        with self.connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS commands (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    body TEXT NOT NULL,
                    state TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    operation_id TEXT NOT NULL DEFAULT '',
                    sequence INTEGER NOT NULL DEFAULT 0,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def load_credentials(self) -> Optional[Dict[str, str]]:
        agent_id = self.get_meta("credential.agent_id")
        secret = self.get_meta("credential.secret")
        if agent_id and secret:
            return {"agent_id": agent_id, "secret": secret}
        try:
            value = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or not value.get("agent_id") or not value.get("secret"):
            return None
        migrated = {"agent_id": str(value["agent_id"]), "secret": str(value["secret"])}
        self.save_credentials(migrated["agent_id"], migrated["secret"])
        return migrated

    def save_credentials(self, agent_id: str, secret: str):
        with self.connection() as connection:
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("credential.agent_id", str(agent_id)),
                    ("credential.secret", str(secret)),
                ),
            )

    def stage_credentials(self, operation_id: str, agent_id: str, secret: str, rotation_id: str):
        values = {
            "rotation.operation_id": operation_id,
            "rotation.agent_id": agent_id,
            "rotation.secret": secret,
            "rotation.rotation_id": rotation_id,
        }
        with self.connection() as connection:
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                tuple(values.items()),
            )

    def pending_credentials(self) -> Optional[Dict[str, str]]:
        value = {
            "operation_id": self.get_meta("rotation.operation_id"),
            "agent_id": self.get_meta("rotation.agent_id"),
            "secret": self.get_meta("rotation.secret"),
            "rotation_id": self.get_meta("rotation.rotation_id"),
        }
        return value if all(value.values()) else None

    def promote_pending_credentials(self):
        pending = self.pending_credentials()
        if not pending:
            return
        with self.connection() as connection:
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("credential.agent_id", pending["agent_id"]),
                    ("credential.secret", pending["secret"]),
                ),
            )
            connection.execute("DELETE FROM metadata WHERE key LIKE 'rotation.%'")

    def clear_pending_credentials(self):
        with self.connection() as connection:
            connection.execute("DELETE FROM metadata WHERE key LIKE 'rotation.%'")

    def enqueue_command(self, command: Dict[str, Any]) -> bool:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO commands "
                "(operation_id, action, body, state, received_at, updated_at) "
                "VALUES (?, ?, ?, 'received', ?, ?)",
                (
                    command["operation_id"],
                    command["action"],
                    json.dumps(command, sort_keys=True),
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def has_running_command(self) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM commands WHERE state='running' LIMIT 1"
            ).fetchone()
        return bool(row)

    def accepted_command(self) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT body FROM commands WHERE state='accepted' ORDER BY received_at LIMIT 1"
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def has_pending_event(self, operation_id: str, state: str) -> bool:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT body FROM outbox WHERE kind='event' AND operation_id=?",
                (operation_id,),
            ).fetchall()
        return any(json.loads(row["body"]).get("state") == state for row in rows)

    def set_command_state(self, operation_id: str, state: str):
        with self.connection() as connection:
            connection.execute(
                "UPDATE commands SET state=?, updated_at=? WHERE operation_id=?",
                (state, utc_now(), operation_id),
            )

    def command_state(self, operation_id: str) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT state FROM commands WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return str(row["state"]) if row else ""

    def next_received(self) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT body FROM commands WHERE state='received' ORDER BY received_at LIMIT 1"
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def running_commands(self, action: Optional[str] = None) -> Iterable[Dict[str, Any]]:
        query = "SELECT body FROM commands WHERE state IN ('accepted', 'running')"
        params = ()
        if action:
            query += " AND action=?"
            params = (action,)
        query += " ORDER BY received_at"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def transition(self, operation_id: str, state: str, detail: Optional[Dict[str, Any]] = None):
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT sequence FROM commands WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not row:
                return
            sequence = int(row["sequence"]) + 1
            connection.execute(
                "UPDATE commands SET state=?, sequence=?, updated_at=? WHERE operation_id=?",
                (state, sequence, now, operation_id),
            )
            event = {
                "schema_version": 1,
                "operation_id": operation_id,
                "sequence": sequence,
                "state": state,
                "observed_at": now,
                "detail": detail or {},
            }
            connection.execute(
                "INSERT INTO outbox (kind, operation_id, sequence, body, created_at) "
                "VALUES ('event', ?, ?, ?, ?)",
                (operation_id, sequence, json.dumps(event, sort_keys=True), now),
            )

    def queue_outbox(self, kind: str, body: Dict[str, Any], operation_id: str = ""):
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO outbox (kind, operation_id, body, created_at) VALUES (?, ?, ?, ?)",
                (kind, operation_id, json.dumps(body, sort_keys=True), utc_now()),
            )

    def pending_outbox(self, limit: int = 25):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, kind, operation_id, sequence, body FROM outbox ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) | {"body": json.loads(row["body"])} for row in rows]

    def acknowledge_outbox(self, item_id: int):
        with self.connection() as connection:
            connection.execute("DELETE FROM outbox WHERE id=?", (item_id,))

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str):
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
