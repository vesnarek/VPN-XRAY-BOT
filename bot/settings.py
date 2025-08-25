from dotenv import load_dotenv
import os

load_dotenv()

def _getenv(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v

BOT_TOKEN      = _getenv("BOT_TOKEN")
API_URL        = _getenv("API_URL", "http://127.0.0.1:8081")
API_URL_2      = _getenv("API_URL_2", "")
LOAD_THRESH    = int(_getenv("LOAD_THRESH", "75"))
API1_CAP       = int(_getenv("API1_CAP", "200"))
API2_CAP       = int(_getenv("API2_CAP", "200"))

ADMINS = {
    int(x) for x in _getenv("ADMINS", "").replace(" ", "").split(",") if x
}

DEFAULT_DAYS   = int(_getenv("DEFAULT_DAYS", "30"))
MONTHLY_FEE    = int(_getenv("MONTHLY_FEE", "60"))
MONTHLY_FEE_C  = MONTHLY_FEE * 100

DAILY_FEE_C    = max(100, round(MONTHLY_FEE_C / 30))

SUPPORT_USER   = _getenv("SUPPORT_USER", "support")

YKASSA_ACCOUNT_ID     = _getenv("YKASSA_ACCOUNT_ID", "")
YKASSA_SECRET_KEY     = _getenv("YKASSA_SECRET_KEY", "")
YKASSA_RETURN_URL     = _getenv("YKASSA_RETURN_URL", "")
YKASSA_WEBHOOK_SECRET = _getenv("YKASSA_WEBHOOK_SECRET", "")

TOPUP_RUB = 60

def user_name(tg_id: int) -> str:
    return f"tg_{tg_id}"
