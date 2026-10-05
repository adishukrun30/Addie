"""Encrypted SQLite (SQLCipher) storage.

The database key is derived from the master password with Argon2id and is
never written to disk. Only the random salt is stored next to the database.
"""
import os
import threading
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from sqlcipher3 import dbapi2 as sqlcipher

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('CHECKING','CARD','SAVINGS','SECURITY','LOAN')),
    institution TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    external_id TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL CHECK (kind IN ('expense','income','savings','transfer')),
    color TEXT NOT NULL DEFAULT '#888888'
);
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,                 -- YYYY-MM-DD
    description TEXT NOT NULL,
    amount      INTEGER NOT NULL,              -- agorot; negative = money out
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    note        TEXT,
    source      TEXT NOT NULL DEFAULT 'import',
    dedup_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category_id);
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY,
    pattern     TEXT NOT NULL UNIQUE,          -- case-insensitive substring
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS budgets (
    category_id    INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
    monthly_amount INTEGER NOT NULL CHECK (monthly_amount >= 0)   -- agorot
);
CREATE TABLE IF NOT EXISTS savings_goals (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    target_amount        INTEGER NOT NULL CHECK (target_amount > 0),
    current_amount       INTEGER NOT NULL DEFAULT 0,
    monthly_contribution INTEGER NOT NULL DEFAULT 0,
    target_date          TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_CATEGORIES = [
    ("משכורת", "income", "#2e7d32"),
    ("הכנסה אחרת", "income", "#66bb6a"),
    ("מזון וסופר", "expense", "#ef6c00"),
    ("אוכל בחוץ", "expense", "#f4511e"),
    ("דיור ושכירות", "expense", "#6d4c41"),
    ("חשבונות", "expense", "#5c6bc0"),
    ("תחבורה ודלק", "expense", "#00838f"),
    ("בריאות ופארם", "expense", "#d81b60"),
    ("מנויים", "expense", "#8e24aa"),
    ("קניות", "expense", "#c0ca33"),
    ("בילויים ופנאי", "expense", "#fb8c00"),
    ("ביטוחים", "expense", "#546e7a"),
    ("עמלות בנק", "expense", "#757575"),
    ("שונות", "expense", "#9e9e9e"),
    ("חיסכון והשקעות", "savings", "#1565c0"),
    ("העברה בין חשבונות", "transfer", "#bdbdbd"),
]

DEFAULT_RULES = {
    "משכורת": ["משכורת", "שכר"],
    "מזון וסופר": ["שופרסל", "רמי לוי", "ויקטורי", "יוחננוף", "אושר עד", "מגה", "טיב טעם", "יינות ביתן", "חצי חינם"],
    "אוכל בחוץ": ["וולט", "wolt", "תן ביס", "10bis", "מקדונלדס", "ארומה", "קפה"],
    "חשבונות": ["חברת החשמל", "חשמל", "מקורות", "ארנונה", "תאגיד מים", "בזק", "פרטנר", "סלקום", "הוט"],
    "תחבורה ודלק": ["פז", "סונול", "דלק", "דור אלון", "רב קו", "רב-קו", "gett", "מוביט", "חניון", "פנגו"],
    "בריאות ופארם": ["סופר-פארם", "סופר פארם", "כללית", "מכבי", "מאוחדת", "לאומית", "בית מרקחת"],
    "מנויים": ["netflix", "spotify", "apple.com", "google", "disney", "amazon prime", "youtube"],
    "קניות": ["amazon", "aliexpress", "איקאה", "ikea", "shein", "zara", "h&m"],
    "ביטוחים": ["ביטוח", "הראל", "מגדל", "הפניקס", "כלל ביטוח", "מנורה"],
    "עמלות בנק": ["עמלה", "דמי ניהול חשבון"],
    "חיסכון והשקעות": ["קופת גמל", "קרן השתלמות", "פיקדון", "הפקדה לחיסכון", "ני\"ע", "ניירות ערך"],
}


class WrongPasswordError(Exception):
    pass


def derive_key(password: str, salt: bytes) -> bytes:
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=3,
        memory_cost=64 * 1024,  # 64 MiB
        parallelism=2,
        hash_len=32,
        type=Type.ID,
    )


class Database:
    """A single SQLCipher connection guarded by a lock (one local user)."""

    def __init__(self, db_path: Path, salt_path: Path):
        self.db_path = Path(db_path)
        self.salt_path = Path(salt_path)
        self._conn = None
        self._lock = threading.RLock()

    @property
    def initialized(self) -> bool:
        return self.db_path.exists() and self.salt_path.exists()

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _connect(self, key: bytes):
        conn = sqlcipher.connect(str(self.db_path), check_same_thread=False)
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlcipher.DatabaseError as exc:
            conn.close()
            raise WrongPasswordError() from exc
        conn.row_factory = sqlcipher.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA secure_delete = ON")
        return conn

    def create(self, password: str) -> None:
        if self.initialized:
            raise RuntimeError("database already exists")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        salt = os.urandom(16)
        self.salt_path.write_bytes(salt)
        conn = self._connect(derive_key(password, salt))
        with conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            for name, kind, color in DEFAULT_CATEGORIES:
                conn.execute("INSERT INTO categories(name, kind, color) VALUES (?, ?, ?)", (name, kind, color))
            for cat_name, patterns in DEFAULT_RULES.items():
                cat_id = conn.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)).fetchone()[0]
                for pattern in patterns:
                    conn.execute("INSERT OR IGNORE INTO rules(pattern, category_id) VALUES (?, ?)", (pattern.lower(), cat_id))
        for path in (self.db_path, self.salt_path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        self._conn = conn

    def open(self, password: str) -> None:
        if not self.initialized:
            raise RuntimeError("database not initialized")
        salt = self.salt_path.read_bytes()
        conn = self._connect(derive_key(password, salt))
        with conn:
            conn.executescript(SCHEMA)
        with self._lock:
            self.close()
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            self._require_open()
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params=()) -> int:
        """Run a write statement in its own transaction; returns lastrowid."""
        with self._lock:
            self._require_open()
            with self._conn:
                cur = self._conn.execute(sql, params)
                return cur.lastrowid

    def transaction(self):
        """Context manager for multi-statement writes: `with db.transaction() as conn:`."""
        db = self

        class _Tx:
            def __enter__(self_inner):
                db._lock.acquire()
                db._require_open()
                return db._conn.__enter__() or db._conn

            def __exit__(self_inner, *exc):
                try:
                    return db._conn.__exit__(*exc)
                finally:
                    db._lock.release()

        return _Tx()

    def _require_open(self):
        if self._conn is None:
            raise RuntimeError("database is locked")
