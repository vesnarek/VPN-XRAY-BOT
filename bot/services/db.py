import sqlite3
import time
import contextlib
from pathlib import Path
from typing import Optional, Dict, Any
import os

REF_BONUS_INVITER_CENTS = int(os.getenv("REF_BONUS_INVITER_CENTS", "3000"))
REF_BONUS_FRIEND_CENTS  = int(os.getenv("REF_BONUS_FRIEND_CENTS",  "2000"))
MAX_DEVICES_PER_USER = int(os.getenv("MAX_DEVICES_PER_USER", "3"))

DB_PATH = Path(__file__).resolve().parent.parent / "bot.db"

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users(
  tg_id          INTEGER PRIMARY KEY,
  created_at     INTEGER NOT NULL,
  balance_cents  INTEGER NOT NULL DEFAULT 0,
  referrer       INTEGER,
  got_welcome    INTEGER NOT NULL DEFAULT 0,
  username       TEXT
);

CREATE TABLE IF NOT EXISTS devices(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id          INTEGER NOT NULL,
  uuid           TEXT NOT NULL,
  name           TEXT NOT NULL,
  os             TEXT,
  status         TEXT NOT NULL,            -- pending|active|paused|deleted
  created_at     INTEGER NOT NULL,
  activated_at   INTEGER,                  -- epoch, когда активировалось
  last_billed    INTEGER,                  -- последняя дата суточного списания (целые сутки)
  expires_at     TEXT,                     -- ISO, если знаем со стороны сервера
  sub_id         TEXT,                     -- добавляем для подписи/рефрешей
  FOREIGN KEY(tg_id) REFERENCES users(tg_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_devices_uuid ON devices(uuid);
CREATE INDEX IF NOT EXISTS ix_devices_tg       ON devices(tg_id);
CREATE INDEX IF NOT EXISTS ix_devices_status   ON devices(status);
CREATE INDEX IF NOT EXISTS ix_devices_sub_id   ON devices(sub_id);

CREATE TABLE IF NOT EXISTS payments(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id          INTEGER NOT NULL,
  amount_cents   INTEGER NOT NULL,
  method         TEXT NOT NULL,            -- card|promo|referral
  ref            TEXT,
  created_at     INTEGER NOT NULL,
  FOREIGN KEY(tg_id) REFERENCES users(tg_id)
);
CREATE INDEX IF NOT EXISTS ix_payments_tg      ON payments(tg_id);
CREATE INDEX IF NOT EXISTS ix_payments_created ON payments(created_at);

CREATE TABLE IF NOT EXISTS promos (
  code           TEXT PRIMARY KEY,
  amount_cents   INTEGER NOT NULL,
  uses_left      INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id          INTEGER,
  type           TEXT NOT NULL,
  payload        TEXT,
  created_at     INTEGER NOT NULL
);
"""

@contextlib.contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON;")
        con.execute("PRAGMA busy_timeout=5000;")
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        yield con
        con.commit()
    finally:
        con.close()

def init():
    with db() as con:
        con.executescript(DDL)
    migrate()

def migrate():
    with db() as con:
        def _try(sql: str):
            try:
                con.execute(sql)
            except Exception:
                pass


        _try("ALTER TABLE users ADD COLUMN username TEXT")
        _try("ALTER TABLE devices ADD COLUMN expires_at TEXT")
        _try("ALTER TABLE devices ADD COLUMN sub_id TEXT")

        _try("CREATE INDEX IF NOT EXISTS ix_devices_tg ON devices(tg_id)")
        _try("CREATE INDEX IF NOT EXISTS ix_devices_status ON devices(status)")
        _try("CREATE INDEX IF NOT EXISTS ix_payments_tg ON payments(tg_id)")
        _try("CREATE INDEX IF NOT EXISTS ix_payments_created ON payments(created_at)")
        _try("CREATE INDEX IF NOT EXISTS ix_devices_sub_id ON devices(sub_id)")
        _try("CREATE INDEX IF NOT EXISTS ix_devices_last_billed   ON devices(last_billed)")
        _try("CREATE INDEX IF NOT EXISTS ix_devices_activated_at ON devices(activated_at)")
        _try("ALTER TABLE devices ADD COLUMN server_base TEXT")
        _try("CREATE INDEX IF NOT EXISTS ix_devices_server_base ON devices(server_base)")
        _try("CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_card_ref ON payments(ref) WHERE method='card'")
        _try("CREATE INDEX IF NOT EXISTS ix_payments_method_id ON payments(method, id)")

        _try("CREATE INDEX IF NOT EXISTS ix_payments_referral_ref ON payments(ref) WHERE method='referral'")


        _try(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_promo_unique "
            "ON payments(tg_id, method, ref) "
            "WHERE method='promo'"
        )



def now() -> int:
    return int(time.time())



def ensure_user(tg_id: int, username: Optional[str] = None) -> Dict[str, Any]:
    with db() as con:
        r = con.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not r:
            con.execute(
                "INSERT INTO users(tg_id, created_at, balance_cents, username) VALUES(?,?,?,?)",
                (tg_id, now(), 0, (username or None))
            )
            r = con.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        else:
            if username:
                cur = (r["username"] or "")
                if cur != username:
                    con.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
                    r = con.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(r)

def update_username(tg_id: int, username: Optional[str]):
    if not username:
        return
    with db() as con:
        con.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))

def set_referrer(tg_id: int, referrer: int):
    if not referrer or referrer == tg_id:
        return
    with db() as con:
        con.execute(
            "UPDATE users SET referrer=? WHERE tg_id=? AND (referrer IS NULL OR referrer=0)",
            (referrer, tg_id)
        )

def set_welcome_given(tg_id: int):
    with db() as con:
        con.execute("UPDATE users SET got_welcome=1 WHERE tg_id=?", (tg_id,))

def got_welcome(tg_id: int) -> bool:
    with db() as con:
        r = con.execute("SELECT got_welcome FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return bool(r and r[0])

def get_balance_cents(tg_id: int) -> int:
    with db() as con:
        r = con.execute("SELECT balance_cents FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return int(r[0]) if r else 0

def add_balance(tg_id: int, amount_cents: int, method: str, ref: Optional[str] = None):
    with db() as con:
        add_balance_con(con, tg_id, amount_cents, method, ref)

def add_balance_con(con: sqlite3.Connection, tg_id: int, amount_cents: int, method: str, ref: Optional[str] = None):
    con.execute(
        "UPDATE users SET balance_cents = balance_cents + ? WHERE tg_id=?",
        (int(amount_cents), tg_id)
    )
    con.execute(
        "INSERT INTO payments(tg_id, amount_cents, method, ref, created_at) VALUES(?,?,?,?,?)",
        (tg_id, int(amount_cents), method, ref, now())
    )

def burn_balance(tg_id: int, cents: int) -> bool:
    cents = int(cents)
    with db() as con:
        r = con.execute("SELECT balance_cents FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not r:
            return False
        cur = int(r[0])
        if cur < cents:
            return False
        con.execute("UPDATE users SET balance_cents=? WHERE tg_id=?", (cur - cents, tg_id))
        return True


def list_devices(tg_id: int) -> list[Dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM devices WHERE tg_id=? AND status!='deleted' ORDER BY id",
            (tg_id,)
        ).fetchall()
        return [dict(x) for x in rows]

def add_device(
    tg_id: int,
    uuid: str,
    name: str,
    os: Optional[str],
    status: str,
    expires_at: Optional[str] = None,
    sub_id: Optional[str] = None,
    server_base: Optional[str] = None
):
    with db() as con:

        row = con.execute(
            "SELECT COUNT(*) FROM devices WHERE tg_id=? AND status!='deleted'",
            (tg_id,)
        ).fetchone()
        if int(row[0] or 0) >= MAX_DEVICES_PER_USER:
            raise ValueError("device_limit_reached")

        con.execute(
            """INSERT OR IGNORE INTO devices(tg_id, uuid, name, os, status, created_at, expires_at, sub_id, server_base)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (tg_id, uuid, name, os, status, now(), expires_at, sub_id, server_base)
        )
        if server_base:
            con.execute("UPDATE devices SET server_base=? WHERE uuid=? AND (server_base IS NULL OR server_base='')",
                        (server_base, uuid))


def set_device_status(uuid: str, status: str):
    with db() as con:
        con.execute("UPDATE devices SET status=? WHERE uuid=?", (status, uuid))

def set_device_expires(uuid: str, expires_at: Optional[str]):
    with db() as con:
        con.execute("UPDATE devices SET expires_at=? WHERE uuid=?", (expires_at, uuid))

def set_device_sub_id(uuid: str, sub_id: str):
    with db() as con:
        con.execute("UPDATE devices SET sub_id=? WHERE uuid=?", (sub_id, uuid))

def set_device_activated(uuid: str, ts: Optional[int] = None):
    with db() as con:
        t = ts or now()
        con.execute(
            "UPDATE devices SET status='active', activated_at=?, last_billed=? "
            "WHERE uuid=? AND (activated_at IS NULL OR activated_at=0)",
            (t, t, uuid)
        )
def set_device_server_base(uuid: str, server_base: Optional[str]):
    with db() as con:
        con.execute("UPDATE devices SET server_base=? WHERE uuid=?", (server_base, uuid))

def device_by_uuid(uuid: str) -> Optional[Dict[str, Any]]:
    with db() as con:
        r = con.execute("SELECT * FROM devices WHERE uuid=?", (uuid,)).fetchone()
        return dict(r) if r else None

def device_by_id(device_id: int) -> Optional[Dict[str, Any]]:
    with db() as con:
        r = con.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        return dict(r) if r else None

def mark_billed(uuid: str, ts_day_start: int):
    with db() as con:
        con.execute("UPDATE devices SET last_billed=? WHERE uuid=?", (int(ts_day_start), uuid))

def nearest_expiry_for_user(tg_id: int) -> Optional[str]:
    with db() as con:
        r = con.execute(
            "SELECT MIN(expires_at) FROM devices "
            "WHERE tg_id=? AND status='active' AND expires_at IS NOT NULL",
            (tg_id,)
        ).fetchone()
    return r[0] if r and r[0] else None


def _referral_paid_already_con(con: sqlite3.Connection, referred_tg_id: int) -> bool:
    marker = f"user:{int(referred_tg_id)}"
    r = con.execute(
        "SELECT 1 FROM payments WHERE method='referral' AND ref=? LIMIT 1",
        (marker,)
    ).fetchone()
    return bool(r)

def maybe_grant_referral_bonus_for_user(tg_id: int, bonus_cents: int = 2000) -> bool:
    with db() as con:

        r = con.execute(
            "SELECT COUNT(*) FROM devices WHERE tg_id=? AND activated_at IS NOT NULL",
            (tg_id,)
        ).fetchone()
        ever_activated = int(r[0] or 0) > 0

        if not ever_activated:

            return False


        if _referral_paid_already_con(con, tg_id):
            return False


        u = con.execute("SELECT referrer FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        referrer = int(u["referrer"]) if (u and u["referrer"]) else 0
        if not referrer or referrer == tg_id:
            return False

        marker = f"user:{int(tg_id)}"

        add_balance_con(con, tg_id, REF_BONUS_FRIEND_CENTS, "referral", marker)
        add_balance_con(con, referrer, REF_BONUS_INVITER_CENTS, "referral", marker)

        return True

def activate_device_and_maybe_referral(uuid: str, bonus_cents: int = 2000) -> Dict[str, Any]:
    with db() as con:
        dev = con.execute("SELECT * FROM devices WHERE uuid=?", (uuid,)).fetchone()
        if not dev:
            return {"ok": False, "error": "device_not_found"}

        if dev["activated_at"]:

            return {"ok": True, "already": True, "granted": False}

        t = now()
        con.execute(
            "UPDATE devices SET status='active', activated_at=?, last_billed=? "
            "WHERE uuid=? AND (activated_at IS NULL OR activated_at=0)",
            (t, t, uuid)
        )

        tg_id = int(dev["tg_id"])

        r = con.execute(
            "SELECT COUNT(*) FROM devices WHERE tg_id=? AND activated_at IS NOT NULL",
            (tg_id,)
        ).fetchone()
        count_after = int(r[0] or 0)

        granted = False
        if count_after == 1:
            if not _referral_paid_already_con(con, tg_id):
                u = con.execute("SELECT referrer FROM users WHERE tg_id=?", (tg_id,)).fetchone()
                referrer = int(u["referrer"]) if (u and u["referrer"]) else 0
                if referrer and referrer != tg_id:
                    marker = f"user:{int(tg_id)}"
                    add_balance_con(con, tg_id, REF_BONUS_FRIEND_CENTS, "referral", marker)
                    add_balance_con(con, referrer, REF_BONUS_INVITER_CENTS, "referral", marker)
                    granted = True

        return {"ok": True, "already": False, "granted": granted}


def create_promo(code: str, amount_cents: int, uses_left: int = 1):
    with db() as con:
        con.execute(
            "INSERT INTO promos(code, amount_cents, uses_left) VALUES(?,?,?)",
            (code, int(amount_cents), int(uses_left))
        )

def fetch_promo(code: str) -> Optional[Dict[str, Any]]:
    with db() as con:
        r = con.execute(
            "SELECT code, amount_cents, uses_left FROM promos WHERE code=?",
            (code,)
        ).fetchone()
        return dict(r) if r else None

def decrement_promo_use(code: str) -> bool:
    with db() as con:
        r = con.execute("SELECT uses_left FROM promos WHERE code=? AND uses_left>0", (code,)).fetchone()
        if not r:
            return False
        con.execute("UPDATE promos SET uses_left = uses_left - 1 WHERE code=?", (code,))
        return True


def counts_summary() -> Dict[str, int]:
    with db() as con:
        u = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 0
        d = con.execute("SELECT COUNT(*) FROM devices WHERE status!='deleted'").fetchone()[0] or 0
        bal = con.execute("SELECT COALESCE(SUM(balance_cents),0) FROM users").fetchone()[0] or 0
    return {"users": u, "devices": d, "balance_total_cents": bal}

def users_page(offset: int, limit: int = 20) -> list[Dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            """
          SELECT
            u.tg_id,
            COALESCE(u.username, '') AS username,
            u.balance_cents,
            (SELECT COUNT(*) FROM devices d WHERE d.tg_id=u.tg_id AND d.status!='deleted') AS devs,
            (SELECT MIN(expires_at) FROM devices d WHERE d.tg_id=u.tg_id AND d.status='active' AND d.expires_at IS NOT NULL) AS nearest_exp
          FROM users u
          ORDER BY u.created_at DESC
          LIMIT ? OFFSET ?
        """,
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]

def payments_sum_since(ts_from: int, method: Optional[str] = None) -> int:
    with db() as con:
        if method:
            r = con.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE created_at>=? AND method=?",
                (int(ts_from), method)
            ).fetchone()
        else:
            r = con.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE created_at>=?",
                (int(ts_from),)
            ).fetchone()
    return int(r[0] or 0)

