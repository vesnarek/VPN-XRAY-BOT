import base64
import datetime
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from typing import Optional, Tuple, Dict, List, Any
import threading

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from urllib.parse import quote



CONF            = os.environ.get("XRAY_CONF", "/usr/local/etc/xray/config.json")
XRAY_API_ADDR   = os.environ.get("XRAY_API_ADDR", "127.0.0.1:10085")
XRAY_BIN        = os.environ.get("XRAY_BIN", "xray")

DOMAIN          = os.environ.get("XRAY_DOMAIN", "zhuukh.ru")
DB              = os.environ.get("XRAY_DB", "/var/lib/xraymgr/users.db")
REALITY_TAG     = os.environ.get("XRAY_REALITY_TAG", "vless-in")

SUB_PORT        = int(os.environ.get("XRAY_SUB_PORT", "8443"))
REALITY_PORT    = int(os.environ.get("XRAY_REALITY_PORT", "443"))

ACCESS_LOG      = os.environ.get("XRAY_ACCESS_LOG", "/var/log/xray/access.log")
SESSIONS_WINDOW_SEC = int(os.environ.get("SESSIONS_WINDOW_SEC", "45"))

BOT_NOTIFY_URL  = os.environ.get("BOT_NOTIFY_URL", "http://127.0.0.1:8081/notify")

_PBK_ENV        = (os.environ.get("XRAY_REALITY_PBK") or "").strip() or None

_VIOL_TICKS: dict[str, int] = {}
_LAST_KICK_TS: dict[str, float] = {}
XRAY_SLOT_FILE   = "/var/lib/xraymgr/active_slot"
XRAY_CFG_A       = "/etc/xray/config-a.json"
XRAY_CFG_B       = "/etc/xray/config-b.json"
XRAY_PORT_A      = 10000
XRAY_PORT_B      = 10001
XRAY_API_PORT_A  = 10085
XRAY_API_PORT_B  = 10086
ACCESS_LOGS      = ["/var/log/xray/access-a.log", "/var/log/xray/access-b.log"]


SUB_PREFIX      = os.environ.get("XRAY_SUB_PREFIX", "api").strip("/")


