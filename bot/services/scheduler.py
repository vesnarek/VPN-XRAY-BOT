import asyncio
import logging
from aiogram import Bot

from bot.services import db, api
from bot.settings import MONTHLY_FEE, DAILY_FEE_C

LOW_BALANCE_CENTS = 1000  # 10 ₽
_last_notif_day = 0


async def mark_activated_if_ready():
    return


def _start_of_utc_day(ts: int | None = None) -> int:
    ts = ts if ts is not None else db.now()
    return ts - (ts % 86400)


def _sec(x) -> int:
    try:
        v = int(x or 0)
    except Exception:
        return 0
    return v // 1000 if v > 10**11 else v


async def daily_billing_tick():
    charge_cents = int(DAILY_FEE_C())
    if charge_cents <= 0:
        return

    sod = _start_of_utc_day()

    with db.db() as con:
        rows = con.execute("""
            SELECT id, uuid, tg_id, status, activated_at, last_billed, sub_id
            FROM devices
            WHERE status='active' AND activated_at IS NOT NULL
            ORDER BY id
        """).fetchall()

    if not rows:
        return

    for r in rows:
        uuid   = (r["uuid"]   or "").strip()
        sub_id = (r["sub_id"] or "").strip()
        tg_id  = int(r["tg_id"])

        activated_at = _sec(r["activated_at"])
        last_billed  = _sec(r["last_billed"])

        if activated_at <= 0 or activated_at > sod or last_billed >= sod:
            continue

        charged = False
        with db.db() as con:
            cur = con.execute("SELECT balance_cents FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            cur_cents = int(cur["balance_cents"] if cur else 0)

            if cur_cents >= charge_cents:
                res = con.execute(
                    "UPDATE users SET balance_cents = balance_cents - ? "
                    "WHERE tg_id=? AND balance_cents >= ?",
                    (charge_cents, tg_id, charge_cents)
                )
                if res.rowcount == 1:
                    ref = f"uuid:{uuid}" if uuid else f"dev:{r['id']}"
                    db.add_balance_con(con, tg_id, -charge_cents, "daily", ref)
                    con.execute("UPDATE devices SET last_billed=? WHERE uuid=?", (sod, uuid))
                    charged = True
                    logging.info("[billing] charged uuid=%s tg=%s -%dc", uuid, tg_id, charge_cents)

        if charged:
            continue

        with db.db() as con:
            con.execute("UPDATE devices SET status='paused' WHERE uuid=?", (uuid,))
            db.log_event_con(con, tg_id, "auto_pause", f"uuid={uuid}")

        ident = sub_id or uuid
        if ident:
            try:
                await api.pause(ident)
            except Exception as e:
                logging.warning("[billing] api.pause failed for %s: %s", ident, e)


async def send_low_balance_notifications(bot: Bot):
    global _last_notif_day
    start_of_day = _start_of_utc_day()

    if _last_notif_day == start_of_day:
        return
    _last_notif_day = start_of_day

    with db.db() as con:
        rows = con.execute("""
            SELECT u.tg_id, u.balance_cents
            FROM users u
            WHERE u.balance_cents < ?
              AND EXISTS (
                  SELECT 1 FROM devices d
                  WHERE d.tg_id = u.tg_id AND d.status = 'active'
              )
        """, (LOW_BALANCE_CENTS,)).fetchall()

    daily_rub = max(1, round(MONTHLY_FEE / 30))
    for r in rows:
        tg_id = r["tg_id"]
        bal_rub = r["balance_cents"] // 100
        try:
            await bot.send_message(
                tg_id,
                f"⚠️ Баланс {bal_rub} ₽ — меньше 10 ₽.\n"
                f"Списание ~{daily_rub} ₽/день. Пополните баланс, иначе подписка уйдёт на паузу."
            )
        except Exception as e:
            logging.warning("low-balance notify failed for %s: %s", tg_id, e)


async def run_scheduler(bot: Bot):
    while True:
        try:
            await mark_activated_if_ready()
            await daily_billing_tick()
            await send_low_balance_notifications(bot)
        except Exception as e:
            logging.exception("scheduler error: %s", e)
        await asyncio.sleep(3600)
