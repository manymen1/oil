from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .clock import instant, stamp, utc_now
from .schema import canonical, digest, to_dict


class Journal:
    """Append-only records with transactional cursors. SQLite serializes writers.

    Each component has its own database; BEGIN IMMEDIATE also protects budget
    reservations from simultaneous workers. Read connections never take ownership.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ValueError("unsupported oil journal schema version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS record_kind ON records(kind,seq);
                CREATE INDEX IF NOT EXISTS story_source ON records(json_extract(payload,'$.source_id'))
                    WHERE kind='story_revision';
                CREATE TABLE IF NOT EXISTS parse_work (
                    observation_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    observation_seq INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','parsed','failed')),
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS parse_work_pending ON parse_work(source_id,observation_seq)
                    WHERE state='pending';
                CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON records
                    BEGIN SELECT RAISE(ABORT, 'records are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON records
                    BEGIN SELECT RAISE(ABORT, 'records are immutable'); END;
                PRAGMA user_version=1;
            """)

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def connect(self):
        db = self._open()
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def append(self, kind: str, payload: dict, *, available_at: str | None = None,
               record_id: str | None = None, db=None) -> str:
        at = instant(available_at or utc_now()).isoformat(timespec="microseconds")
        rid = record_id or str(uuid.uuid4())
        if db is None:
            with self.transaction() as connection:
                return self.append(kind, payload, available_at=at, record_id=rid, db=connection)
        body = canonical(payload)
        old = db.execute("SELECT kind,payload,available_at FROM records WHERE id=?", (rid,)).fetchone()
        if old:
            if old["kind"] != kind or old["payload"] != body or old["available_at"] != at:
                raise ValueError("immutable identity collision")
            return rid
        db.execute("INSERT INTO records(id,kind,available_at,payload,recorded_at) VALUES(?,?,?,?,?)",
                   (rid, kind, at, body, utc_now()))
        if kind == "observation":
            seq = db.execute("SELECT seq FROM records WHERE id=?", (rid,)).fetchone()[0]
            db.execute("INSERT OR IGNORE INTO parse_work VALUES(?,?,?,'pending',NULL)",
                       (rid, payload["source_id"], seq))
        elif kind == "parse_receipt":
            for ref in payload.get("input_revision_ids", []):
                db.execute("UPDATE parse_work SET state='parsed',reason=NULL WHERE observation_id=?", (ref,))
        return rid

    def index_legacy_parse_work(self, limit=500) -> dict:
        """Bounded one-time migration, reading metadata rather than raw bodies.

        The fence prevents recovery from reprocessing an old observation before
        its later parse receipt has been indexed. New writes maintain the queue
        transactionally; after migration, this only reads one cursor.
        """
        with self.transaction() as db:
            old = db.execute("SELECT value FROM cursors WHERE key='recovery:index'").fetchone()
            progress = json.loads(old[0]) if old else {
                "seq": 0, "through": db.execute("SELECT COALESCE(MAX(seq),0) FROM records").fetchone()[0]}
            if progress["seq"] >= progress["through"]:
                self.set_cursor(db, "recovery:index", progress)
                return {**progress, "complete": True, "scanned": 0}
            rows = db.execute("""SELECT seq,id,kind,
                json_extract(payload,'$.source_id') AS source_id,
                json_extract(payload,'$.input_revision_ids') AS refs,
                json_extract(payload,'$.status') AS status
                FROM records WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?""",
                (progress["seq"], progress["through"], limit)).fetchall()
            for row in rows:
                if row["kind"] == "observation":
                    db.execute("INSERT OR IGNORE INTO parse_work VALUES(?,?,?,'pending',NULL)",
                               (row["id"], row["source_id"], row["seq"]))
                elif row["kind"] == "parse_receipt":
                    for rid in json.loads(row["refs"] or "[]"):
                        db.execute("UPDATE parse_work SET state='parsed',reason=NULL WHERE observation_id=?", (rid,))
                elif row["kind"] == "source_health" and row["status"] not in {
                        "OK", "EMPTY", "UNCHANGED", "OK_DETAIL_INCOMPLETE", "RECOVERED_UNPARSED_RESPONSE"}:
                    for rid in json.loads(row["refs"] or "[]"):
                        db.execute("UPDATE parse_work SET state='failed',reason=? WHERE observation_id=? AND state='pending'",
                                   (row["status"], rid))
            progress["seq"] = rows[-1]["seq"] if rows else progress["through"]
            self.set_cursor(db, "recovery:index", progress)
            return {**progress, "complete": progress["seq"] >= progress["through"], "scanned": len(rows)}

    def pending_parse_work(self, source_ids, *, limit=8, migration=None):
        migration = migration or self.index_legacy_parse_work()
        if not source_ids:
            return []
        # One indexed seek per source avoids sorting/scanning a global queue.
        rows = []
        with self.connect() as db:
            for sid in source_ids:
                rows.extend(dict(r) for r in db.execute("""SELECT observation_id,observation_seq FROM parse_work
                    WHERE state='pending' AND source_id=? AND observation_seq>?
                    ORDER BY observation_seq LIMIT ?""",
                    (sid, 0 if migration["complete"] else migration["through"], limit)))
        return sorted(rows, key=lambda r: r["observation_seq"])[:limit]

    def fail_parse_work(self, observation_id, reason, *, health=None):
        with self.transaction() as db:
            if health is not None:
                self.append("source_health", health, db=db)
            db.execute("UPDATE parse_work SET state='failed',reason=? WHERE observation_id=? AND state='pending'",
                       (reason, observation_id))

    def records(self, kind: str | None = None, *, through: str | None = None) -> list[dict]:
        conditions, args = [], []
        if kind:
            conditions.append("kind=?")
            args.append(kind)
        if through:
            conditions.append("available_at<=?")
            args.append(instant(through).isoformat(timespec="microseconds"))
        sql = "SELECT * FROM records" + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY seq"
        with self.connect() as db:
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in db.execute(sql, args)]

    def get(self, rid: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def cursor(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_cursor(db, key: str, value) -> None:
        db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, canonical(value)))

    def reserve_attempt(self, limit: int, at: str | None = None) -> bool:
        day = instant(at or utc_now()).date().isoformat()
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM budget WHERE day=?", (day,)).fetchone()
            count = row[0] if row else 0
            if count >= limit:
                return False
            db.execute("INSERT INTO budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET attempts=excluded.attempts",
                       (day, count + 1))
        return True

    def budget(self) -> dict:
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT day,attempts FROM budget ORDER BY day")}

    def capture(self, source: dict, response: dict) -> str:
        """Durable raw bytes before any parser sees them. Repeated receipts persist."""
        body = response["body"]
        payload = {key: value for key, value in response.items() if key != "body"}
        payload.update(source_id=source["id"], source_role=source["role"],
                       body_b64=base64.b64encode(body).decode(),
                       sha256=hashlib.sha256(body).hexdigest(), source_policy=digest(source))
        rid = self.append("observation", payload, available_at=response["received"]["utc"])
        committed = stamp()
        self.append("commit_receipt", {"input_revision_ids": [rid], "commit_observed": committed,
                                     "receive_to_commit_ms": (committed["monotonic_ns"] - response["received"]["monotonic_ns"]) / 1e6})
        return rid

    def accept_items(self, source: dict, observation_id: str, items: list, cursor: dict) -> list[str]:
        observation = self.get(observation_id)
        if observation is None:
            raise ValueError("missing raw observation")
        output = []
        with self.transaction() as db:
            at = utc_now()
            # Scheduling/HTTP validators can exist after an error, empty feed or
            # 304. Only a previously captured story establishes a content baseline.
            baseline = db.execute(
                "SELECT payload FROM records WHERE kind='story_revision' AND json_extract(payload,'$.source_id')=? ORDER BY seq LIMIT 1",
                (source["id"],)).fetchone()
            initial_snapshot = baseline is None
            if baseline:
                baseline_inputs = json.loads(baseline[0])["input_revision_ids"]
                baseline_seq = max(db.execute("SELECT seq FROM records WHERE id=?", (rid,)).fetchone()[0]
                                   for rid in baseline_inputs)
                # Recovery may parse the first response after a newer snapshot.
                # An unseen old GUID is still baseline content, not a fresh event.
                initial_snapshot = observation["seq"] <= baseline_seq
            for item in items:
                story = digest([source["id"], item.native_id])
                content = digest(to_dict(item))
                previous = db.execute("SELECT value FROM cursors WHERE key=?", ("story:" + story,)).fetchone()
                previous = json.loads(previous[0]) if previous else None
                if previous:
                    observed_seq = previous.get("observation_seq")
                    if observed_seq is None:  # Legacy story cursor.
                        old = json.loads(db.execute("SELECT payload FROM records WHERE id=?", (previous["revision_id"],)).fetchone()[0])
                        observed_seq = max(db.execute("SELECT seq FROM records WHERE id=?", (rid,)).fetchone()[0]
                                           for rid in old["input_revision_ids"])
                    if observation["seq"] < observed_seq:
                        # Late recovery preserves parsed content but must not
                        # supersede a more recently received version.
                        self.append("late_story_parse", {**to_dict(item), "source_id": source["id"],
                            "story_id": story, "content_hash": content, "exclusion": "OLDER_OBSERVATION",
                            "local_received_at": observation["payload"]["received"]["utc"],
                            "input_revision_ids": [observation_id, previous["revision_id"]]}, db=db)
                        continue
                if previous and previous["content_hash"] == content:
                    self.append("story_receipt", {"input_revision_ids": [observation_id, previous["revision_id"]],
                                                  "source_id": source["id"]}, db=db)
                    self.set_cursor(db, "story:" + story, {**previous, "observation_seq": observation["seq"]})
                    continue
                rid = digest([story, content, previous["revision_id"] if previous else None])
                payload = {**to_dict(item), "source_id": source["id"], "source_role": source["role"],
                           "source_type": source.get("source_type", source["role"]),
                           "source_profile": source.get("profile"),
                           "story_id": story, "content_hash": content,
                           "revision": previous["revision"] + 1 if previous else 1,
                           "supersedes_id": previous["revision_id"] if previous else None,
                           "input_revision_ids": [observation_id], "transform": "source-v1",
                           "observed_at": observation["available_at"],
                           "local_received_at": observation["payload"]["received"]["utc"],
                           "request_started_at": observation["payload"].get("started", {}).get("utc"),
                           "first_byte_at": (observation["payload"].get("first_byte") or {}).get("utc"),
                           "publisher_timestamp": item.published_at,
                           "initial_snapshot": initial_snapshot,
                           "origin_status": "attributed" if item.origin else "unknown",
                           "claim_origin": None,  # Publication/wire attribution is not an original actor claim.
                           "model_processing": source["rights"]["model_processing"]}
                self.append("story_revision", payload, available_at=at, record_id=rid, db=db)
                self.set_cursor(db, "story:" + story, {"content_hash": content, "revision_id": rid,
                    "revision": payload["revision"], "observation_seq": observation["seq"]})
                output.append(rid)
            self.append("parse_receipt", {"input_revision_ids": [observation_id], "revision_ids": output,
                                          "transform": "source-v1"}, db=db)
            self.set_cursor(db, "source:" + source["id"], cursor)
        return output

    def backup(self, destination: Path) -> str:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError("backup destination already exists")
        with self.connect() as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup integrity check failed")
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        return hashlib.sha256(destination.read_bytes()).hexdigest()


@contextmanager
def component_lock(root: Path, name: str):
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{name}.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"{name} already has an active writer") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
