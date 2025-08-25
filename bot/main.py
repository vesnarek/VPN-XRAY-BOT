import asyncio
import importlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties

from bot.settings import BOT_TOKEN
from bot.services import db, api
from bot.services.scheduler import run_scheduler
from bot.services.balance_guard import run_balance_guard
from bot.handlers.referral import run_referral_notifier
from bot.services.notify_server import start_notify_server

logging.basicConfig(level=logging.INFO)


def build_dp() -> Dispatcher:
    dp = Dispatcher()
    for name in ["start", "home", "vpn", "payments", "referral", "support", "admin"]:
        mod = importlib.import_module(f"bot.handlers.{name}")
        dp.include_router(mod.router)
    return dp


async def run_multi_guard(bot: Bot):
    while True:
        try:
            data = await api.kick_multi_sessions(window=60, min_sessions=2)

            for item in data.get("kicked", []):
                sub_id = item.get("sub_id")
                old_uuid = item.get("old_uuid")
                new_uuid = item.get("new_uuid")

                with db.db() as con:
                    dev = con.execute(
                        "SELECT tg_id, name FROM devices WHERE sub_id=? AND uuid=?",
                        (sub_id, old_uuid)
                    ).fetchone()

                    if dev:
                        con.execute(
                            "UPDATE devices SET uuid=? WHERE sub_id=? AND uuid=?",
                            (new_uuid, sub_id, old_uuid)
                        )
                        con.commit()

                        tg_id = dev["tg_id"]
                        dev_name = dev["name"] or "ваше устройство"

                        try:
                            await bot.send_message(
                                tg_id,
                                (
                                    "🚫 Нарушение правил!\n\n"
                                    f"Ключ <b>{dev_name}</b> был использован на нескольких устройствах.\n"
                                    "Он аннулирован, вам выдан новый.\n\n"
                                    "⚠️ Обновите VPN в приложении.\n"
                                    "Многократные нарушения могут привести к блокировке подписки."
                                )
                            )
                        except Exception as e:
                            logging.warning(f"Не смог отправить уведомление {tg_id}: {e}")

        except Exception as e:
            logging.error(f"multi_guard error: {e}")

        await asyncio.sleep(30)


async def main():
    db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dp()

    asyncio.create_task(run_scheduler(bot))
    asyncio.create_task(run_balance_guard())
    asyncio.create_task(run_multi_guard(bot))
    asyncio.create_task(run_referral_notifier(bot))

    asyncio.create_task(start_notify_server(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
