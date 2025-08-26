import asyncio
import logging
from aiogram import Router, F, types
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
import time
from bot.keyboards.common import (
    os_kb,
    first_buy_kb,
    confirm_buy_kb,
    devices_list_kb,
    back_kb,
    device_actions_basic_kb,
    key_actions_kb
)
from bot.views.render import os_instruction
from bot.services import api, db
from bot.settings import DEFAULT_DAYS, MONTHLY_FEE, API_URL
from bot.settings import MAX_DEVICES_PER_USER

router = Router()

_REFRESH_READY: dict[tuple[int, str], float] = {}


async def safe_edit(msg: types.Message, text: str, retries: int = 3, **kwargs):
    for _ in range(retries):
        try:
            return await msg.edit_text(text, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 1)) + 1)
        except TelegramNetworkError:
            await asyncio.sleep(1)
        except Exception:
            break
    try:
        return await msg.answer(text, **kwargs)
    except Exception:
        return None

async def safe_answer(cq: types.CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await cq.answer(text=text, show_alert=show_alert)
    except TelegramNetworkError:
        pass
    except Exception:
        pass



async def _user_devices(tg_id: int) -> list[dict]:
    return db.list_devices(tg_id)

def _next_device_name(os_code: str, existing: list[dict], uuid: str | None = None) -> str:

    base = "iOS" if os_code.lower() == "ios" else os_code.capitalize()


    suffix = ""
    if uuid:
        tail = uuid.replace("-", "")[-4:]
        suffix = f" {tail.upper()}"

    used = {str(u.get("name", "")).strip() for u in existing}

    cand = f"{base}{suffix}"
    if cand not in used:
        return cand

    i = 1
    while True:
        cand = f"{base}{suffix} ({i})"
        if cand not in used:
            return cand
        i += 1

def _find_device_local(device_id: str, tg_id: int) -> dict | None:
    try:
        d = db.device_by_uuid(device_id)
        if d and d.get("tg_id") == tg_id and d.get("status") != "deleted":
            return d
    except Exception:
        pass
    for field in ("id", "name"):
        try:
            with db.db() as con:
                r = con.execute(
                    f"SELECT * FROM devices WHERE {field}=? AND tg_id=? AND status!='deleted'",
                    (int(device_id) if field == "id" else device_id, tg_id)
                ).fetchone()
                if r:
                    return dict(r)
        except Exception:
            pass
    return None



@router.callback_query(F.data == "vpn_setup")
async def vpn_setup(cq: types.CallbackQuery):
    await safe_edit(cq.message, "Выберите операционную систему:", reply_markup=os_kb())
    await safe_answer(cq)

@router.callback_query(F.data.startswith("os_"))
async def choose_os(cq: types.CallbackQuery):
    os_code = cq.data.split("_", 1)[1]
    devices = await _user_devices(cq.from_user.id)
    bal_rub = await api.get_balance(cq.from_user.id)
    need = 2 * (len(devices) + 1)
    afford = bal_rub >= need

    # отображаемое имя ОС + эмодзи
    os_l = os_code.lower()
    platform = "iOS" if os_l == "ios" else ("macOS" if os_l == "macos" else os_code.capitalize())
    emoji = "🍎" if platform in ("iOS", "macOS") else ("🖥" if platform == "Windows" else ("🤖" if platform == "Android" else ""))

    # дневная цена из MONTHLY_FEE (округляем ≈2 ₽/день)
    daily_rub = max(1, round(MONTHLY_FEE / 30))

    text = (
        "Подтвердите подключение\n\n"
        f"Устройство: {platform}{(' ' + emoji) if emoji else ''}\n"
        f"Тариф: {daily_rub}₽/день"
    )

    kb = first_buy_kb(afford, MONTHLY_FEE)
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.callback_data == "buy":
                btn.callback_data = f"confirm_buy:{os_code}"

    await safe_edit(cq.message, text, reply_markup=kb)
    await safe_answer(cq)



@router.callback_query(F.data.startswith("confirm_buy:"))
async def buy_create(cq: types.CallbackQuery):
    os_code = cq.data.split(":", 1)[1]
    devices = await _user_devices(cq.from_user.id)

    if len(devices) >= MAX_DEVICES_PER_USER:
        await safe_edit(
            cq.message,
            (
                "📱 <b>Лимит устройств достигнут</b>\n\n"
                f"Можно подключить не более {MAX_DEVICES_PER_USER} устройств.\n"
                "Удалите одно из существующих, чтобы добавить новое."
            ),
            parse_mode="HTML",
            reply_markup=back_kb()
        )
        await safe_answer(cq)
        return

    created = await api.create_user(f"tg_{cq.from_user.id}", DEFAULT_DAYS)
    logging.info(f"[buy_create] API create_user -> {created}")

    if not isinstance(created, dict) or created.get("_error"):
        await safe_edit(cq.message, f"❌ Не удалось выдать ключ. {created.get('_error','')}", reply_markup=back_kb())
        await safe_answer(cq)
        return

    sub_link   = created.get("sub_link")
    sub_id     = (created.get("sub_id") or "").strip()
    uuid_      = (created.get("uuid") or "").strip()
    expires_at = (created.get("expires_at") or "").strip()
    server_base = (created.get("_server") or "").strip()

    if not sub_link or not uuid_:
        await safe_edit(cq.message, "❌ Сервер не вернул ссылку/UUID.", reply_markup=back_kb())
        await safe_answer(cq)
        return

    name = _next_device_name(os_code, devices, uuid_)

    logging.info(f"[buy_create] Creating device name={name} for uuid={uuid_}")

    try:
        db.add_device(
            tg_id=cq.from_user.id,
            uuid=uuid_,
            name=name,
            os=("iOS" if os_code.lower() == "ios" else os_code.capitalize()),
            status="active",
            expires_at=expires_at,
            sub_id=(sub_id or None),
            server_base=(server_base or None)
        )
    except ValueError as e:
        if str(e) == "device_limit_reached":
            await safe_edit(
                cq.message,
                (
                    "📱 <b>Лимит устройств достигнут</b>\n\n"
                    f"Можно подключить не более {MAX_DEVICES_PER_USER} устройств.\n"
                    "Удалите одно из существующих, чтобы добавить новое."
                ),
                parse_mode="HTML",
                reply_markup=back_kb()
            )
            await safe_answer(cq)
            return
        logging.exception(f"[buy_create] DB add_device failed: {e}")
        await safe_edit(cq.message, "❌ Не удалось выдать ключ. Попробуйте позже.", reply_markup=back_kb())
        await safe_answer(cq)
        return

    ident = sub_id or uuid_
    sub = f"{API_URL.rstrip('/')}/sub/{ident}?b64=1"
    text = os_instruction(os_code) + f"\n\n<b>Ваша ссылка:</b>\n<code>{sub}</code>"
    done_kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Готово!", callback_data="home")]
        ]
    )
    await safe_edit(cq.message, text, parse_mode="HTML", reply_markup=done_kb)
    await safe_answer(cq)