def _db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with _db() as conn:

        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_id      TEXT UNIQUE,
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            status      TEXT NOT NULL,
            uuid        TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS devices(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id       TEXT,
            uuid        TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            os          TEXT,
            status      TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
        """)


        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "upload_bytes" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN upload_bytes INTEGER NOT NULL DEFAULT 0")
        if "download_bytes" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN download_bytes INTEGER NOT NULL DEFAULT 0")
        if "total_quota_bytes" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN total_quota_bytes INTEGER NOT NULL DEFAULT 0")

        if "first_traffic_notified" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN first_traffic_notified INTEGER NOT NULL DEFAULT 0")


        conn.execute("CREATE INDEX IF NOT EXISTS ix_users_uuid ON users(uuid)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_users_sub_id ON users(sub_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_users_status ON users(status)")


        conn.execute("""
        CREATE TABLE IF NOT EXISTS traffic_cursor(
            uuid        TEXT PRIMARY KEY,
            last_up     INTEGER NOT NULL DEFAULT 0,
            last_down   INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL
        )
        """)

        conn.commit()



def _get_active_slot() -> str:
    try:
        with open(XRAY_SLOT_FILE, "r") as f:
            s = (f.read() or "A").strip().upper()
            return "A" if s == "A" else "B"
    except Exception:
        return "A"

def _inactive(slot: str) -> str:
    return "B" if slot == "A" else "A"

def _clients_from_db() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT uuid FROM users WHERE status!='deleted'").fetchall()
    return [{
        "id": r["uuid"],
        "flow": "xtls-rprx-vision",
        "email": r["uuid"],
        "level": 0,
    } for r in rows]


def _build_cfg_for_slot(slot: str) -> dict:

    base = _load_cfg()

    if slot == "A":
        access, error, vless_port, api_port = "/var/log/xray/access-a.log", "/var/log/xray/error-a.log", XRAY_PORT_A, XRAY_API_PORT_A
    else:
        access, error, vless_port, api_port = "/var/log/xray/access-b.log", "/var/log/xray/error-b.log", XRAY_PORT_B, XRAY_API_PORT_B

    base.setdefault("log", {})
    base["log"]["access"] = access
    base["log"]["error"]  = error


    for ib in base.get("inbounds", []):
        if ib.get("tag") == "api-in":
            ib["listen"] = "127.0.0.1"
            ib["port"]   = api_port


    ib = _find_reality_inbound(base)
    ib["listen"] = "127.0.0.1"
    ib["port"]   = vless_port
    ib.setdefault("settings", {})["clients"] = _clients_from_db()


    base.setdefault("routing", {}).setdefault("rules", [])
    has_api_rule = any(
        r.get("type") == "field" and "api-in" in (r.get("inboundTag") or [])
        for r in base["routing"]["rules"]
    )
    if not has_api_rule:
        base["routing"]["rules"].append({
            "type": "field",
            "inboundTag": ["api-in"],
            "outboundTag": "api"
        })


    outs = base.setdefault("outbounds", [])
    if not any(o.get("tag") == "api" for o in outs):
        outs.append({"tag": "api", "protocol": "freedom"})

    return base

def _save_cfg_for_slot(slot: str, cfg: dict):
    path = XRAY_CFG_A if slot == "A" else XRAY_CFG_B
    with open(path, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    subprocess.run([XRAY_BIN, "-test", "-config", path], check=True)

def _hc(port: int, timeout=1.0) -> bool:
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False

def switch_live_without_downtime():
    active = _get_active_slot()
    idle   = _inactive(active)
    idle_port = XRAY_PORT_A if idle == "A" else XRAY_PORT_B
    svc_idle  = "xray-a" if idle == "A" else "xray-b"

    cfg = _build_cfg_for_slot(idle)
    _save_cfg_for_slot(idle, cfg)

    subprocess.run(["systemctl", "restart", svc_idle], check=True)

    for _ in range(30):
        if _hc(idle_port):
            break
        time.sleep(0.2)

    subprocess.run(["/usr/local/bin/xray-promote", idle], check=True)


def _load_cfg() -> dict:
    with open(CONF, "r") as f:
        return json.load(f)

def _save_cfg(cfg: dict):

    with open(CONF, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    subprocess.run([XRAY_BIN, "-test", "-config", CONF], check=True)
    subprocess.run(["systemctl", "restart", "xray"], check=True)
    print("[XRAY] restarted after config change")

def _find_reality_inbound(cfg: dict) -> dict:
    for ib in cfg.get("inbounds", []):
        if ib.get("tag") == REALITY_TAG:
            return ib
    raise RuntimeError(f"Reality inbound '{REALITY_TAG}' not found in config")

def _get_reality_settings(ib: dict) -> dict:
    rs = ib.get("streamSettings", {}).get("realitySettings", {})
    if not rs:
        rs = ib.get("settings", {}).get("realitySettings", {})
    return rs or {}

def _bytes_sid_to_hex(val: Any) -> Optional[str]:
    try:
        if isinstance(val, str):
            return val
        if isinstance(val, list) and val and isinstance(val[0], int):
            return "".join(f"{b:02x}" for b in val)
    except Exception:
        pass
    return None

def _reality_params(cfg: dict) -> Tuple[str, str]:
    ib = _find_reality_inbound(cfg)
    rs = _get_reality_settings(ib)

    sni = None
    sid = None

    sns = rs.get("serverNames") or []
    if sns:
        sni = sns[0]

    sids = rs.get("shortIds") or []
    if sids:
        sid = _bytes_sid_to_hex(sids[0]) if isinstance(sids[0], list) else sids[0]

    if not sni:
        sni = os.environ.get("XRAY_REALITY_SNI")
    if not sid:
        sid = os.environ.get("XRAY_REALITY_SID")

    if not (sni and sid):
        raise RuntimeError("Missing Reality params (sni or sid). Check config.json or ENV")

    return (sni, sid)

def _get_reality_public_key() -> Optional[str]:
    cfg = _load_cfg()
    ib = _find_reality_inbound(cfg)
    rs = _get_reality_settings(ib)
    return rs.get("publicKey")

def _ensure_clients_list(ib: dict) -> list:
    ib.setdefault("settings", {}).setdefault("clients", [])
    return ib["settings"]["clients"]

def _cid(c: dict) -> Optional[str]:

    if not isinstance(c, dict):
        return None
    return c.get("id") or c.get("email") or (isinstance(c.get("account"), dict) and c["account"].get("id"))

def _add_client_to_cfg(user_uuid: str):
    cfg = _load_cfg()
    ib = _find_reality_inbound(cfg)
    clients = _ensure_clients_list(ib)
    if not any(_cid(c) == user_uuid for c in clients):
        clients.append({
            "id": user_uuid,
            "flow": "xtls-rprx-vision",
            "email": user_uuid,
            "level": 0
        })
        _save_cfg(cfg)
    else:
        print(f"[XRAY] client {user_uuid} already present in config")

def _remove_client_from_cfg(user_uuid: str) -> bool:
    cfg = _load_cfg()
    ib = _find_reality_inbound(cfg)
    clients = _ensure_clients_list(ib)
    before = len(clients)
    ib["settings"]["clients"] = [c for c in clients if _cid(c) != user_uuid]
    if len(ib["settings"]["clients"]) != before:
        _save_cfg(cfg)
        return True
    print(f"[XRAY] client {user_uuid} not found in config")
    return False

def _replace_client_in_cfg(old_uuid: str, new_uuid: str):

    cfg = _load_cfg()
    ib = _find_reality_inbound(cfg)
    clients = _ensure_clients_list(ib)


    clients = [c for c in clients if _cid(c) != old_uuid]


    if not any(_cid(c) == new_uuid for c in clients):
        clients.append({
            "id": new_uuid,
            "flow": "xtls-rprx-vision",
            "email": new_uuid,
            "level": 0
        })

    ib["settings"]["clients"] = clients
    _save_cfg(cfg)



def _api_add_user(uuid_str: str, email: Optional[str] = None, inbound_tag: str = REALITY_TAG) -> bool:
    print("[XRAY] runtime hot-add unsupported on this binary; using hard restart after config write")
    return False

def _api_remove_user(email_or_uuid: str, inbound_tag: str = REALITY_TAG) -> bool:
    print("[XRAY] runtime hot-remove unsupported on this binary; using hard restart after config write")
    return False



def _reality_link(uuid_str: str, name: str) -> str:
    cfg = _load_cfg()
    sni, sid = _reality_params(cfg)
    pbk = _get_reality_public_key() or _PBK_ENV
    if not (sni and sid and pbk):
        raise RuntimeError("Missing Reality params.")
    frag = quote(name, safe="")
    return (f"vless://{uuid_str}@{DOMAIN}:{REALITY_PORT}"
            f"?encryption=none&security=reality&sni={sni}&fp=chrome"
            f"&pbk={pbk}&sid={sid}&type=tcp&flow=xtls-rprx-vision#{frag}")

def _notify_bot(sub_id: str, old_uuid: str, new_uuid: str, reason: str):
    try:
        payload = {"sub_id": sub_id, "old_uuid": old_uuid, "new_uuid": new_uuid, "reason": reason}
        requests.post(BOT_NOTIFY_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"notify bot failed: {e}")

def _sub_link(sub_id: str, b64: int = 1) -> str:
    base = f"https://{DOMAIN}:{SUB_PORT}"
    path = f"/{SUB_PREFIX}/sub" if SUB_PREFIX else "/sub"
    return f"{base}{path}/{sub_id}?b64={int(bool(b64))}"


def _notify_first_traffic(sub_id: str, total_bytes: int):
    try:
        url = (BOT_NOTIFY_URL.rstrip("/") + "/first_traffic")
        payload = {"sub_id": sub_id, "bytes": int(total_bytes)}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"notify first_traffic failed: {e}")

app = FastAPI()

class CreateReq(BaseModel):
    name: Optional[str] = None
    days: Optional[int] = None

class RefreshReq(BaseModel):
    id: Optional[str] = None
    sub_id: Optional[str] = None
    uuid: Optional[str] = None   # на всякий

class RotateAnyReq(BaseModel):
    id: Optional[str] = None
    uuid: Optional[str] = None

class RevokeReq(BaseModel):
    id: Optional[str] = None
    sub_id: Optional[str] = None
    uuid: Optional[str] = None

class PauseReq(BaseModel):
    id: Optional[str] = None
    sub_id: Optional[str] = None
    uuid: Optional[str] = None


class ResumeReq(BaseModel):
    id: Optional[str] = None
    sub_id: Optional[str] = None
    uuid: Optional[str] = None
    rotate: Optional[bool] = True

class SetNameReq(BaseModel):
    id: Optional[str] = None
    sub_id: Optional[str] = None
    uuid: Optional[str] = None
    name: str

@app.on_event("startup")
def _startup():
    _init_db()
    threading.Thread(target=_first_traffic_watcher, daemon=True).start()
    threading.Thread(target=_stats_loop, daemon=True).start()

def _stats_loop():
    while True:
        try:
            pull_stats_for_all_users()
        except Exception as e:
            print(f"[stats] pull failed: {e}")
        time.sleep(60)


def _get_user_by_sub_or_uuid(conn: sqlite3.Connection, sub_or_uuid: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
        (sub_or_uuid, sub_or_uuid)
    ).fetchone()

def _update_user_uuid_by_sub(conn: sqlite3.Connection, sub_id: str, new_uuid: str):
    conn.execute("UPDATE users SET uuid=? WHERE sub_id=?", (new_uuid, sub_id))



@app.get("/sub/{sub_or_uuid}")
def get_sub_config(sub_or_uuid: str, b64: int = 1):

    with _db() as conn:
        user = _get_user_by_sub_or_uuid(conn, sub_or_uuid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")


    try:
        _update_user_stats_now(user["uuid"])
        with _db() as conn:
            user = _get_user_by_sub_or_uuid(conn, sub_or_uuid)
    except Exception as e:
        print(f"[sub] failed to refresh stats for {user['uuid']}: {e}")


    display_name = "Нидерланды 🇳🇱"
    link = _reality_link(user["uuid"], display_name)
    body = link + "\n"


    up = int(user["upload_bytes"] or 0)
    down = int(user["download_bytes"] or 0)
    total = int(user["total_quota_bytes"] or 0)

    headers = {
        "Subscription-Userinfo": f"upload={up}; download={down}; total={total}",
        "Profile-Update-Interval": "60",
        "Cache-Control": "no-cache"
    }

    if int(b64):
        enc = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return Response(enc, media_type="text/plain; charset=utf-8", headers=headers)
    else:
        return Response(body, media_type="text/plain; charset=utf-8", headers=headers)




@app.post("/create")
def create(r: CreateReq):
    sub_id    = str(uuid.uuid4())
    user_uuid = str(uuid.uuid4())

    raw_name  = (r.name or "").strip()
    name = "Нидерланды 🇳🇱" if ((not raw_name) or re.fullmatch(r"tg_\\d+", raw_name)) else raw_name

    now = datetime.datetime.utcnow()
    FAR_FUTURE = "2099-12-31T00:00:00Z"

    with _db() as conn:
        conn.execute(
            "INSERT INTO users(sub_id, name, created_at, expires_at, status, uuid) VALUES(?,?,?,?,?,?)",
            (sub_id, name, now.isoformat()+"Z", FAR_FUTURE, "active", user_uuid)
        )
        conn.commit()

    switch_live_without_downtime()

    return {
        "sub_id": sub_id,
        "uuid": user_uuid,
        "name": name,
        "expires_at": FAR_FUTURE,
        "reality": _reality_link(user_uuid, "Нидерланды 🇳🇱"),
        "sub_link": _sub_link(sub_id, b64=1)
    }


@app.post("/refresh")
def refresh(req: RefreshReq):
    ident = (req.id or req.sub_id or req.uuid or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="empty id/sub_id/uuid")

    with _db() as conn:
        row = conn.execute(
            "SELECT sub_id, uuid, name FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
            (ident, ident)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="sub_or_uuid not found")

    sub_id  = row["sub_id"]
    name    = row["name"]
    new_uid = str(uuid.uuid4())

    with _db() as conn:
        conn.execute("UPDATE users SET uuid=? WHERE sub_id=?", (new_uid, sub_id))
        conn.commit()

    switch_live_without_downtime()

    return {
        "ok": True,
        "uuid": new_uid,
        "reality": _reality_link(new_uid, name),
        "sub_link": _sub_link(sub_id, b64=1)
    }


@app.post("/rotate")
def rotate_any(req: RotateAnyReq):
    ident = ((req.id or req.uuid) or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="empty id/uuid")

    with _db() as conn:
        row = conn.execute(
            "SELECT sub_id, uuid, name FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
            (ident, ident)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="sub_or_uuid not found")

    sub_id  = row["sub_id"]
    name    = row["name"]
    new_uid = str(uuid.uuid4())

    with _db() as conn:
        conn.execute("UPDATE users SET uuid=? WHERE sub_id=?", (new_uid, sub_id))
        conn.commit()

    switch_live_without_downtime()

    return {
        "ok": True,
        "uuid": new_uid,
        "reality": _reality_link(new_uid, name),
        "sub_link": _sub_link(sub_id, b64=1)
    }


@app.post("/revoke")
def revoke(req: RevokeReq):
    ident = (req.id or req.sub_id or req.uuid or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="empty id/sub_id/uuid")

    with _db() as conn:
        row = conn.execute(
            "SELECT sub_id, uuid FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
            (ident, ident)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="sub_or_uuid not found")

    sub_id = row["sub_id"]
    uuid_  = row["uuid"]

    with _db() as conn:
        conn.execute("UPDATE users SET status='deleted' WHERE sub_id=?", (sub_id,))
        conn.commit()

    switch_live_without_downtime()

    return {"ok": True, "sub_id": sub_id, "uuid": uuid_}

@app.get("/list")
def list_users():
    with _db() as con:
        rows = con.execute(
            """
            SELECT
                id, sub_id, uuid, name, created_at, expires_at, status,
                upload_bytes, download_bytes,
                (upload_bytes + download_bytes) AS total_bytes
            FROM users
            WHERE status!='deleted'
            ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]