def users_count_low_balance(threshold_cents: int = 1000) -> int:
    with db() as con:
        r = con.execute(
            "SELECT COUNT(*) FROM users WHERE balance_cents < ?",
            (int(threshold_cents),)
        ).fetchone()
        return int(r[0] or 0)


def log_event(tg_id: int, etype: str, payload: str):
    with db() as con:
        con.execute(
            "INSERT INTO events(tg_id, type, payload, created_at) VALUES(?,?,?,?)",
            (tg_id, etype, payload, now())
        )

def log_event_con(con: sqlite3.Connection, tg_id: int, etype: str, payload: str):
    con.execute(
        "INSERT INTO events(tg_id, type, payload, created_at) VALUES(?,?,?,?)",
        (tg_id, etype, payload, now())
    )

def find_event_by_payment(payment_id: str):
    with db() as con:
        r = con.execute(
            "SELECT tg_id, type, payload, created_at FROM events WHERE type='yk:create' AND payload LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%"{payment_id}"%',)
        ).fetchone()
        return dict(r) if r else None

def card_payment_exists(ref: str) -> bool:
    with db() as con:
        r = con.execute(
            "SELECT 1 FROM payments WHERE method='card' AND ref=? LIMIT 1",
            (ref,)
        ).fetchone()
        return bool(r)

def add_card_payment_if_new(tg_id: int, amount_cents: int, ref: str) -> bool:
    import sqlite3 as _sqlite3
    with db() as con:
        try:
            add_balance_con(con, tg_id, amount_cents, "card", ref)
            return True
        except _sqlite3.IntegrityError:
            return False


