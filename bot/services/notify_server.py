from aiohttp import web
import json, logging
from bot.services import db

BONUS_CENTS = 2000

async def make_app(bot):
    async def handle_root(request: web.Request):
        return web.Response(text="ok", content_type="text/plain")

    async def handle_first_traffic(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        sub_id = (data.get("sub_id") or "").strip()
        if not sub_id:
            return web.json_response({"ok": False, "error": "missing sub_id"}, status=400)

        # находим устройство по sub_id
        with db.db() as con:
            dev = con.execute(
                "SELECT uuid, tg_id, name FROM devices WHERE sub_id=? LIMIT 1",
                (sub_id,)
            ).fetchone()
        if not dev:
            return web.json_response({"ok": False, "error": "device for sub_id not found"}, status=404)

        uuid = dev["uuid"]
        invitee_tg = int(dev["tg_id"])


        result = db.activate_device_and_maybe_referral(uuid)
        granted = bool(result.get("granted"))

        if granted:

            with db.db() as con:
                row = con.execute(
                    "SELECT referrer FROM users WHERE tg_id=? LIMIT 1",
                    (invitee_tg,)
                ).fetchone()
                referrer_tg = int(row["referrer"]) if (row and row["referrer"]) else None


                con.execute("UPDATE users SET balance_cents = COALESCE(balance_cents,0) + ? WHERE tg_id=?",
                            (BONUS_CENTS, invitee_tg))
                if referrer_tg:
                    con.execute("UPDATE users SET balance_cents = COALESCE(balance_cents,0) + ? WHERE tg_id=?",
                                (BONUS_CENTS, referrer_tg))
                con.commit()


            try:
                await bot.send_message(
                    invitee_tg,
                    "🎁 <b>Бонус за старт!</b>\n\n"
                    "Первое подключение зафиксировано.\n"
                    "На ваш баланс зачислено <b>20 ₽</b>."
                )
            except Exception as e:
                logging.warning(f"notify invitee failed: {e}")

            if referrer_tg:
                try:
                    await bot.send_message(
                        referrer_tg,
                        "🎉 <b>Ваш друг подключился!</b>\n\n"
                        "На ваш баланс зачислено <b>20 ₽</b> за приглашение."
                    )
                except Exception as e:
                    logging.warning(f"notify referrer failed: {e}")

        return web.json_response({"ok": True, **result})

    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_post("/notify/first_traffic", handle_first_traffic)
    return app

async def start_notify_server(bot):
    app = await make_app(bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8081)
    await site.start()