_active_sessions_cache: Dict[str, List[tuple[float, Optional[str]]]] = {}
_log_state: Dict[str, Dict[str, int]] = {}  # path -> {"pos": int, "ino": int}



import datetime as _dt

_TIME_RE = re.compile(r'^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})')
_UUID_RE1 = re.compile(r"id=([0-9a-fA-F\-]{36})")
_UUID_RE2 = re.compile(r"email:\s*([0-9a-fA-F\-]{36})")
_IP_RE    = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")

def _parse_log_ts(line: str, fallback: float) -> float:
    m = _TIME_RE.search(line)
    if not m:
        return fallback
    try:
        dt = _dt.datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
        return dt.timestamp()
    except Exception:
        return fallback

def _tail_access_log_for_snapshot(window_sec: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    win = int(window_sec or SESSIONS_WINDOW_SEC)
    now = time.time()

    for path in [p.strip() for p in ACCESS_LOGS if p.strip()]:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue

        state = _log_state.setdefault(path, {"pos": 0, "ino": st.st_ino})
        if state["ino"] != st.st_ino or state["pos"] > st.st_size:
            state["pos"] = 0
            state["ino"] = st.st_ino

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(state["pos"], 0)
            chunk = f.read()
            state["pos"] = f.tell()

        for line in chunk.splitlines():
            ts  = _parse_log_ts(line, fallback=now)
            if (now - ts) > win:
                continue

            m = _UUID_RE1.search(line) or _UUID_RE2.search(line)
            if not m:
                continue
            uid = m.group(1)

            ip = None
            mi = _IP_RE.search(line)
            if mi:
                ip = mi.group(1)

            recs = _active_sessions_cache.setdefault(uid, [])
            recs.append((ts, ip))

    snapshot: Dict[str, Dict[str, Any]] = {}
    for uid, events in list(_active_sessions_cache.items()):
        fresh = [(t, ip) for (t, ip) in events if (now - t) <= win]
        if not fresh:
            _active_sessions_cache.pop(uid, None)
            continue
        _active_sessions_cache[uid] = fresh

        ips: Dict[str, int] = {}
        for _, ip in fresh:
            if ip:
                ips[ip] = ips.get(ip, 0) + 1

        snapshot[uid] = {
            "count":   len(fresh),
            "ips":     ips,
            "last_ts": max(t for t, _ in fresh),
        }
    return snapshot




def _kick_uuid_by_sub(sub_id: str, old_uuid: str, reason: str = "multi_session") -> Dict[str, Any]:
    new_uuid = str(uuid.uuid4())


    with _db() as conn:
        _update_user_uuid_by_sub(conn, sub_id, new_uuid)
        conn.commit()


    switch_live_without_downtime()

    try:
        _notify_bot(sub_id, old_uuid, new_uuid, reason=reason)
    except Exception:
        pass

    return {"ok": True, "sub_id": sub_id, "old_uuid": old_uuid, "new_uuid": new_uuid}

def _first_traffic_watcher():
    while True:
        try:
            snap = _tail_access_log_for_snapshot(window_sec=300)  # 5 минут окна достаточно
            if not snap:
                time.sleep(3)
                continue

            with _db() as conn:
                for uid in list(snap.keys()):
                    row = conn.execute("SELECT sub_id, first_traffic_notified FROM users WHERE uuid=? LIMIT 1",
                                       (uid,)).fetchone()
                    if not row:
                        continue
                    already = int(row["first_traffic_notified"] or 0) == 1
                    if already:
                        continue

                    # помечаем и шлём хук
                    conn.execute("UPDATE users SET first_traffic_notified=1 WHERE uuid=?", (uid,))
                    conn.commit()
                    try:
                        _notify_first_traffic(row["sub_id"], 1)
                    except Exception as e:
                        print(f"notify first_traffic failed: {e}")
        except Exception as e:
            print(f"[first_traffic] watcher error: {e}")
        time.sleep(3)



from fastapi import Query

@app.get("/sessions")
def sessions(
    kick: bool = False,
    min_sessions: int = Query(2, ge=2, description="Минимум событий за окно"),
    window: Optional[int] = Query(None, ge=5, description="Окно (сек)"),
    limit: int = Query(0, ge=0, description="Максимум киков за вызов (0 = без лимита)"),
    include_ips: bool = Query(False, description="Добавлять карту IP в ответ"),
    distinct_ips_min: int = Query(2, ge=1, description="Минимум разных IP для нарушения"),
):

    snap = _tail_access_log_for_snapshot(window_sec=window)
    now = int(time.time())

    items: list[dict] = []
    offenders: list[dict] = []
    kicked: list[dict] = []
    to_kick: list[tuple[str, str]] = []


    for uid, info in snap.items():
        row = {
            "uuid": uid,
            "sessions": info["count"],
            "last_ts": int(info["last_ts"]),
        }
        if include_ips:
            row["ips"] = info["ips"]
        items.append(row)


    for uid, info in snap.items():
        if info["count"] < min_sessions:
            continue
        if len(info["ips"]) < distinct_ips_min:
            continue


        with _db() as conn:
            row = conn.execute("SELECT sub_id, name FROM users WHERE uuid=? LIMIT 1", (uid,)).fetchone()
        if not row:
            continue

        offenders.append({
            "uuid": uid,
            "sub_id": row["sub_id"],
            "name": row["name"],
            "sessions": info["count"],
            **({"ips": info["ips"]} if include_ips else {}),
        })

        if kick:
            to_kick.append((row["sub_id"], uid))

    if kick and limit > 0:
        to_kick = to_kick[:limit]

    if kick:
        for sub_id, old_uuid in to_kick:
            kicked.append(_kick_uuid_by_sub(sub_id, old_uuid, reason="multi_session"))

    return {
        "ts": now,
        "window": int(window or SESSIONS_WINDOW_SEC),
        "threshold": int(min_sessions),
        "items": items,
        "offenders": offenders,
        "kicked": kicked,
    }


@app.post("/pause")
def pause(req: PauseReq):
    ident = (req.id or req.sub_id or req.uuid or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="empty id/sub_id/uuid")

    with _db() as conn:
        row = conn.execute(
            "SELECT sub_id, uuid FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
            (ident, ident)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="sub_or_uuid not found")

    sub_id = row["sub_id"]
    uuid_  = row["uuid"]

    with _db() as conn:
        conn.execute("UPDATE users SET status='paused' WHERE sub_id=?", (sub_id,))
        conn.commit()

    switch_live_without_downtime()

    return {"ok": True, "sub_id": sub_id, "uuid": uuid_}


@app.post("/resume")
def resume(req: ResumeReq):
    ident = (req.id or req.sub_id or req.uuid or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="empty id/sub_id/uuid")

    with _db() as conn:
        row = conn.execute(
            "SELECT sub_id, uuid, name FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
            (ident, ident)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="sub_or_uuid not found")

    sub_id = row["sub_id"]
    old    = row["uuid"]
    name   = row["name"]

    if req.rotate:
        new_uuid = str(uuid.uuid4())
        with _db() as conn:
            conn.execute("UPDATE users SET uuid=?, status='active' WHERE sub_id=?", (new_uuid, sub_id))
            conn.commit()
        switch_live_without_downtime()
        return {
            "ok": True,
            "uuid": new_uuid,
            "reality": _reality_link(new_uuid, name),
            "sub_link": _sub_link(sub_id, b64=1)
        }
    else:
        with _db() as conn:
            conn.execute("UPDATE users SET status='active' WHERE sub_id=?", (sub_id,))
            conn.commit()
        switch_live_without_downtime()
        return {
            "ok": True,
            "uuid": old,
            "reality": _reality_link(old, name),
            "sub_link": _sub_link(sub_id, b64=1)
        }


def _xray_stats_get(name: str) -> int:
    XRAY = XRAY_BIN
    def query_one(port: int, metric: str) -> int:

        try:
            out = subprocess.check_output(
                [XRAY, "api", "stats", "--server", f"127.0.0.1:{port}", "-name", metric],
                text=True, timeout=2
            )
            data = json.loads(out.strip())
            if isinstance(data, dict):
                if "stat" in data and isinstance(data["stat"], dict):
                    val = data["stat"].get("value")
                    return int(val or 0)
                if "value" in data:
                    return int(data.get("value") or 0)
        except Exception:
            pass

        try:
            out = subprocess.check_output(
                [XRAY, "api", "stats", "--server", f"127.0.0.1:{port}", "-pattern", metric],
                text=True, timeout=2
            )
            data = json.loads(out.strip())
            if isinstance(data, list):
                for item in data:
                    if item.get("name") == metric:
                        return int(item.get("value") or 0)
                    if "stat" in item and item["stat"].get("name") == metric:
                        return int(item["stat"].get("value") or 0)
        except Exception:
            pass

        return 0

    total = 0
    for p in (XRAY_API_PORT_A, XRAY_API_PORT_B):
        total += query_one(p, name)
    return total




def pull_stats_for_all_users():
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    with _db() as conn:
        users = conn.execute(
            "SELECT sub_id, uuid, upload_bytes, download_bytes, first_traffic_notified "
            "FROM users WHERE status!='deleted'"
        ).fetchall()

        for u in users:
            sub_id   = u["sub_id"]
            uid      = u["uuid"]
            prev_up  = int(u["upload_bytes"] or 0)
            prev_dn  = int(u["download_bytes"] or 0)
            was_zero = (prev_up + prev_dn) == 0
            already  = int(u["first_traffic_notified"] or 0) == 1

            up_name   = f"user>>>{uid}>>>traffic>>>uplink"
            down_name = f"user>>>{uid}>>>traffic>>>downlink"
            curr_up   = _xray_stats_get(up_name)
            curr_down = _xray_stats_get(down_name)

            cur = conn.execute("SELECT last_up, last_down FROM traffic_cursor WHERE uuid=?", (uid,)).fetchone()
            last_up   = int(cur["last_up"])   if cur else 0
            last_down = int(cur["last_down"]) if cur else 0


            delta_up   = curr_up   - last_up   if curr_up   >= last_up   else curr_up
            delta_down = curr_down - last_down if curr_down >= last_down else curr_down
            if delta_up < 0: delta_up = 0
            if delta_down < 0: delta_down = 0


            if delta_up or delta_down:
                conn.execute(
                    "UPDATE users SET upload_bytes = upload_bytes + ?, "
                    "download_bytes = download_bytes + ? WHERE uuid=?",
                    (delta_up, delta_down, uid)
                )


            if cur:
                conn.execute(
                    "UPDATE traffic_cursor SET last_up=?, last_down=?, updated_at=? WHERE uuid=?",
                    (curr_up, curr_down, now_iso, uid)
                )
            else:
                conn.execute(
                    "INSERT INTO traffic_cursor(uuid, last_up, last_down, updated_at) VALUES(?,?,?,?)",
                    (uid, curr_up, curr_down, now_iso)
                )


            if (delta_up + delta_down) > 0 and was_zero and not already:

                conn.execute(
                    "UPDATE users SET first_traffic_notified=1 WHERE sub_id=?",
                    (sub_id,)
                )

                try:
                    _notify_first_traffic(sub_id, delta_up + delta_down)
                except Exception:
                    pass

        conn.commit()


@app.post("/setname")
def set_name(req: SetNameReq):
    ident = (req.id or req.sub_id or req.uuid or "").strip()
    if not ident or not req.name.strip():
        raise HTTPException(status_code=400, detail="empty id/sub_id/uuid or name")
    with _db() as conn:
        row = conn.execute("SELECT sub_id, uuid FROM users WHERE sub_id=? OR uuid=? LIMIT 1",
                           (ident, ident)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="user not found")
        conn.execute("UPDATE users SET name=? WHERE sub_id=?", (req.name.strip(), row["sub_id"]))
        conn.commit()

    link = _reality_link(row["uuid"], req.name.strip())
    return {
        "ok": True,
        "sub_id": row["sub_id"],
        "uuid": row["uuid"],
        "reality": link,
        "sub_link": _sub_link(row["sub_id"], b64=1)
    }

def _update_user_stats_now(uuid_str: str):
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    up_name   = f"user>>>{uuid_str}>>>traffic>>>uplink"
    down_name = f"user>>>{uuid_str}>>>traffic>>>downlink"
    curr_up   = _xray_stats_get(up_name)
    curr_down = _xray_stats_get(down_name)

    with _db() as conn:
        cur = conn.execute("SELECT last_up, last_down FROM traffic_cursor WHERE uuid=?", (uuid_str,)).fetchone()
        last_up   = int(cur["last_up"])   if cur else 0
        last_down = int(cur["last_down"]) if cur else 0

        delta_up   = curr_up   - last_up   if curr_up   >= last_up   else curr_up
        delta_down = curr_down - last_down if curr_down >= last_down else curr_down
        if delta_up < 0: delta_up = 0
        if delta_down < 0: delta_down = 0

        if delta_up or delta_down:
            conn.execute(
                "UPDATE users SET upload_bytes = upload_bytes + ?, download_bytes = download_bytes + ? WHERE uuid=?",
                (delta_up, delta_down, uuid_str)
            )

        if cur:
            conn.execute(
                "UPDATE traffic_cursor SET last_up=?, last_down=?, updated_at=? WHERE uuid=?",
                (curr_up, curr_down, now_iso, uuid_str)
            )
        else:
            conn.execute(
                "INSERT INTO traffic_cursor(uuid, last_up, last_down, updated_at) VALUES(?,?,?,?)",
                (uuid_str, curr_up, curr_down, now_iso)
            )
        conn.commit()
