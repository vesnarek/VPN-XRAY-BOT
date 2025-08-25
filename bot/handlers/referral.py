from aiogram import Router, types, F, Bot
import asyncio
import logging
from bot.views.render import referral_text
from bot.keyboards.common import back_kb
from bot.services import db

router = Router()


@router.callback_query(F.data == "ref")
async def cb_ref(cq: types.CallbackQuery, bot: Bot):
    me = await bot.get_me()
    txt = referral_text(cq.from_user.id, me.username)
    await cq.message.edit_text(txt, reply_markup=back_kb())
    await cq.answer()


# --- фоновые уведомления о бонусах ---
async def run_referral_notifier(bot: Bot, poll_interval: float = 2.0):
    """
    Следит за новыми payments.method='referral' и шлёт уведомления.
    """
    last_id = 0
    try:
        with db.db() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(id),0) FROM payments WHERE method='referral'"
            ).fetchone()
            last_id = int(row[0] or 0)
    except Exception as e:
        logging.warning(f"[referral_notifier] init last_id failed: {e}")

    logging.info(f"[referral_notifier] start from payment id > {last_id}")

    while True:
        try:
            with db.db() as con:
                rows = con.execute(
                    """
                    SELECT id, tg_id, amount_cents, ref, created_at
                    FROM payments
                    WHERE method='referral' AND id > ?
                    ORDER BY id ASC
                    """,
                    (last_id,)
                ).fetchall()

            for r in rows:
                pid   = int(r["id"])
                tg_id = int(r["tg_id"])
                rub   = int(r["amount_cents"]) // 100

                text = (
                    "🎁 <b>Бонус за приглашение</b>\n\n"
                    f"На ваш баланс зачислено <b>{rub} ₽</b>."
                )
                try:
                    await bot.send_message(tg_id, text, parse_mode="HTML")
                except Exception as e:
                    logging.warning(f"[referral_notifier] send to {tg_id} failed: {e}")

                last_id = pid

        except Exception as e:
            logging.error(f"[referral_notifier] loop error: {e}")

        await asyncio.sleep(poll_interval)
