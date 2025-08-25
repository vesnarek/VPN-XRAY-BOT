# bot/services/api.py
import os
import socket
import logging
from typing import Optional, Tuple
from aiogram import Bot
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from bot.services import db

API_URL = os.getenv("API_URL", "").strip()
API_URL_2 = os.getenv("API_URL_2", "").strip()
LOAD_THRESH = int(os.getenv("LOAD_THRESH", "75"))
API1_CAP = int(os.getenv("API1_CAP", "200"))
API2_CAP = int(os.getenv("API2_CAP", "200"))

TIMEOUT = ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=50)

def _build_session() -> ClientSession:
    connector = TCPConnector()
    return ClientSession(timeout=TIMEOUT, connector=connector)

def _norm_base(base: Optional[str]) -> str:
    b = (base or API_URL or "").strip()
    if not b:
        raise RuntimeError("API base URL is not configured")
    return b.rstrip("/")

async def _read(r: aiohttp.ClientResponse, path: str):
    if r.status != 200:
        text = await r.text()
        return {"_error": f"{path} {r.status} {text}"}
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        return await r.json()
    return {"_error": f"{path} bad content-type {ct}"}

async def api_post(path: str, payload: dict, base: Optional[str] = None):
    base = _norm_base(base)
    url = f"{base}{path}"
    logging.info(f"[api_post] {url} payload={payload}")
    try:
        async with _build_session() as s:
            async with s.post(url, json=payload) as r:
                data = await _read(r, path)
                logging.info(f"[api_post] {url} -> {data}")
                return data
    except Exception as e:
        logging.exception(f"[api_post] {url} failed")
        return {"_error": f"{path} exception: {e}"}

async def api_get(path: str, params: dict | None = None, base: Optional[str] = None):
    base = _norm_base(base)
    url = f"{base}{path}"
    logging.info(f"[api_get] {url} params={params}")
    try:
        async with _build_session() as s:
            async with s.get(url, params=params) as r:
                data = await _read(r, path)
                logging.info(f"[api_get] {url} -> {data}")
                return data
    except Exception as e:
        logging.exception(f"[api_get] {url} failed")
        return {"_error": f"{path} exception: {e}"}



async def _sessions_count(base: str) -> Optional[int]:
    try:
        resp = await api_get("/sessions", {"window": "60", "include_ips": "true"}, base=base)
        if isinstance(resp, dict):
            if "total" in resp and isinstance(resp["total"], int):
                return resp["total"]
            if "sessions" in resp and isinstance(resp["sessions"], list):
                return len(resp["sessions"])
            if "items" in resp and isinstance(resp["items"], list):
                return len(resp["items"])
        return None
    except Exception:
        return None

async def _choose_api_base() -> str:
    primary = (API_URL or "").strip()
    secondary = (API_URL_2 or "").strip()


    if not secondary:
        return _norm_base(primary)


    c1 = await _sessions_count(primary)
    if isinstance(c1, int) and API1_CAP > 0:
        load1 = int(c1 * 100 / API1_CAP)
        logging.info(f"[load] primary {primary} sessions={c1} load≈{load1}% (cap={API1_CAP})")
        if load1 >= LOAD_THRESH:

            c2 = await _sessions_count(secondary)
            if isinstance(c2, int) and API2_CAP > 0:
                load2 = int(c2 * 100 / API2_CAP)
                logging.info(f"[load] secondary {secondary} sessions={c2} load≈{load2}% (cap={API2_CAP})")

                return _norm_base(secondary)

            return _norm_base(secondary)

        return _norm_base(primary)

    c2 = await _sessions_count(secondary)
    if isinstance(c2, int):
        logging.info(f"[load] primary N/A, using secondary {secondary} sessions={c2}")
        return _norm_base(secondary)

    logging.info("[load] neither measured; using primary by default")
    return _norm_base(primary)


async def list_users(base: Optional[str] = None):
    return await api_get("/list", base=base)

