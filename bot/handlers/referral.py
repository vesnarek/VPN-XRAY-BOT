import asyncio
import logging
from typing import Sequence, Tuple, Any, Dict, Optional
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError
from bot.services import db

from aiogram import Router
router = Router()
log = logging.getLogger(__name__)

async def run_referral_notifier(bot: Bot, poll_interval: float = 2.0):
    last_id = 0
    try:
        with db.db() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(id),0) FROM payments WHERE method='referral'"
            ).fetchone()
            last_id = int(row[0] or 0)
    except Exception as e:
        log.warning("[referral_notifier] init last_id failed: %s", e)

    log.info("[referral_notifier] start from payment id > %s", last_id)

    backoff = 1.0
    while True:
        try:
            rows: Sequence[Dict[str, Any]]
            with db.db() as con:
                rows = con.execute(
                    """
                    SELECT id, tg_id, amount_cents, ref, created_at
                    FROM payments
                    WHERE method='referral' AND id > ?
                    ORDER BY id ASC
                    LIMIT 200
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

                text = (
                    "🎁 <b>Бонус за приглашение</b>\n\n"
                    f"На ваш баланс зачислено <b>{rub} ₽</b>."
                )

                try:
                    await bot.send_message(tg_id, text, parse_mode="HTML")

                    last_id = pid

                except TelegramRetryAfter as e:

                    delay = max(1, int(getattr(e, "retry_after", 5)))
                    log.warning("[referral_notifier] 429, sleep %ss", delay)
                    await asyncio.sleep(delay)

                    break

                except TelegramForbiddenError as e:

                    log.warning("[referral_notifier] 403 for user %s: %s", tg_id, e)
                    last_id = pid

                except TelegramAPIError as e:

                    log.error("[referral_notifier] TelegramAPIError: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    break

                except Exception as e:
                    log.exception("[referral_notifier] send error for pid=%s: %s", pid, e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    break

        except Exception as e:
            log.exception("[referral_notifier] loop error: %s", e)
            await asyncio.sleep(2.0)
            backoff = min(backoff * 2, 60)
