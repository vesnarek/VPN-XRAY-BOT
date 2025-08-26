from __future__ import annotations
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict, Optional
import math, time
from bot.settings import MAX_DEVICES_PER_USER
def _safe_text(s: str, max_len: int = 32) -> str:

    if not s:
        return s
    s = s.strip()
    return s if len(s) <= max_len else (s[:max_len - 1] + "…")

def _safe_cb(data: str, max_len: int = 64) -> str:

    if len(data) <= max_len:
        return data
    head = data[:max_len - 3]
    return head + "..."

def _device_display_name(d: Dict, idx: int) -> str:

    for k in ("name", "label", "model", "title"):
        val = d.get(k)
        if isinstance(val, str) and val.strip():
            return _safe_text(val)
    uid = (d.get("uuid") or d.get("id") or "").strip()
    if uid:
        tail = uid[-6:]
        return f"Устройство {idx} · …{tail}"
    return f"Устройство {idx}"

def _device_id_of(d: Dict, fallback: str) -> str:

    dev_id = d.get("id") or d.get("uuid") or d.get("name") or fallback
    return str(dev_id)





def first_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активировать VPN", callback_data="welcome_activate")]
    ])




def main_kb(has_devices: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_devices:
        rows.append([InlineKeyboardButton(text="📱 Мои устройства", callback_data="devices")])
    else:
        rows.append([InlineKeyboardButton(text="⚡ Настроить VPN", callback_data="vpn_setup")])

    rows += [
        [
            InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="pay"),
            InlineKeyboardButton(text="👥 Пригласить", callback_data="ref"),
        ],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="sup")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin:genpromo")],
        [InlineKeyboardButton(text="🏠 В обычное меню", callback_data="home")],
    ])

def admin_users_kb(offset: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    page = offset // 20 + 1
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:users:{max(0, offset-20)}"))
    nav.append(InlineKeyboardButton(text=f"Стр. {page}", callback_data="admin:users:{}".format(offset)))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️ Далее", callback_data=f"admin:users:{offset+20}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)




def os_kb(show_back: bool = True) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="🍎 iOS", callback_data="os_ios"),
            InlineKeyboardButton(text="🤖 Android", callback_data="os_android"),
        ],
        [
            InlineKeyboardButton(text="🖥 Windows", callback_data="os_windows"),
            InlineKeyboardButton(text="🍎 macOS", callback_data="os_macos"),
        ],
    ]
    if show_back:
        keyboard.append([InlineKeyboardButton(text="Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)




def confirm_create_kb(os_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data=_safe_cb(f"confirm_buy:{os_code}"))],
        [InlineKeyboardButton(text="Назад", callback_data="vpn_setup")],
    ])

def first_buy_kb(balance_ok: bool, monthly_fee: int) -> InlineKeyboardMarkup:

    rows = []
    if balance_ok:
        rows.append([InlineKeyboardButton(text="Получить ключ✅", callback_data="buy")])
    else:
        rows.append([InlineKeyboardButton(text="Пополнить баланс", callback_data="pay")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="vpn_setup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def confirm_buy_kb(os_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data=_safe_cb(f"confirm_buy:{os_code}"))],
        [InlineKeyboardButton(text="Назад", callback_data="vpn_setup")],
    ])




def devices_list_kb(devices: List[Dict]) -> InlineKeyboardMarkup:
    rows = []

    if devices:
        for i, d in enumerate(devices, start=1):
            dev_id = _device_id_of(d, fallback=str(i))
            name = _device_display_name(d, idx=i)
            rows.append([
                InlineKeyboardButton(
                    text=name,
                    callback_data=_safe_cb(f"dev:{dev_id}:open")
                )
            ])
    else:

        rows.append([InlineKeyboardButton(text="Настроить VPN", callback_data="vpn_setup")])


    if len(devices) < MAX_DEVICES_PER_USER:
        rows.append([InlineKeyboardButton(text="Добавить устройство", callback_data="vpn_setup")])

    rows.append([InlineKeyboardButton(text="Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def device_actions_basic_kb(uuid: str, created_or_activated_at: int | None = None) -> InlineKeyboardMarkup:
    can_delete = True
    remain_hours = 0
    if created_or_activated_at:
        diff = int(time.time()) - int(created_or_activated_at)
        if diff < 24 * 3600:
            can_delete = False
            remain_hours = max(1, math.ceil((24*3600 - diff) / 3600))

    rows = [
        [InlineKeyboardButton(text="🔄 Обновить конфиг", callback_data=f"dev:{uuid}:refresh")],
    ]

    if can_delete:
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"dev:{uuid}:delete")])
    else:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить (через ~{remain_hours} ч)",
            callback_data=f"dev:{uuid}:delete_blocked"
        )])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="devices")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_actions_kb(uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить конфиг", callback_data=f"dev:{uuid}:refresh")],
        [InlineKeyboardButton(text="Назад",           callback_data=f"dev:{uuid}:open")],
    ])




def pay_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Картой", callback_data="pay_card")],
        [InlineKeyboardButton(text="Промокод", callback_data="pay_promo")],
        [InlineKeyboardButton(text="Назад", callback_data="home")],
    ])

def pay_amount_kb() -> InlineKeyboardMarkup:
    # сейчас один фикс 60 ₽; позже можно добавить 120/180…
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пополнить на 60 ₽", callback_data="pay_amount:60")],
        [InlineKeyboardButton(text="Назад", callback_data="home")],
    ])

def pay_link_kb(payment_id: str) -> InlineKeyboardMarkup:
    # callback для проверки статуса по payment_id
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить оплату", callback_data=f"yk:check:{payment_id}")],
        [InlineKeyboardButton(text="Вернуться", callback_data="home")],
    ])

def support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="home")]
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="home")]
    ])
