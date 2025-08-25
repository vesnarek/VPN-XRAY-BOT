import uuid
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from yookassa import Configuration, Payment
from bot.settings import YKASSA_ACCOUNT_ID, YKASSA_SECRET_KEY, YKASSA_RETURN_URL


Configuration.account_id = YKASSA_ACCOUNT_ID
Configuration.secret_key = YKASSA_SECRET_KEY


def _add_query_param(url: str, **params) -> str:

    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))


def create_payment_link(tg_id: int, amount_rub: int, description: Optional[str] = None) -> dict:

    try:

        base_desc = description or f"Пополнение баланса TG {tg_id} на {amount_rub} ₽"
        desc = (base_desc[:125] + "...") if len(base_desc) > 128 else base_desc


        val = f"{float(amount_rub):.2f}"


        return_url = _add_query_param(YKASSA_RETURN_URL, tg_id=tg_id)


        order_id = str(uuid.uuid4())

        body = {
            "amount": {"value": val, "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "capture": True,
            "description": desc,
            "metadata": {
                "tg_id": str(tg_id),
                "amount_rub": str(amount_rub),
                "order_id": order_id,
                "source": "vpn_bot",
            },

        }


        idem_key = str(uuid.uuid4())
        p = Payment.create(body, idem_key)

        url = getattr(getattr(p, "confirmation", None), "confirmation_url", None)
        pid = getattr(p, "id", None)
        if not url or not pid:
            return {"ok": False, "error": "no confirmation_url or payment_id from YooKassa"}

        return {"ok": True, "payment_id": pid, "url": url, "order_id": order_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