async def get_user_info(tg_id: int, base: Optional[str] = None):
    users = await list_users(base=base)
    if isinstance(users, list):
        wanted = f"tg_{tg_id}"
        for u in users:
            if str(u.get("name")) == wanted:
                return u
        return {"_error": f"User {wanted} not found"}
    return users

async def attach_ref(tg_id: int, referrer: int, base: Optional[str] = None):
    return await api_post("/ref/attach", {"tg_id": tg_id, "referrer_tg_id": referrer}, base=base)

async def create_payment(tg_id: int, amount_cents: int, base: Optional[str] = None):
    return await api_post("/payments/create", {"tg_id": tg_id, "amount_cents": amount_cents}, base=base)

async def redeem_promo(tg_id: int, code: str, base: Optional[str] = None):
    return await api_post("/promo/redeem", {"tg_id": tg_id, "code": code}, base=base)

async def admin_stats(base: Optional[str] = None):
    return await api_get("/admin/stats", base=base)

async def get_user_by_name(name: str, base: Optional[str] = None):
    users = await list_users(base=base)
    if isinstance(users, list):
        for u in users:
            if str(u.get("name")) == name:
                return u
        return {"_error": f"user {name} not found"}
    return users

async def get_balance(tg_id: int) -> int:
    balance_cents = db.get_balance_cents(tg_id)
    return balance_cents // 100

async def refresh_by_sub_id(sub_id: str, base: Optional[str] = None):
    return await api_post("/refresh", {"id": sub_id}, base=base)

async def refresh_sub(identifier: str, base: Optional[str] = None):
    return await refresh_by_sub_id(identifier, base=base)

async def resolve_sub_id_from_uuid(cur_uuid: str, base: Optional[str] = None) -> Optional[str]:
    users = await list_users(base=base)
    if isinstance(users, list):
        for u in users:
            if str(u.get("uuid")) == str(cur_uuid):
                s = (u.get("sub_id") or "").strip()
                return s or None
    return None

async def rotate_by_id(identifier: str, base: Optional[str] = None):
    return await api_post("/rotate", {"id": identifier}, base=base)

async def revoke(ident: str, base: Optional[str] = None):
    return await api_post("/revoke", {"id": ident}, base=base)

async def pause(identifier: str, base: Optional[str] = None):
    return await api_post("/pause", {"id": identifier}, base=base)

async def resume(identifier: str, rotate: bool = True, base: Optional[str] = None):
    return await api_post("/resume", {"id": identifier, "rotate": bool(rotate)}, base=base)

async def kick_multi_sessions(window: int = 60, min_sessions: int = 2, base: Optional[str] = None):
    params = {
        "kick": "true",
        "window": str(window),
        "min_sessions": str(min_sessions),
        "include_ips": "true",
        "distinct_ips_min": "2",
        "require_persistence": "2",
        "cooldown_sec": "180",
    }
    return await api_get("/sessions", params, base=base)

async def fetch_live_traffic_by_ident(ident: str, base: Optional[str] = None) -> tuple[int, int]:
    b = _norm_base(base)
    url = f"{b}/sub/{ident}?b64=0"
    try:
        timeout = ClientTimeout(total=5.0)
        async with _build_session() as session:
            async with session.get(
                url,
                headers={"Cache-Control": "no-cache"},
                ssl=False,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    return 0, 0
                hdr = (resp.headers.get("subscription-userinfo")
                       or resp.headers.get("Subscription-Userinfo")
                       or "")
        up = dn = 0
        for part in hdr.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k == "upload":
                    up = int(v or 0)
                elif k == "download":
                    dn = int(v or 0)
        return up, dn
    except Exception:
        return 0, 0



async def create_user(name: str, days: int = 30) -> dict:
    base = await _choose_api_base()
    payload = {"name": name, "days": int(days)}
    resp = await api_post("/create", payload, base=base)
    if isinstance(resp, dict) and not resp.get("_error"):
        resp["_server"] = base
    return resp
