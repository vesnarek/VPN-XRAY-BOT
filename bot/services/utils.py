def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default
