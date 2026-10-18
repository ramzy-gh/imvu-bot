"""Local JSON for testing, or optional external PostgreSQL for rebuild durability."""
import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, kind, local_path):
        self.path = Path(local_path)
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        namespace = os.getenv("STATE_NAMESPACE", "ramsei-" + os.getenv("IMVU_ROOM_ID", "330286535-177"))
        self.key = namespace + ":" + kind

    def database(self, value=None, write=False):
        try:
            import psycopg
            with psycopg.connect(self.database_url, connect_timeout=8, sslmode="require",
                                  options="-c statement_timeout=8000") as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS ramsei_state (state_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                if write:
                    conn.execute("INSERT INTO ramsei_state (state_key, payload) VALUES (%s, %s) "
                        "ON CONFLICT (state_key) DO UPDATE SET payload = EXCLUDED.payload",
                        (self.key, json.dumps(value)))
                    return None
                row = conn.execute("SELECT payload FROM ramsei_state WHERE state_key = %s", (self.key,)).fetchone()
                return json.loads(row[0]) if row else None
        except Exception:
            # Never log connection strings, passwords or session data from DB errors.
            raise OSError("External state database unavailable; no local fallback was used") from None

    def load(self):
        if self.database_url:
            return self.database()
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, value):
        if self.database_url:
            return self.database(value, write=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        descriptor = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
            os.chmod(temp, 0o600)
            temp.replace(self.path)
        finally:
            if temp.exists():
                temp.unlink()
