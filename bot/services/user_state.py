from bot.services import api

async def is_first_time(tg_id: int) -> bool:

    users = await api.list_users()
    if isinstance(users, list):
        name = f"tg_{tg_id}"
        return all(u.get("name") != name for u in users)
    return False