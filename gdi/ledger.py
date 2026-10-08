"""Durable execution/publication state; never infer execution from queue filenames."""

import sqlite3

from .exchange import decode, encode


class Ledger:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL, repository_id TEXT NOT NULL, request BLOB NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL, process BLOB)")
        self.db.commit()

    def discover(self, req, raw, run_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(job_id,repository_id,request,run_id,state) VALUES(?,?,?,?, 'DISCOVERED')",
                            (req["job_id"], req["repository_id"], raw, run_id))
        row = self.get(req["job_id"])
        if row["raw"] != raw:
            from .git import GdiError
            raise GdiError("immutable CI request differs from local ledger")
        return row

    def get(self, jid):
        value = self.db.execute("SELECT job_id,repository_id,request,run_id,state,process FROM jobs WHERE job_id=?", (jid,)).fetchone()
        if value is None:
            return None
        return dict(zip(("job_id", "repository_id", "raw", "run_id", "state", "process"), value))

    def update(self, jid, state, process=None):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?, process=? WHERE job_id=?",
                            (state, encode(process) if process else None, jid))

    def pending(self):
        return [self.get(value[0]) for value in self.db.execute("SELECT job_id FROM jobs WHERE state!='PUBLISHED' ORDER BY sequence").fetchall()]

    def close(self):
        self.db.close()
