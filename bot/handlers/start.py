# bot/handlers/start.py
import logging
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject

from bot.keyboards.common import first_start_kb, main_kb, os_kb
from bot.views.render import promo_text, main_menu_text
from bot.services import db, api
import logging, html

router = Router()

WELCOME_BONUS_CENTS = 20 * 100  # 20 ₽

async def _user_devices(tg_id: int) -> list[dict]:
    # Только устройства текущего пользователя из локальной БД
    return db.list_devices(tg_id)

def _username_from_db_or_tg(tg_id: int, tg_username: str | None) -> str | None:
    with db.db() as con:
        r = con.execute("SELECT username FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return (r["username"] if r and r["username"] else (tg_username or None))


@router.message(Command("start"))
async def start(message: types.Message, command: CommandObject):
    tg_id = message.from_user.id
    # создаём пользователя и обновляем username (как было)
    db.ensure_user(tg_id)
    db.update_username(tg_id, (message.from_user.username or "").strip())

    # рефералка
    try:
        if command and command.args and command.args.startswith("ref_"):
            ref_id = int(command.args.split("_", 1)[1])
            if ref_id != tg_id:
                await api.attach_ref(tg_id, ref_id)
    except Exception:
        logging.exception("[start] attach_ref failed")

    devices = await _user_devices(tg_id)

    # первый визит + не выдавали бонус → экран с «Активировать VPN»
    if not devices and not db.got_welcome(tg_id):
        await message.answer(promo_text(), reply_markup=first_start_kb())
        return

    # главный экран
    balance_cents = db.get_balance_cents(tg_id)
    active_devices = sum(1 for d in devices if str(d.get("status", "")).lower() == "active")

    # используем имя профиля, а не @username
    fullname = html.escape(message.from_user.full_name or "друг")

    text = main_menu_text(
        fullname=fullname,
        balance_cents=balance_cents,
        active_devices=active_devices,
    )

    await message.answer(text, parse_mode="HTML", reply_markup=main_kb(has_devices=bool(devices)))


# ВАЖНО: callback_data в клавиатуре должна быть "welcome_activate"
@router.callback_query(F.data == "welcome_activate")
async def cb_welcome_activate(cq: types.CallbackQuery):
    tg_id = cq.from_user.id
    db.ensure_user(tg_id)
    db.update_username(tg_id, (cq.from_user.username or "").strip())

    note = ""
    if not db.got_welcome(tg_id):
        db.add_balance(tg_id, WELCOME_BONUS_CENTS, method="promo", ref="welcome")
        db.set_welcome_given(tg_id)
        note = "✅ Начислено 20 ₽ приветственного бонуса.\n\n"

    # ВАЖНО: первый раз — выбор ОС без кнопки «Назад»
    await cq.message.edit_text(
        note + "Выберите операционную систему:",
        reply_markup=os_kb(show_back=False)
    )
    await cq.answer()

