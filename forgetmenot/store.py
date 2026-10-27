from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet


INTERVALS = (60, 300, 900, 3600, 21600, 86400, 604800, 2592000)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "forgetmenot.sqlite3"
        key_path = self.directory / "master.key"
        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError("master.key is missing. Restore it with the database backup.")
            with key_path.open("xb") as handle:
                os.chmod(key_path, 0o600)
                handle.write(Fernet.generate_key())
        self.key = key_path.read_bytes()
        self.cipher = Fernet(self.key)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS watches (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', interval INTEGER NOT NULL,
                    automatic INTEGER NOT NULL, channels TEXT NOT NULL,
                    created REAL NOT NULL, next_check REAL NOT NULL, last_check REAL,
                    approach TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    claim TEXT, model TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                    checked REAL NOT NULL, outcome TEXT NOT NULL, summary TEXT NOT NULL,
                    evidence TEXT NOT NULL, model TEXT NOT NULL, activity TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS checks_watch ON checks(watch_id, id DESC);
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                    created REAL NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    evidence TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'match', seen INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY, notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL, delivered REAL, error TEXT NOT NULL DEFAULT '',
                    UNIQUE(notification_id, channel)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(watches)")}
            if "model" not in columns:
                db.execute("ALTER TABLE watches ADD COLUMN model TEXT NOT NULL DEFAULT '{}'")
                db.execute("UPDATE watches SET model=?", (json.dumps(self.setting("model", {})),))
            if "activity" not in {row["name"] for row in db.execute("PRAGMA table_info(checks)")}:
                db.execute("ALTER TABLE checks ADD COLUMN activity TEXT NOT NULL DEFAULT '[]'")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, name, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0]).decode()) if row else default

    def save_setting(self, name, value):
        encrypted = self.cipher.encrypt(json.dumps(value).encode()).decode()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (name, encrypted))
            if name == "model" and isinstance(value, dict) and value.get("id"):
                db.execute("UPDATE watches SET model=? WHERE model='{}'", (json.dumps(value),))

    def host_id(self):
        current = self.setting("host_id")
        if not current:
            current = "urn:uuid:" + str(uuid.uuid4())
            self.save_setting("host_id", current)
        return current

    def watches(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM watches ORDER BY created DESC").fetchall()
        return [self.watch_dict(row) for row in rows]

    @staticmethod
    def watch_dict(row):
        result = dict(row)
        result["channels"] = json.loads(result["channels"])
        result["model"] = json.loads(result["model"])
        result["checking"] = bool(result.pop("claim") and result["lease_until"] > time.time())
        result.pop("lease_until")
        return result

    def create(self, goal, interval, channels, model=None):
        watch_id, now = str(uuid.uuid4()), time.time()
        selected = model if model is not None else self.setting("model", {})
        with self.connect() as db:
            db.execute("""INSERT INTO watches
                (id,goal,title,interval,automatic,channels,created,next_check,model)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (watch_id, goal, goal[:90], interval or 86400, interval is None, json.dumps(channels), now, now, json.dumps(selected)))
        return watch_id

    def update(self, watch_id, action):
        with self.connect() as db:
            if action == "delete":
                result = db.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            elif action == "pause":
                result = db.execute("UPDATE watches SET status='paused',revision=revision+1,claim=NULL,lease_until=0 WHERE id=?", (watch_id,))
            else:
                result = db.execute("""UPDATE watches SET status='active',next_check=?,revision=revision+1,
                    claim=NULL,lease_until=0 WHERE id=?""", (time.time(), watch_id))
            return result.rowcount > 0

    def edit(self, watch_id, goal, interval, channels, model=None):
        with self.connect() as db:
            current = db.execute("SELECT model FROM watches WHERE id=?", (watch_id,)).fetchone()
            if not current:
                return False
            selected = json.dumps(model) if model is not None else current["model"]
            result = db.execute("""UPDATE watches SET goal=?,title=?,interval=?,automatic=?,channels=?,
                summary='',approach='',error='',failures=0,last_check=NULL,status='active',next_check=?,
                revision=revision+1,claim=NULL,lease_until=0,model=? WHERE id=?""",
                (goal, goal[:90], interval or 86400, interval is None, json.dumps(channels), time.time(), selected, watch_id))
            return result.rowcount > 0

    def claim(self, now=None, configured_only=False):
        now = now or time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM watches WHERE status='active' AND next_check<=?
                AND lease_until<=? AND (?=0 OR model<>'{}') ORDER BY next_check LIMIT 1""", (now, now, configured_only)).fetchone()
            if not row:
                return None
            token = str(uuid.uuid4())
            db.execute("UPDATE watches SET claim=?,lease_until=? WHERE id=?", (token, now + 600, row["id"]))
            return dict(row, claim=token, model=json.loads(row["model"]))

    def finish(self, watch, result, model, now=None):
        now = now or time.time()
        interval = result["interval"] if watch["automatic"] else watch["interval"]
        inconclusive = result["outcome"] == "inconclusive"
        failures = watch["failures"] + 1 if inconclusive else 0
        delay = max(interval, min(86400, 300 * 2 ** min(failures, 8))) if inconclusive else interval
        with self.connect() as db:
            changed = db.execute("""UPDATE watches SET title=?,approach=?,summary=?,status=?,interval=?,
                last_check=?,next_check=?,error=?,failures=?,claim=NULL,lease_until=0
                WHERE id=? AND revision=? AND claim=? AND status='active'""",
                (result["title"], result["approach"], result["summary"],
                 "matched" if result["outcome"] == "matched" else "active", interval,
                 now, now + delay, result["summary"] if inconclusive else "", failures,
                 watch["id"], watch["revision"], watch["claim"]))
            if not changed.rowcount:
                return False
            db.execute("INSERT INTO checks(watch_id,checked,outcome,summary,evidence,model,activity) VALUES (?,?,?,?,?,?,?)",
                       (watch["id"], now, result["outcome"], result["summary"], json.dumps(result["evidence"]), model, json.dumps(result.get("activity", []))))
            if result["outcome"] == "matched" or failures == 3:
                attention = result["outcome"] != "matched"
                title = ("Needs attention: " if attention else "") + result["title"]
                body = ("Three consecutive checks were inconclusive. Please review this watch. " if attention else "") + result["summary"]
                notification = db.execute("INSERT INTO notifications(watch_id,created,title,body,evidence,kind) VALUES (?,?,?,?,?,?)",
                                          (watch["id"], now, title, body, json.dumps(result["evidence"]), "attention" if attention else "match")).lastrowid
                for channel in json.loads(watch["channels"]):
                    db.execute("INSERT INTO deliveries(notification_id,channel,next_attempt) VALUES (?,?,?)", (notification, channel, now))
            return True

    def history(self, watch_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM checks WHERE watch_id=? ORDER BY id DESC LIMIT 100", (watch_id,)).fetchall()
        return [dict(row, evidence=json.loads(row["evidence"]), activity=json.loads(row["activity"])) for row in rows]

    def inbox(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 100").fetchall()
            deliveries = [dict(row) for row in db.execute("SELECT id,notification_id,channel,attempts,delivered,error FROM deliveries ORDER BY id DESC LIMIT 300")]
        return {"notifications": [dict(row, evidence=json.loads(row["evidence"])) for row in rows], "deliveries": deliveries}

    def pending_delivery(self):
        with self.connect() as db:
            row = db.execute("""SELECT d.id AS delivery_id,d.channel,d.attempts,n.* FROM deliveries d
                JOIN notifications n ON n.id=d.notification_id WHERE d.delivered IS NULL AND d.next_attempt<=?
                ORDER BY d.next_attempt LIMIT 1""", (time.time(),)).fetchone()
        return dict(row) if row else None

    def delivered(self, delivery, error=""):
        attempts = delivery["attempts"] + 1
        with self.connect() as db:
            db.execute("UPDATE deliveries SET attempts=?,next_attempt=?,delivered=?,error=? WHERE id=?",
                       (attempts, time.time() + min(86400, 60 * 2 ** min(attempts, 10)),
                        None if error else time.time(), error, delivery["delivery_id"]))
