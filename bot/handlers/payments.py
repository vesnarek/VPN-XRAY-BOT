# bot/handlers/payments.py
from aiogram import Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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


    note = "У вас 2+ устройств — можно пополнить сразу на 120 ₽." if active >= 2 else "Выберите сумму пополнения:"
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
        [InlineKeyboardButton(text=f"💳 Оплатить", url=url)],
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


# один пользователь может активировать ОДИН и тот же промокод только один раз
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
