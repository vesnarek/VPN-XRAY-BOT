from aiogram import Router, types, F
from bot.keyboards.common import support_kb
from bot.settings import SUPPORT_USER

router = Router()

@router.callback_query(F.data == "sup")
async def cb_support(cq: types.CallbackQuery):
    await cq.message.edit_text(f"Поддержка — {SUPPORT_USER}", reply_markup=support_kb())
    await cq.answer()
