"""JSON state store: domains + their Cloudflare credentials.

File lives on a mounted volume (DATA_DIR/state.json), survives restarts.
Only written via the bot chat flows — never commit it (see .gitignore).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

_lock = asyncio.Lock()


def state_path() -> str:
    if os.environ.get("STATE_FILE"):
        return os.environ["STATE_FILE"]
    return os.path.join(os.environ.get("DATA_DIR", "data"), "state.json")


def load() -> dict:
    path = state_path()
    if not os.path.exists(path):
        return {"domains": {}, "active": {}}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("domains", {})
    data.setdefault("active", {})
    return data


async def _save(data: dict) -> None:
    path = state_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    async with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


async def add_domain(domain: str, zone_id: str, account_id: str, api_token: str) -> None:
    data = load()
    data["domains"][domain] = {
        "zone_id": zone_id,
        "account_id": account_id,
        "api_token": api_token,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    await _save(data)


async def remove_domain(domain: str) -> bool:
    data = load()
    if domain not in data["domains"]:
        return False
    del data["domains"][domain]
    await _save(data)
    return True


def get_domain(domain: str) -> dict | None:
    return load()["domains"].get(domain)


def list_domains() -> dict:
    return load()["domains"]


async def set_active(chat_id: int | str, domain: str) -> None:
    data = load()
    data["active"][str(chat_id)] = domain
    await _save(data)


def get_active(chat_id: int | str) -> str | None:
    data = load()
    domain = data["active"].get(str(chat_id))
    return domain if domain in data["domains"] else None


async def clear_active_domain(domain: str) -> None:
    data = load()
    data["active"] = {c: d for c, d in data["active"].items() if d != domain}
    await _save(data)