@router.callback_query(F.data == "devices")
async def devices_list(cq: types.CallbackQuery):
    try:
        devices = await _user_devices(cq.from_user.id)
    except Exception:
        devices = []

    kb = devices_list_kb(devices)

    limit = MAX_DEVICES_PER_USER
    reached = len(devices) >= limit

    if devices:
        text = (
            "📱 <b>Ваши устройства</b>\n\n"
            "Выберите устройство из списка ниже или добавьте новое.\n\n"
            f"👥 Можно подключить до {limit} устройств."
            + ("\n⚠️ Лимит достигнут. Удалите устройство, чтобы добавить новое." if reached else "")
            + "\n\n💳 Тариф — 60 ₽ в месяц за каждое устройство.\n"
            "⚠️ Если VPN работает неправильно, обновите настройки для выбранного устройства."
        )
    else:
        text = (
            "📱 <b>Ваши устройства</b>\n\n"
            "У вас пока нет устройств.\n"
            "Нажмите «Добавить устройство», чтобы подключиться.\n\n"
            f"👥 Можно подключить до {limit} устройств.\n"
            "💳 Тариф — 60 ₽ в месяц за каждое устройство."
        )

    await safe_edit(cq.message, text, parse_mode="HTML", reply_markup=kb)
    await safe_answer(cq)



