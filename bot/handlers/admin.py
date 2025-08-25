import asyncio
import secrets
import sqlite3
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.settings import ADMINS
from bot.services import db
from bot.services import api

router = Router()

_AWAITING_BROADCAST: set[int] = set()


def admin_only(user_id: int) -> bool:
    return user_id in ADMINS


def human_bytes(n: int) -> str:
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    f = float(max(0, int(n or 0)))
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.2f} {units[i]}"


def human_user_label(tg_id: int, username: str | None) -> str:
    if username:
        u = username.strip().lstrip("@")
        return f"@{u}"
    return f'<a href="tg://user?id={tg_id}">{tg_id}</a>'


def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin:genpromo")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🏠 В обычное меню", callback_data="home")],
    ])


def kb_back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu")]
    ])


def kb_users_nav(offset: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    page = offset // 20 + 1
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:users:{max(0, offset-20)}"))
    nav.append(InlineKeyboardButton(text=f"Стр. {page}", callback_data=f"admin:users:{offset}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️ Далее", callback_data=f"admin:users:{offset+20}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_broadcast_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="admin:broadcast:cancel")],
        [InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu")],
    ])


def render_admin_header() -> str:
    with db.db() as con:
        u = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 0
        d = con.execute("SELECT COUNT(*) FROM devices WHERE status!='deleted'").fetchone()[0] or 0
        bal = con.execute("SELECT COALESCE(SUM(balance_cents),0) FROM users").fetchone()[0] or 0
    return (
        "👮 <b>Админ-панель</b>\n\n"
        f"👤 Юзеров: <b>{u}</b>\n"
        f"📱 Устройств: <b>{d}</b>\n"
        f"💰 Баланс суммарный: <b>{bal//100} ₽</b>"
    )


@router.message(Command("admin"))
async def admin_root(message: types.Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer(render_admin_header(), parse_mode="HTML", reply_markup=kb_admin_menu())


@router.callback_query(F.data == "admin_menu")
async def admin_menu(cq: types.CallbackQuery):
    if not admin_only(cq.from_user.id):
        await cq.answer()
        return
    _AWAITING_BROADCAST.discard(cq.from_user.id)
    text = render_admin_header()
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_menu())
    except TelegramBadRequest:
        try:
            await cq.message.edit_reply_markup(reply_markup=kb_admin_menu())
        except Exception:
            pass
    await cq.answer()

@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users_btn(cq: types.CallbackQuery):
    if not admin_only(cq.from_user.id):
        await cq.answer()
        return


    try:
        offset = int((cq.data or "admin:users:0").split(":")[2])
    except Exception:
        offset = 0
    limit = 20


    with db.db() as con:
        rows = con.execute("""
            SELECT
                u.tg_id,
                COALESCE(u.username, '') AS username,
                u.balance_cents,
                (SELECT COUNT(*) FROM devices d
                  WHERE d.tg_id = u.tg_id AND d.status != 'deleted') AS devs
            FROM users u
            ORDER BY u.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit + 1, offset)).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]


    traffic_map: dict[int, tuple[int, int]] = {}
    mgr = await api.list_users()
    if isinstance(mgr, list):
        for u in mgr:
            name = (u.get("name") or "").strip()
            if name.startswith("tg_"):
                try:
                    tid = int(name[3:])
                except Exception:
                    continue
                up = int(u.get("upload_bytes") or 0)
                dn = int(u.get("download_bytes") or 0)
                traffic_map[tid] = (up, dn)

    lines: list[str] = []


    for r in rows:
        tg_id = int(r["tg_id"])
        username = (r["username"] or "").strip() or None
        label = human_user_label(tg_id, username)

        bal_rub = int(r["balance_cents"] or 0) // 100
        devs_cnt = int(r["devs"] or 0)


        up = dn = 0
        try:
            with db.db() as con2:
                devs = con2.execute(
                    "SELECT sub_id, uuid FROM devices "
                    "WHERE tg_id = ? AND status != 'deleted'",
                    (tg_id,)
                ).fetchall()

            for d in devs or []:
                sub_id = (d["sub_id"] or "").strip()
                uuid_  = (d["uuid"]  or "").strip()
                ident  = sub_id or uuid_
                if ident:
                    u, v = await api.fetch_live_traffic_by_ident(ident)  # (upload, download)
                    up += int(u or 0)
                    dn += int(v or 0)
        except Exception:

            pass


        if (up + dn) == 0:
            tup = traffic_map.get(tg_id)
            if tup:
                up, dn = tup

        total = up + dn
        traffic_str = (
            f" {human_bytes(total)} (↑{human_bytes(up)} / ↓{human_bytes(dn)})"
            if total else " 0 Б"
        )

        lines.append(f"{label}: <b>{bal_rub} ₽</b> · устройств: {devs_cnt} ·{traffic_str}")

    text = (
        "👥 <b>Пользователи</b>\n"
        "(последние по регистрации)\n\n" +
        ("\n".join(lines) if lines else "пусто")
    )

    kb = kb_users_nav(offset, has_more)
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        try:
            await cq.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    await cq.answer()



@router.callback_query(F.data == "admin:genpromo")
async def admin_genpromo_prompt(cq: types.CallbackQuery):
    if not admin_only(cq.from_user.id):
        await cq.answer()
        return
    txt = (
        "Отправьте сообщением один из форматов:\n"
        "• <b>сумма</b> <b>использований</b> — код сгенерим сами\n"
        "• <b>КОД</b> <b>сумма</b> <b>использований</b> — свой промокод\n\n"
        "Примеры:\n"
        "<code>60 10</code>\n"
        "<code>BACK2SCHOOL 100 50</code>"
    )
    try:
        await cq.message.edit_text(txt, parse_mode="HTML", reply_markup=kb_back_admin())
    except TelegramBadRequest:
        try:
            await cq.message.edit_reply_markup(reply_markup=kb_back_admin())
        except Exception:
            pass
    await cq.answer()


@router.message(Command("genpromo"))
async def genpromo_cmd(message: types.Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return
    if not command.args:
        await message.answer("Использование: /genpromo <сумма_руб> <использований> [КОД]")
        return

    parts = command.args.split()
    try:
        if len(parts) == 2:
            amount_rub = int(parts[0]); uses = int(parts[1])
            code = secrets.token_urlsafe(10)
        elif len(parts) >= 3:
            code = parts[2] if len(parts) == 3 else parts[0]
            if len(parts) == 3:
                amount_rub = int(parts[0]); uses = int(parts[1])
            else:
                amount_rub = int(parts[1]); uses = int(parts[2])
        else:
            raise ValueError
        assert amount_rub > 0 and uses > 0
        assert 3 <= len(code) <= 64

        import re
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", code), "Недопустимые символы в коде"
    except Exception as e:
        await message.answer(f"Неверные аргументы: {e}\nПример: /genpromo 60 10  или  /genpromo 100 5 BACK2SCHOOL")
        return

    try:
        with db.db() as con:
            con.execute(
                "INSERT INTO promos(code, amount_cents, uses_left) VALUES(?,?,?)",
                (code, amount_rub * 100, uses)
            )
        await message.answer(
            f"✅ Промокод создан\nКод: <code>{code}</code>\nНоминал: {amount_rub} ₽\nИспользований: {uses}",
            parse_mode="HTML"
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ Такой код уже существует. Выберите другой.")
    except Exception as e:
        await message.answer(f"❌ Ошибка БД: {e}")


@router.message(F.text.regexp(r"^\s*\S{3,64}\s+\d+\s+\d+\s*$"))
async def genpromo_with_code(message: types.Message):
    if not admin_only(message.from_user.id):
        return
    parts = message.text.strip().split()
    code = parts[0]
    try:
        amount_rub = int(parts[1]); uses = int(parts[2])
        assert amount_rub > 0 and uses > 0
        import re
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", code), "Недопустимые символы в коде"
    except Exception as e:
        await message.answer(f"Неверный формат: {e}\nПример: BACK2SCHOOL 100 50")
        return

    try:
        with db.db() as con:
            con.execute(
                "INSERT INTO promos(code, amount_cents, uses_left) VALUES(?,?,?)",
                (code, amount_rub * 100, uses)
            )
        await message.answer(
            f"✅ Промокод создан\nКод: <code>{code}</code>\nНоминал: {amount_rub} ₽\nИспользований: {uses}",
            parse_mode="HTML"
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ Такой код уже существует. Выберите другой.")
    except Exception as e:
        await message.answer(f"❌ Ошибка БД: {e}")


@router.message(F.text.regexp(r"^\s*\d+\s+\d+\s*$"))
async def genpromo_plain(message: types.Message):
    if not admin_only(message.from_user.id):
        return
    try:
        amount_rub_str, uses_str = message.text.split()
        amount_rub = int(amount_rub_str)
        uses = int(uses_str)
        assert amount_rub > 0 and uses > 0
    except Exception:
        await message.answer("Нужны два положительных числа. Пример: 60 10")
        return

    code = secrets.token_urlsafe(10)
    try:
        with db.db() as con:
            con.execute(
                "INSERT INTO promos(code, amount_cents, uses_left) VALUES(?,?,?)",
                (code, amount_rub * 100, uses)
            )
        await message.answer(
            f"✅ Промокод создан\nКод: <code>{code}</code>\nНоминал: {amount_rub} ₽\nИспользований: {uses}",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка БД: {e}")


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_prompt(cq: types.CallbackQuery):
    if not admin_only(cq.from_user.id):
        await cq.answer()
        return
    _AWAITING_BROADCAST.add(cq.from_user.id)
    txt = (
        "📣 Режим рассылки.\n\n"
        "Пришли текст одним сообщением, я отправлю всем пользователям.\n"
        "Чтобы отменить — нажми «✖️ Отмена»."
    )
    try:
        await cq.message.edit_text(txt, parse_mode="HTML", reply_markup=kb_broadcast_controls())
    except TelegramBadRequest:
        try:
            await cq.message.edit_reply_markup(reply_markup=kb_broadcast_controls())
        except Exception:
            pass
    await cq.answer()


@router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(cq: types.CallbackQuery):
    if not admin_only(cq.from_user.id):
        await cq.answer()
        return
    _AWAITING_BROADCAST.discard(cq.from_user.id)
    try:
        await cq.message.edit_text("Отменено.", reply_markup=kb_admin_menu())
    except TelegramBadRequest:
        try:
            await cq.message.edit_reply_markup(reply_markup=kb_admin_menu())
        except Exception:
            pass
    await cq.answer()


@router.message(F.text)
async def admin_broadcast_catcher(message: types.Message):
    uid = message.from_user.id
    if not admin_only(uid) or uid not in _AWAITING_BROADCAST:
        return

    text = message.html_text or message.text or ""
    if not text.strip():
        await message.answer("Пустое сообщение, ничего не отправил.")
        return

    with db.db() as con:
        rows = con.execute("SELECT DISTINCT tg_id FROM users").fetchall()
    recipients = [int(r[0]) for r in rows if r and r[0]]

    total = len(recipients)
    sent = 0
    failed = 0

    for rid in recipients:
        try:
            await message.bot.send_message(rid, text, parse_mode="HTML", disable_web_page_preview=False)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    _AWAITING_BROADCAST.discard(uid)
    await message.answer(f"Готово. Отправлено {sent}/{total}, ошибок: {failed}", reply_markup=kb_admin_menu())




