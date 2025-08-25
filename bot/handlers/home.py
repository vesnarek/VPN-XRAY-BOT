from __future__ import annotations
from aiogram import Router, types, F
from bot.keyboards.common import main_kb
from bot.views.render import main_menu_text
from bot.services import db

router = Router()

async def _user_devices(tg_id: int) -> list[dict]:

    return db.list_devices(tg_id)

@router.callback_query(F.data == "home")
async def cb_home(cq: types.CallbackQuery):
    tg_id = cq.from_user.id
    devices = await _user_devices(tg_id)
    balance_cents = db.get_balance_cents(tg_id)


    active_devices = sum(1 for d in devices if str(d.get("status", "")).lower() == "active")


    fullname = cq.from_user.full_name or "друг"

    text = main_menu_text(
        fullname=fullname,
        balance_cents=balance_cents,
        active_devices=active_devices,
    )
    kb = main_kb(has_devices=bool(devices))

    try:

        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await cq.message.edit_reply_markup(kb)
        except Exception:
            pass
    await cq.answer()