@router.callback_query(F.data.regexp(r"^dev:.+?:open$"))
async def dev_open(cq: types.CallbackQuery):
    _, dev_id, _ = cq.data.split(":", 2)
    d = _find_device_local(dev_id, cq.from_user.id)
    if not d:
        await safe_answer(cq, "Устройство не найдено", show_alert=True)
        return

    name   = (d.get("name") or d.get("label") or "Устройство").strip()
    status = (d.get("status") or "—").strip()

    uuid_  = (d.get("uuid") or "").strip()
    sub_id = (d.get("sub_id") or "").strip()
    ident  = (sub_id or uuid_).strip()

    if not ident:
        await safe_edit(
            cq.message,
            "⚠️ У этого устройства нет идентификатора, ссылка недоступна.",
            reply_markup=devices_list_kb(await _user_devices(cq.from_user.id))
        )
        await safe_answer(cq)
        return

    sub = f"{API_URL.rstrip('/')}/sub/{ident}?b64=1"

    text = (
        f"Устройство: <b>{name}</b>\n"
        f"Статус: <b>{status}</b>\n\n"
        f"<b>Ссылка для подключения:</b>\n<code>{sub}</code>\n\n"
        "Удалить устройство можно не ранее, чем через 24 часа после его добавления.\n"
        "Если VPN перестал работать — нажмите «Обновить». "
        "Ключ обновится автоматически, ничего менять не нужно. "
        "После обновления переподключитесь к VPN."
    )

    actual_id = str(uuid_ or d.get("id") or d.get("name") or dev_id)
    # используем activated_at или created_at (что есть)
    created_or_activated_at = int(d.get("activated_at") or d.get("created_at") or 0)

    await safe_edit(
        cq.message,
        text,
        parse_mode="HTML",
        reply_markup=device_actions_basic_kb(actual_id, created_or_activated_at)
    )
    await safe_answer(cq)



@router.callback_query(F.data.regexp(r"^dev:.+?:key$"))
async def dev_key(cq: types.CallbackQuery):
    _, dev_id, _ = cq.data.split(":", 2)
    d = _find_device_local(dev_id, cq.from_user.id)
    if not d:
        await safe_answer(cq, "Устройство не найдено", show_alert=True)
        return

    uuid_  = (d.get("uuid") or "").strip()
    sub_id = (d.get("sub_id") or "").strip()
    if not uuid_ and not sub_id:
        await safe_edit(
            cq.message,
            "⚠️ У этого устройства не найден идентификатор.",
            reply_markup=devices_list_kb(await _user_devices(cq.from_user.id))
        )
        await safe_answer(cq)
        return

    ident = sub_id or uuid_
    sub = f"{API_URL.rstrip('/')}/sub/{ident}?b64=1"
    text = os_instruction((d.get("os") or "").lower()) + f"\n\n<b>Ваша ссылка:</b>\n<code>{sub}</code>"

    await safe_edit(
        cq.message,
        text,
        parse_mode="HTML",
        reply_markup=key_actions_kb(uuid_ or ident)
    )
    await safe_answer(cq)


