"""Entrypoint: `python -m bot`. Long-polling Telegram bot."""
from __future__ import annotations

import logging
import os
import sys

from .handlers import build_application

log = logging.getLogger("cfhost")


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("BOT_TOKEN is not set — copy .env.example to .env and fill it in.", file=sys.stderr)
        raise SystemExit(2)
    if not os.environ.get("ALLOWED_CHAT_IDS", "").strip():
        log.warning("ALLOWED_CHAT_IDS is empty — the bot will reply to anyone.")
    app = build_application(token)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
