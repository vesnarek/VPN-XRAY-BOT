import asyncio
import logging
from aiogram import Router, types, F, Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError

from bot.keyboards.common import pay_kb, back_kb
from bot.services import db
from bot.settings import MONTHLY_FEE
from bot.services.yookassa_pay import create_payment_link

router = Router()

@router.callback_query(F.data == "pay")
async def cb_pay(cq: types.CallbackQuery):
    await cq.message.edit_text("Выбери способ пополнения:", reply_markup=pay_kb())
    await cq.answer()


@router.callback_query(F.data == "pay_card")
async def cb_pay_card(cq: types.CallbackQuery):
    tg_id = cq.from_user.id

    devices = db.list_devices(tg_id)
    active = sum(1 for d in devices if str(d.get("status", "")).lower() == "active")

    buttons = [[InlineKeyboardButton(text="💳 60 ₽", callback_data="pay:card:60")]]

    if active >= 2:
        buttons.insert(0, [InlineKeyboardButton(text="💳 120 ₽", callback_data="pay:card:120")])

    buttons.append([InlineKeyboardButton(text="↩ Назад", callback_data="pay")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    note_lines = ["Выберите сумму пополнения:"]
    if active >= 2:
        note_lines.append("У вас 2+ устройств — можно пополнить сразу на 120 ₽.")
    note = "\n\n".join(note_lines)
    await cq.message.edit_text(note, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.regexp(r"^pay:card:(\d+)$"))
async def cb_pay_card_amount(cq: types.CallbackQuery):
    amount = int(cq.data.split(":")[-1])
    tg_id = cq.from_user.id

    resp = create_payment_link(tg_id, amount, description="Пополнение баланса VPN")
    if not resp.get("ok"):
        await cq.message.edit_text(
            "❌ Не удалось создать платёж. Попробуйте позже.",
            reply_markup=back_kb()
        )
        await cq.answer()
        return

    url = resp["url"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=url)],
        [InlineKeyboardButton(text="↩ Назад", callback_data="pay")]
    ])

    await cq.message.edit_text(
        f"Вы выбрали оплату {amount} ₽. Нажмите кнопку ниже для перехода к оплате:",
        reply_markup=kb
    )
    await cq.answer()


@router.callback_query(F.data == "pay_promo")
async def cb_pay_promo(cq: types.CallbackQuery):
    await cq.message.edit_text(
        "Введи свой промокод:",
        reply_markup=back_kb()
    )
    await cq.answer()


@router.message(F.text.regexp(r"^[A-Za-z0-9_-]{6,32}$"))
async def catch_promo(message: types.Message):
    code = message.text.strip()
    tg_id = message.from_user.id

    with db.db() as con:
        used = con.execute(
            "SELECT 1 FROM payments WHERE tg_id=? AND method='promo' AND ref=? LIMIT 1",
            (tg_id, code)
        ).fetchone()
        if used:
            await message.answer("Вы уже активировали этот промокод.")
            return

        row = con.execute(
            "SELECT amount_cents, uses_left FROM promos WHERE code=?",
            (code,)
        ).fetchone()

        if not row or int(row["uses_left"]) <= 0:
            await message.answer("Промокод не найден или уже использован.")
            return

        cur = con.execute(
            "UPDATE promos SET uses_left = uses_left - 1 WHERE code=? AND uses_left > 0",
            (code,)
        )
        if cur.rowcount == 0:
            await message.answer("Промокод уже использован.")
            return

        amount_cents = int(row["amount_cents"])

    db.add_balance(tg_id, amount_cents, method="promo", ref=code)
    await message.answer("✅ Промокод активирован.")


# === ФОНОВЫЙ УВЕДОМИТЕЛЬ КАРТОЧНЫХ ПЛАТЕЖЕЙ ===

log = logging.getLogger(__name__)

async def run_card_payment_notifier(bot: Bot, poll_interval: float = 2.0):
    """
    Смотрим новые записи в payments(method='card') и шлём юзеру сообщение о зачислении.
    """
    last_id = 0
    try:
        with db.db() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(id),0) FROM payments WHERE method='card'"
            ).fetchone()
            last_id = int(row[0] or 0)
    except Exception as e:
        log.warning("[card_notifier] init last_id failed: %s", e)

    log.info("[card_notifier] start from payment id > %s", last_id)

    backoff = 1.0
    while True:
        try:
            with db.db() as con:
                rows = con.execute(
                    """
                    SELECT id, tg_id, amount_cents, ref, created_at
                    FROM payments
                    WHERE method='card' AND id > ?
                    ORDER BY id ASC
                    LIMIT 300
                    """,
                    (last_id,)
                ).fetchall()

            if not rows:
                backoff = 1.0
                await asyncio.sleep(poll_interval)
                continue

            for r in rows:
                pid   = int(r["id"])
                tg_id = int(r["tg_id"])
                rub   = int(r["amount_cents"]) // 100

                try:
                    balance_rub = db.get_balance_cents(tg_id) // 100
                except Exception:
                    balance_rub = None

                text = (
                    "💳 <b>Платёж зачислен</b>\n\n"
                    f"+{rub} ₽ на ваш баланс."
                    + (f"\nТекущий баланс: <b>{balance_rub} ₽</b>" if balance_rub is not None else "")
                )

                try:
                    await bot.send_message(tg_id, text, parse_mode="HTML")
                    last_id = pid

                except TelegramRetryAfter as e:
                    delay = max(1, int(getattr(e, "retry_after", 5)))
                    log.warning("[card_notifier] 429, sleep %ss", delay)
                    await asyncio.sleep(delay)
                    break

                except TelegramForbiddenError as e:
                    log.warning("[card_notifier] 403 for user %s: %s", tg_id, e)
                    last_id = pid

                except TelegramAPIError as e:
                    log.error("[card_notifier] TelegramAPIError: %s", e)
                    await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)
                    break

                except Exception as e:
                    log.exception("[card_notifier] send error for pid=%s: %s", pid, e)
                    await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)
                    break

        except Exception as e:
            log.exception("[card_notifier] loop error: %s", e)
            await asyncio.sleep(2.0)
            backoff = min(backoff * 2, 60)