@router.callback_query(F.data.regexp(r"^dev:.+?:refresh$"))
async def dev_refresh(cq: types.CallbackQuery):
    _, dev_id, _ = cq.data.split(":", 2)
    d = _find_device_local(dev_id, cq.from_user.id)
    if not d:
        await safe_answer(cq, "Устройство не найдено", show_alert=True)
        return

    uuid_cur = (d.get("uuid") or "").strip()
    sub_id   = (d.get("sub_id") or "").strip()


    key = (cq.from_user.id, str(d.get("uuid") or d.get("id") or dev_id))
    now = time.time()
    ready_until = _REFRESH_READY.get(key, 0)
    if now > ready_until:
        _REFRESH_READY[key] = now + 90  # окно подтверждения 90 сек
        await safe_answer(
            cq,
            "⚠️ Перед обновлением ключа отключите VPN в приложении.\n\n"
            "Когда отключите — нажмите «Обновить конфиг» ещё раз.",
            show_alert=True
        )
        return


    if not sub_id and uuid_cur:
        try:
            sub_found = await api.resolve_sub_id_from_uuid(uuid_cur)
        except Exception:
            sub_found = None
        if sub_found:
            sub_id = sub_found
            try:
                with db.db() as con:
                    con.execute("UPDATE devices SET sub_id=? WHERE id=?", (sub_id, d["id"]))
                    con.commit()
            except Exception:
                pass

    if not sub_id:
        _REFRESH_READY.pop(key, None)
        await safe_answer(cq, "sub_id не найден для этого устройства.", show_alert=True)
        return

    resp = await api.refresh_by_sub_id(sub_id)
    if isinstance(resp, dict) and resp.get("_error"):
        _REFRESH_READY.pop(key, None)
        await safe_answer(cq, f"Не удалось обновить: {resp['_error']}", show_alert=True)
        return

    new_uuid = ""
    if isinstance(resp, dict):
        new_uuid = (resp.get("uuid") or "").strip()

    if new_uuid and new_uuid != uuid_cur:
        try:
            with db.db() as con:
                con.execute("UPDATE devices SET uuid=? WHERE id=?", (new_uuid, d["id"]))
                con.execute(
                    "INSERT INTO events(tg_id, type, payload, created_at) VALUES(?,?,?,?)",
                    (cq.from_user.id, "refresh", f"old={uuid_cur} new={new_uuid}", int(time.time()))
                )
                con.commit()
        except Exception as e:
            logging.exception(f"[dev_refresh] DB write failed: {e}")


    _REFRESH_READY.pop(key, None)

    d = db.device_by_id(d["id"]) or d
    name   = (d.get("name") or d.get("label") or "Устройство").strip()
    status = (d.get("status") or "—").strip()
    uuid_now = (d.get("uuid") or uuid_cur or "").strip()
    sub_id   = (d.get("sub_id") or "").strip()
    ident    = (sub_id or uuid_now).strip()
    sub_link = f"{API_URL.rstrip('/')}/sub/{ident}?b64=1"

    text = (
        f"Устройство: <b>{name}</b>\n"
        f"Статус: <b>{status}</b>\n\n"
        f"<b>Ссылка для подключения:</b>\n<code>{sub_link}</code>\n\n"
        "Удалить устройство можно не ранее, чем через 24 часа после его добавления.\n"
        "Если VPN перестал работать — нажмите «Обновить». "
        "Ключ обновится автоматически, ничего менять не нужно. "
        "После обновления переподключитесь к VPN."
    )

    actual_id = str(uuid_now or d.get("id") or d.get("name") or dev_id)
    created_or_activated_at = int(d.get("activated_at") or d.get("created_at") or 0)

    await safe_answer(cq, "✅ Конфиг обновлён! Обновите VPN в приложении.", show_alert=True)
    await safe_edit(
        cq.message,
        text,
        parse_mode="HTML",
        reply_markup=device_actions_basic_kb(actual_id, created_or_activated_at)
    )



@router.callback_query(F.data.regexp(r"^dev:.+?:delete$"))
async def dev_delete(cq: types.CallbackQuery):
    _, dev_id, _ = cq.data.split(":", 2)
    d = _find_device_local(dev_id, cq.from_user.id)
    if not d:
        await safe_answer(cq, "Устройство уже удалено", show_alert=True)
        return
    uuid_ = (d.get("uuid") or "").strip()
    try:
        await api.revoke(uuid_)
    except Exception:
        pass
    db.set_device_status(uuid_, "deleted")
    devices = await _user_devices(cq.from_user.id)
    await safe_edit(
        cq.message,
        "Мои устройства:" if devices else "У вас пока нет устройств.",
        reply_markup=devices_list_kb(devices)
    )
    await safe_answer(cq)
