import asyncio
import time
import random
import logging
from typing import Iterable, Dict, Any

from bot.services import api
from bot.services import db


CHECK_INTERVAL_SEC = 120
JITTER_SEC = 30
COOLDOWN_SEC = 300
ACTIONS_PER_PASS = 30

_last_action_ts: dict[str, float] = {}  # uuid -> timestamp

def _cooldown_ok(uuid: str) -> bool:
    now = time.time()
    ts = _last_action_ts.get(uuid, 0)
    if now - ts >= COOLDOWN_SEC:
        _last_action_ts[uuid] = now
        return True
    return False

def _iter_devices() -> Iterable[Dict[str, Any]]:

    with db.db() as con:
        rows = con.execute(
            """
            SELECT d.uuid, d.tg_id, d.name, d.status, u.balance_cents
            FROM devices d
            JOIN users u ON u.tg_id = d.tg_id
            WHERE d.status != 'deleted'
            ORDER BY d.id
            """
        ).fetchall()
        for r in rows:
            yield dict(r)

async def run_balance_guard():

    while True:
        try:
            actions = 0
            for dev in _iter_devices():
                uuid = dev["uuid"]
                tg_id = int(dev["tg_id"])
                status = str(dev["status"] or "")
                balance_cents = int(dev["balance_cents"] or 0)


                if balance_cents <= 0 and status == "active":
                    if _cooldown_ok(uuid):
                        logging.info(f"[balance_guard] revoke {uuid} (tg_id={tg_id}) bal_cents={balance_cents}")
                        resp = await api.revoke(uuid)

                        db.set_device_status(uuid, "paused")
                        actions += 1


                elif balance_cents > 0 and status in ("paused", "pending"):
                    if _cooldown_ok(uuid):
                        logging.info(f"[balance_guard] refresh {uuid} (tg_id={tg_id}) bal_cents={balance_cents}")
                        resp = await api.refresh_uuid(uuid)
                        db.set_device_status(uuid, "active")
                        actions += 1

                if actions >= ACTIONS_PER_PASS:
                    break

        except Exception as e:
            logging.exception(f"[balance_guard] loop error: {e}")


        await asyncio.sleep(CHECK_INTERVAL_SEC + random.randint(-JITTER_SEC, JITTER_SEC))
