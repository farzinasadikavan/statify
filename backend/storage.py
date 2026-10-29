"""Atomic, owner-only storage for the single-user Statify installation."""
import json
import os
import sqlite3
from pathlib import Path

class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        # Create with restrictive permissions before SQLite opens the file.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')

    def load(self):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT payload FROM session WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, state):
        payload = json.dumps(state, ensure_ascii=False)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO session VALUES (1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (payload,))
