import json
import os
import sqlite3
from pathlib import Path

DATA = Path(os.getenv("BENCHFORGE_DATA", "./data")).resolve()
DATABASE = DATA / "benchforge.db"
ARTIFACTS = DATA / "runs"

ACTIVE = {
    "QUEUED", "PREPARING", "BASELINE", "MODEL", "PATCH", "EVALUATING"
}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS runs_created_at
            ON runs(created_at DESC)
        """)


def save(record):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO runs(id, created_at, status, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, payload=excluded.payload
            """,
            (
                record["id"],
                record["created_at"],
                record["status"],
                json.dumps(record),
            ),
        )


def get(run_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT payload FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def list_runs():
    with connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM runs ORDER BY created_at DESC"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def recover_interrupted():
    for record in list_runs():
        if record["status"] in ACTIVE:
            record["status"] = "INTERRUPTED"
            record["errors"].append("Controller stopped before this run finished.")
            save(record)
