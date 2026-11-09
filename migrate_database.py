"""Copy existing bot state into PostgreSQL without deleting local originals.

Run only in the stopped bot's container, with DATABASE_URL set privately.
Never pass a connection URL on the command line or print state payloads.
"""
import argparse
import json
import os
from pathlib import Path

from state_store import StateStore


def read_sources():
    permissions = Path(os.getenv("PERMISSIONS_PATH", "/app/data/ramsei/permissions.json"))
    paths = {
        "permissions": permissions,
        "rooms": Path(os.getenv("ROOMS_PATH", str(permissions.with_name("rooms.json")))),
        "session:" + os.getenv("IMVU_USERNAME", "Ramsei").lower():
            Path(os.getenv("SESSION_PATH", "/app/data/ramsei/imvu-session.json")),
        "telegram-bridge": Path(os.getenv("TELEGRAM_STATE_PATH",
                                      str(permissions.with_name("telegram-bridge.json")))),
    }
    result = {}
    for kind, path in paths.items():
        if not path.exists():
            if kind in {"permissions", "rooms"}:
                raise ValueError("Required local settings missing; migration cancelled")
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid local state; migration cancelled")
        key = StateStore(kind, path).key
        result[key] = value
    return result


def copy_state(conn, sources):
    """Caller owns one transaction: any conflict rolls back the entire import."""
    conn.execute("CREATE TABLE IF NOT EXISTS ramsei_state (state_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    conn.execute("LOCK TABLE ramsei_state IN EXCLUSIVE MODE")
    for key, value in sources.items():
        row = conn.execute("SELECT payload FROM ramsei_state WHERE state_key = %s", (key,)).fetchone()
        if row is not None:
            if json.loads(row[0]) != value:
                raise ValueError("Database contains different state; nothing was overwritten")
            continue
        conn.execute("INSERT INTO ramsei_state (state_key, payload) VALUES (%s, %s)",
                     (key, json.dumps(value)))
    for key, value in sources.items():
        row = conn.execute("SELECT payload FROM ramsei_state WHERE state_key = %s", (key,)).fetchone()
        if row is None or json.loads(row[0]) != value:
            raise ValueError("Database verification failed; migration cancelled")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Copy and verify state; default only checks local files")
    parser.add_argument("--bot-stopped", action="store_true", help="Confirm all bot workers are stopped")
    args = parser.parse_args()
    try:
        sources = read_sources()
        if not args.apply:
            print(f"Local state checked: {len(sources)} records. No database changes made.")
            return 0
        if not args.bot_stopped:
            raise ValueError("Stop all bot workers before applying migration")
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            raise ValueError("Set DATABASE_URL privately before migration")
        import psycopg
        with psycopg.connect(url, connect_timeout=8, sslmode="require",
                             options="-c statement_timeout=8000") as conn:
            copy_state(conn, sources)
        print(f"Migration verified: {len(sources)} records. Local originals retained.")
        return 0
    except Exception:
        # Driver exceptions can contain passwords, URLs or payloads. No raw error logging.
        print("Migration failed or prerequisites missing. Database transaction rolled back; local files retained.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
