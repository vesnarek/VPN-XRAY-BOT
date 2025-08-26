import os, json, logging, sqlite3
from decimal import Decimal
from fastapi import FastAPI, Request, Response
from yookassa import Configuration, Payment

# === ENV ===
YKASSA_ACCOUNT_ID  = os.getenv("YKASSA_ACCOUNT_ID", "").strip()
YKASSA_SECRET_KEY  = os.getenv("YKASSA_SECRET_KEY", "").strip()
TEST_MODE          = os.getenv("YKASSA_TEST_MODE", "0") == "1"


from bot.services import db as dbsvc

# Конфигурация YooKassa
if YKASSA_ACCOUNT_ID and YKASSA_SECRET_KEY:
    Configuration.account_id = YKASSA_ACCOUNT_ID
    Configuration.secret_key = YKASSA_SECRET_KEY

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True, "service": "yookassa-webhook"}

@app.post("/yookassa/webhook")
@app.post("/payhook")
async def yookassa_webhook(req: Request):
    try:
        body = await req.body()
        data = json.loads((body or b"{}").decode("utf-8"))
    except Exception:
        return Response(status_code=400)

    if data.get("event") != "payment.succeeded":
        return {"ok": True}

    obj = data.get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return {"ok": True}

    # Подтверждаем платёж через API (в TEST_MODE допустим мок)
    if TEST_MODE and str(payment_id).startswith("test_"):
        class P:
            status = "succeeded"
            amount = type("A", (), {"value": obj.get("amount", {}).get("value", "1.00")})
            metadata = obj.get("metadata", {}) or {}
        pay = P()
    else:
        try:
            pay = Payment.find_one(payment_id)
        except Exception as e:
            logging.warning(f"[yk] find_one failed {payment_id}: {e}")
            return {"ok": True}

    if not pay or getattr(pay, "status", "") != "succeeded":
        return {"ok": True}

    meta = dict(getattr(pay, "metadata", {}) or {})
    tg_id = int(meta.get("tg_id") or 0)
    if tg_id <= 0:
        logging.warning(f"[yk] no tg_id in metadata for {payment_id}")
        return {"ok": True}

    try:
        amount_cents = int((Decimal(str(pay.amount.value)) * 100).quantize(Decimal("1")))
    except Exception:
        amount_cents = 0
    if amount_cents <= 0:
        logging.warning(f"[yk] non-positive amount for {payment_id}")
        return {"ok": True}

    try:
        with dbsvc.db() as con:
            con.execute(
                "INSERT OR IGNORE INTO users(tg_id, created_at, balance_cents) VALUES(?,?,?)",
                (tg_id, dbsvc.now(), 0)
            )
        dbsvc.add_balance(tg_id, amount_cents, method="card", ref=payment_id)
    except sqlite3.IntegrityError:
        # уже зачислено ранее
        pass

    return {"ok": True}
