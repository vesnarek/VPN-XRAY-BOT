# bot/views/render.py
from __future__ import annotations
from datetime import datetime
from typing import Dict, List, Optional
from bot.settings import MONTHLY_FEE

# --- helpers -----------------------------------------------------------------

def _fmt_dt_iso(s: Optional[str]) -> str:
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s

def _device_display_name(d: Dict, idx: int = 1) -> str:
    for k in ("name", "label", "model", "title"):
        val = d.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    uid = (d.get("uuid") or d.get("id") or "")
    if uid:
        return f"Устройство {idx} · …{str(uid)[-6:]}"
    return f"Устройство {idx}"

def _device_status_ru(d: Dict) -> str:
    st = (d.get("status") or "active").lower()
    if st == "paused":
        return "на паузе"
    if st == "pending":
        return "ожидает активации"
    if st == "deleted":
        return "удалено"
    return "активен"

def _daily_fee_rub() -> int:
    # ≈ MONTHLY_FEE / 30, минимум 1 ₽
    if not MONTHLY_FEE:
        return 0
    return max(1, round(MONTHLY_FEE / 30))

# --- публичные вьюхи ---------------------------------------------------------

def promo_text() -> str:
    return (
        "Надёжный и быстрый VPN. Настройка за минуту.\n"
        "Нажмите «Активировать VPN» и получите 20 ₽ (≈10 дней)."
    )

def status_and_balance_text(devices: List[Dict], balance_cents: int) -> str:

    status = "нет устройств"
    expires = "—"
    if devices:
        devices = sorted(devices, key=lambda u: u.get("created_at", 0))
        last = devices[-1]
        status = _device_status_ru(last)
        if last.get("expires_at"):
            expires = _fmt_dt_iso(last["expires_at"])
    balance_rub = (balance_cents or 0) // 100
    daily = _daily_fee_rub()
    days_left = (balance_rub // daily) if daily > 0 else 0
    line1 = f"Статус подписки: {status}" + (f" до {expires}" if status == "активен" and expires != "—" else "")
    line2 = f"Баланс: <b>{balance_rub} ₽</b>" + (f" (≈ {days_left} дней)" if daily > 0 else "")
    return f"{line1}\n{line2}"

def device_card(d: Dict, idx: int = 1) -> str:
    name = _device_display_name(d, idx=idx)
    st = _device_status_ru(d)
    created = _fmt_dt_iso(d.get("created_at"))
    expires = _fmt_dt_iso(d.get("expires_at"))
    daily = _daily_fee_rub()
    parts = [
        f"Устройство: {name}",
        f"Статус: {st}",
        f"Дата создания: {created}",
    ]
    if expires != "—":
        parts.append(f"Действует до: {expires}")
    if daily > 0:
        parts.append(f"Дневной платёж ≈ {daily} ₽ (первое списание через 24 часа)")
    return "\n".join(parts)

# На совместимость со старым импортом
vpn_card = device_card

def os_instruction(os_code: str) -> str:
    oc = (os_code or "").lower()
    if oc in ("ios", "iphone", "ipad"):
        return ("📱 Инструкция для iPhone/iPad:\n\n"
                "1) Установите приложение *v2RayTun* из App Store\n"
                "2) Скопируйте ссылку, нажав на неё (персональная ссылка)\n"
                "3) В приложении нажмите «+» в верхнем правом углу, выберите *Добавить из буфера*\n"
                "4) Нажмите на кнопку включить и разрешите добавление конфигурации\n\n"
                "✅ Всё работает!")
    if oc == "android":
        return ("🤖 Инструкция для Android:\n\n"
                "1) Установите приложение *v2RayTun* из Google Play по ссылке:\n"
                "   https://play.google.com/store/apps/details?id=com.v2raytun.android\n"
                "2) Скопируйте ссылку, нажав на неё (персональная ссылка)\n"
                "3) В приложении нажмите «+» в верхнем правом углу, выберите *Импортировать из буфера*\n"
                "4) Нажмите на кнопку включить и разрешите добавление конфигурации\n\n"
                "✅ Всё работает!")
    if oc == "windows":
        return ("💻 Инструкция для Windows:\n\n"
                "1) Установите приложение *v2RayTun* по ссылке:\n"
                "   https://v2raytun-install.ru/v2RayTun_Setup.exe\n"
                "2) Скопируйте ссылку (персональная ссылка)\n"
                "3) В приложении нажмите «+» в верхнем правом углу, выберите *Импорт из буфера обмена*\n"
                "4) Включите VPN\n\n"
                "✅ Всё работает!")
    if oc == "macos":
        return ("🍏 Инструкция для Mac:\n\n"
                "1) Установите приложение *v2RayTun* из App Store\n"
                "2) Скопируйте ссылку (персональная ссылка)\n"
                "3) В приложении нажмите «+» в верхнем правом углу, выберите *Импорт из буфера обмена*\n"
                "4) Нажмите на кнопку включить и разрешите добавление конфигурации\n\n"
                "✅ Всё работает!")
    return "ℹ️ Инструкция: скопируйте ссылку, вставьте её в приложение v2RayTun и включите VPN."


def referral_text(my_tg_id: int, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start=ref_{my_tg_id}"
    return (
        "🎁 Пригласи друга и получи 10 дней бесплатного VPN!\n\n"
        "Как это работает:\n"
        "1) Поделись с ним своей ссылкой\n"
        "2) Друг зарегистрируется и начнёт пользоваться VPN\n"
        "3) На твой баланс зачислят 20 ₽ ✅\n\n"
        f"🔗 Твоя ссылка: {link}\n\n"
        "🙌 Друг тоже получит 10 дней бесплатного VPN"
    )

# --- новый главный экран -----------------------------------------------------

def main_menu_text(fullname: Optional[str], balance_cents: int, active_devices: int) -> str:
    name = fullname or "друг"
    balance_rub = max(0, (balance_cents or 0) // 100)

    daily_one = _daily_fee_rub()  # обычно 2 ₽
    active = max(0, active_devices)
    daily_total = daily_one * active

    if daily_total > 0:
        days_left = balance_rub // daily_total
    else:
        days_left = balance_rub // daily_one if daily_one > 0 else 0

    return (
        f"Привет, {name} 👋\n\n"
        f"💰 <b>Ваш баланс: {balance_rub}₽ ({days_left} дней)</b>\n\n"
        f"Тариф: {daily_one}₽/день за одно устройство\n"
        f"Текущее списание: {daily_total}₽/день\n\n"
        "Не работает VPN? 🛠\n"
        "Главная → Мои устройства → Ваше устройство → Обновить\n\n"
        "Ключ обновится автоматически, ничего менять не нужно.\n"
        "Для корректной работы переподключитесь к VPN.\n\n"
        "🙋‍♂️ За каждого приглашённого друга на баланс начисляется 10 дней (20₽) 🙋‍♀️"
    )

