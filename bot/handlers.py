"""Telegram handlers: /manage_domain (domains) + /menu (hostnames on active domain)."""
from __future__ import annotations

import asyncio
import logging
import os

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import store
from .cf import CFApiError, CloudflareClient

(MANAGE, ASK_DOMAIN_NAME, ASK_ACCOUNT_ID, ASK_TOKEN) = range(4)
(SELECT_DEL_DOMAIN, CONFIRM_DEL_DOMAIN) = range(4, 6)
SELECT_ACTIVE_DOMAIN = 6
(MENU, ASK_HOSTNAME, ASK_CNAME) = range(7, 10)
SELECT_STATUS_HOST = 10

POLL_TRIES = 12
POLL_DELAY = 10

# Fallback-origin subdomain, per spec: `fallback.<domain>` aliasing the apex.
FALLBACK_SUB = "fallback"

log = logging.getLogger("cfhost")

COMMANDS = [
    BotCommand("start", "Manage domains"),
    BotCommand("manage_domain", "Add / delete / switch domain"),
    BotCommand("menu", "Add hostname / status"),
    BotCommand("cancel", "Back to menu"),
]


def allowed_ids() -> set[int]:
    ids: set[int] = set()
    for part in os.environ.get("ALLOWED_CHAT_IDS", "").replace(",", " ").split():
        try:
            ids.add(int(part))
        except ValueError:
            pass
    return ids


def mask(token: str) -> str:
    return "…" + token[-4:] if len(token) > 4 else "…"


def status_icon(status: str, ssl_status: str) -> str:
    host_ok = status == "active"
    cert_ok = ssl_status == "active"
    if host_ok and cert_ok:
        return "🟢"
    if host_ok or cert_ok:
        return "🟠"
    return "🔴"


async def guard(update: Update) -> bool:
    ids = allowed_ids()
    chat = update.effective_chat
    if ids and chat and chat.id not in ids:
        if update.callback_query:
            await update.callback_query.answer()
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Unauthorized.")
        return False
    return True


def manage_markup(has_domains: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Add domain", callback_data="add_domain")]]
    if has_domains:
        rows.append([InlineKeyboardButton("➖ Delete domain", callback_data="del_domain")])
        rows.append([InlineKeyboardButton("🔀 Set/Switch domain", callback_data="set_domain")])
    return InlineKeyboardMarkup(rows)


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add Custom Hostname", callback_data="add_host")],
            [InlineKeyboardButton("📊 Status", callback_data="status")],
            [InlineKeyboardButton("⬅️ Back to Manage Domains", callback_data="manage")],
        ]
    )


def back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_op")],
        ]
    )


def cname_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes", callback_data="cname_yes")],
            [InlineKeyboardButton("❌ No", callback_data="cname_no")],
        ]
    )


async def _show(update: Update, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def show_manage(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str | None = None) -> int:
    if not await guard(update):
        return ConversationHandler.END
    context.user_data.clear()
    domains = sorted(store.list_domains())
    active = store.get_active(update.effective_chat.id)
    lines = ["🗂 Manage domains"]
    if note:
        lines.append(note)
    lines.append(f"Active: `{active}`" if active else "Active: none — add a domain, then Set/Switch to use it.")
    if domains:
        lines.append("")
        lines.append("Domains:\n" + "\n".join(
            f"• `{d}`" + (" ✅" if d == active else "") for d in domains))
    await _show(update, "\n".join(lines), manage_markup(bool(domains)))
    return MANAGE


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    context.user_data.clear()
    active = store.get_active(update.effective_chat.id)
    if not active:
        return await show_manage(update, context, note="Set a domain first:")
    await _show(update, f"📋 Menu — active domain `{active}`:", menu_markup())
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await show_menu(update, context)


# --- Add Domain (name -> account id -> token, validated via zone lookup) ---


async def add_domain_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "Send the domain name (e.g. `example.com`):", parse_mode="Markdown", reply_markup=back_markup()
    )
    return ASK_DOMAIN_NAME


async def recv_domain_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    domain = update.message.text.strip().lower().rstrip(".")
    if not domain or "." not in domain or " " in domain:
        await update.message.reply_text("That doesn't look like a domain. Try again:")
        return ASK_DOMAIN_NAME
    context.user_data["new_domain"] = domain
    await update.message.reply_text("Send the Cloudflare **account ID** for this domain:", parse_mode="Markdown")
    return ASK_ACCOUNT_ID


async def recv_account_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    context.user_data["new_account_id"] = update.message.text.strip()
    await update.message.reply_text(
        "Send the Cloudflare **API token** (needs Zone Read + DNS Edit + SSL Edit):\n"
        "_It is stored only in the server state file, never in the repo._",
        parse_mode="Markdown",
    )
    return ASK_TOKEN


async def recv_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    domain = context.user_data["new_domain"]
    account_id = context.user_data["new_account_id"]
    token = update.message.text.strip()
    await update.message.reply_text(f"Validating `{domain}` against Cloudflare…", parse_mode="Markdown")
    try:
        async with CloudflareClient(token) as client:
            zone_id = await client.resolve_zone_id(domain)
    except CFApiError as exc:
        await update.message.reply_text(f"❌ {exc}", reply_markup=manage_markup(True))
        return MANAGE
    await store.add_domain(domain, zone_id, account_id, token)
    if store.get_active(update.effective_chat.id) is None:
        await store.set_active(update.effective_chat.id, domain)
    return await show_manage(
        update, context, note=f"✅ Domain `{domain}` added (zone `{zone_id}`, token `{mask(token)}`).")


# --- Delete Domain (pick -> confirm; clears active usage) ---


def _domain_buttons(prefix: str) -> InlineKeyboardMarkup | None:
    domains = sorted(store.list_domains())
    if not domains:
        return None
    rows = [[InlineKeyboardButton(d, callback_data=f"{prefix}:{d}")] for d in domains]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op")])
    return InlineKeyboardMarkup(rows)


async def del_domain_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    markup = _domain_buttons("deldom")
    if markup is None:
        return await show_manage(update, context, note="No domains added yet.")
    await update.callback_query.edit_message_text("Choose a domain to delete:", reply_markup=markup)
    return SELECT_DEL_DOMAIN


async def del_domain_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    domain = update.callback_query.data.split(":", 1)[1]
    context.user_data["del_domain"] = domain
    await update.callback_query.edit_message_text(
        f"Delete `{domain}` and its stored token?\n_This does not touch Cloudflare._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Yes, delete", callback_data="confirm_deldomain")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_op")],
            ]
        ),
    )
    return CONFIRM_DEL_DOMAIN


async def del_domain_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    domain = context.user_data.get("del_domain", "?")
    await store.remove_domain(domain)
    await store.clear_active_domain(domain)
    return await show_manage(update, context, note=f"🗑️ `{domain}` removed.")


# --- Set/Switch Domain (sticky active domain for /menu) ---


async def set_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    markup = _domain_buttons("setdom")
    if markup is None:
        return await show_manage(update, context, note="No domains added yet.")
    await update.callback_query.edit_message_text("Choose the domain to use:", reply_markup=markup)
    return SELECT_ACTIVE_DOMAIN


async def set_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    domain = update.callback_query.data.split(":", 1)[1]
    await store.set_active(update.effective_chat.id, domain)
    return await show_menu(update, context)


# --- Add Custom Hostname on the active domain (fallback init -> hostname -> TXT -> background poll) ---


async def _wait_active(client: CloudflareClient, zone_id: str, custom_id: str) -> dict:
    cur = await client.get_custom_hostname(zone_id, custom_id)
    for _ in range(POLL_TRIES):
        if cur.get("status") == "active" and (cur.get("ssl") or {}).get("status") == "active":
            return cur
        await asyncio.sleep(POLL_DELAY)
        cur = await client.get_custom_hostname(zone_id, custom_id)
    return cur


def _txt_targets(*payloads: dict) -> list[tuple[str, str]]:
    """Every TXT (name, value) pair needed: ownership verification + cert DCV records."""
    targets: list[tuple[str, str]] = []
    for payload in payloads:
        ov = payload.get("ownership_verification") or {}
        if ov.get("name") and ov.get("value"):
            targets.append((ov["name"], ov["value"]))
        for rec in ((payload.get("ssl") or {}).get("validation_records") or []):
            name = rec.get("txt_name") or rec.get("name")
            value = rec.get("txt_value") or rec.get("value")
            if name and value:
                targets.append((name, value))
    return list(dict.fromkeys(targets))


async def _status_icon_of(client: CloudflareClient, zone_id: str, custom_id: str) -> str:
    try:
        cur = await client.get_custom_hostname(zone_id, custom_id)
        return status_icon(cur.get("status", "?"), (cur.get("ssl") or {}).get("status", "?"))
    except CFApiError:
        return "❓"


async def _poll_and_notify(app, user_id: int, chat_id: int, api_token: str, zone_id: str,
                           domain: str, hostname: str, custom_id: str, want_cname: bool) -> None:
    """Background verify-poll so the user can keep using the bot meanwhile."""
    try:
        async with CloudflareClient(api_token) as client:
            cur = await _wait_active(client, zone_id, custom_id)
            cname_note = ""
            if want_cname:
                try:
                    await client.add_cname_record(zone_id, hostname, domain, proxied=True)
                    cname_note = f"\n✅ CNAME `{hostname}` → `{domain}` added (proxied)."
                except CFApiError as exc:
                    cname_note = f"\n❌ CNAME failed: {exc}"
    except CFApiError as exc:
        await app.bot.send_message(
            chat_id, f"❌ Background check for `{hostname}` failed: {exc}", parse_mode="Markdown")
        return
    except Exception:
        log.exception("background verify crashed for %s", hostname)
        await app.bot.send_message(
            chat_id, f"⚠️ Background check for `{hostname}` crashed — use Status to re-check.",
            parse_mode="Markdown")
        return
    status = cur.get("status", "?")
    ssl_status = (cur.get("ssl") or {}).get("status", "?")
    if status == "active" and ssl_status == "active":
        note = "✅ Hostname active, certificate issued and deployed."
    else:
        note = f"⏳ `{hostname}`: hostname `{status}`, certificate `{ssl_status}` — re-check later with Status."
    await app.bot.send_message(chat_id, note + cname_note, parse_mode="Markdown")


async def add_host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    domain = store.get_active(update.effective_chat.id)
    if domain is None:
        return await show_manage(update, context, note="Set a domain first:")
    creds = store.get_domain(domain)
    if creds is None:
        return await show_manage(update, context, note="Set a domain first:")
    await update.callback_query.edit_message_text("⏳ Ensuring fallback origin…")
    try:
        async with CloudflareClient(creds["api_token"]) as client:
            zone_id = creds.get("zone_id") or await client.resolve_zone_id(domain)
            origin = f"{FALLBACK_SUB}.{domain}"
            try:
                fb = await client.get_fallback_origin(zone_id)
            except CFApiError as e:
                if e.code != 1551:  # 1551 = never configured, proceed to create
                    raise
                fb = {}
            if fb.get("origin") != origin or (fb.get("status") or "") != "active":
                if not await client.list_dns_records(zone_id, origin, "CNAME"):
                    await client.add_cname_record(zone_id, origin, domain, proxied=True)
                await client.set_fallback_origin(zone_id, origin)
                await asyncio.sleep(3)
                fb = await client.get_fallback_origin(zone_id)
            fb_status = fb.get("status", "?")
    except CFApiError as exc:
        await update.callback_query.edit_message_text(f"❌ {exc}", reply_markup=menu_markup())
        return MENU
    await update.callback_query.edit_message_text(
        f"Fallback origin `{origin}` ({fb_status}).\n"
        f"Send the custom hostname (e.g. `support.zoom.us.{domain}`):",
        parse_mode="Markdown",
        reply_markup=back_markup(),
    )
    return ASK_HOSTNAME


async def recv_hostname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    domain = store.get_active(update.effective_chat.id)
    if domain is None:
        return await show_menu(update, context)
    hostname = update.message.text.strip().lower().rstrip(".")
    if not hostname.endswith("." + domain):
        await update.message.reply_text(
            f"Hostname must be a subdomain of `{domain}` (the zone apex itself cannot be a custom hostname). Try again:",
            parse_mode="Markdown",
        )
        return ASK_HOSTNAME
    creds = store.get_domain(domain)
    if creds is None:
        return await show_menu(update, context)
    context.user_data["host_domain"] = domain
    context.user_data["new_hostname"] = hostname
    await update.message.reply_text(
        f"Also add a proxied CNAME mapping `{hostname}` to the apex `{domain}`?",
        parse_mode="Markdown",
        reply_markup=cname_markup(),
    )
    return ASK_CNAME


async def cname_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    want_cname = update.callback_query.data == "cname_yes"
    domain = context.user_data.get("host_domain")
    hostname = context.user_data.get("new_hostname")
    if not domain or not hostname:
        await update.callback_query.edit_message_text("Session expired — back to menu.", reply_markup=menu_markup())
        return MENU
    creds = store.get_domain(domain)
    if creds is None:
        await update.callback_query.edit_message_text("Domain was removed.", reply_markup=menu_markup())
        return MENU
    await update.callback_query.edit_message_text("Creating hostname + TXT verification records…")
    try:
        async with CloudflareClient(creds["api_token"]) as client:
            zone_id = creds.get("zone_id") or await client.resolve_zone_id(domain)
            created = await client.create_custom_hostname(zone_id, hostname)
            # POST may omit the DCV token — re-fetch details (per CF docs).
            await asyncio.sleep(5)
            try:
                details = await client.get_custom_hostname(zone_id, created["id"])
            except CFApiError:
                details = created
            targets = _txt_targets(details, created)
            for name, value in targets:
                existing = await client.list_dns_records(zone_id, name, "TXT")
                if any(r.get("content") == value for r in existing):
                    continue
                await client.add_txt_record(zone_id, name, value)
            if not targets and want_cname:
                await client.add_cname_record(zone_id, hostname, domain, proxied=True)
    except CFApiError as exc:
        await update.callback_query.edit_message_text(f"❌ {exc}", reply_markup=menu_markup())
        return MENU
    if not targets:
        context.user_data.clear()
        extra = f"\n✅ CNAME `{hostname}` → `{domain}` added (proxied)." if want_cname else ""
        await update.callback_query.edit_message_text(
            "⚠️ Created, but Cloudflare returned no TXT verification records — complete verification manually."
            + extra,
            parse_mode="Markdown",
            reply_markup=menu_markup(),
        )
        return MENU
    asyncio.create_task(
        _poll_and_notify(
            context.application,
            update.effective_user.id,
            update.effective_chat.id,
            creds["api_token"],
            zone_id,
            domain,
            hostname,
            created["id"],
            want_cname,
        )
    )
    context.user_data.clear()
    await update.callback_query.edit_message_text(
        f"⏳ `{hostname}` created, {len(targets)} TXT record(s) ensured — "
        "verifying in background, nothing more to do.",
        parse_mode="Markdown",
        reply_markup=menu_markup(),
    )
    return MENU


# --- Status (hostnames on the active domain -> hostname + certificate status) ---


async def status_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    domain = store.get_active(update.effective_chat.id)
    if domain is None:
        return await show_manage(update, context, note="Set a domain first:")
    creds = store.get_domain(domain)
    if creds is None:
        return await show_manage(update, context, note="Set a domain first:")
    try:
        async with CloudflareClient(creds["api_token"]) as client:
            zone_id = creds.get("zone_id") or await client.resolve_zone_id(domain)
            hosts = await client.list_custom_hostnames(zone_id)
            if not hosts:
                await update.callback_query.edit_message_text(
                    f"No custom hostnames on `{domain}`.", parse_mode="Markdown", reply_markup=menu_markup()
                )
                return MENU
            context.user_data["stat_domain"] = domain
            context.user_data["stat_list"] = [(h.get("id"), h.get("hostname")) for h in hosts]
            icons = await asyncio.gather(
                *(_status_icon_of(client, zone_id, hid) for hid, _ in context.user_data["stat_list"]))
    except CFApiError as exc:
        await update.callback_query.edit_message_text(f"❌ {exc}", reply_markup=menu_markup())
        return MENU
    rows = [[InlineKeyboardButton(f"{icon} {name}", callback_data=f"stathost:{i}")]
            for i, ((_, name), icon) in enumerate(zip(context.user_data["stat_list"], icons))]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op")])
    await update.callback_query.edit_message_text(
        f"Choose a hostname on `{domain}`:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )
    return SELECT_STATUS_HOST


async def status_host_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END
    await update.callback_query.answer()
    idx = int(update.callback_query.data.split(":", 1)[1])
    custom_id, name = context.user_data["stat_list"][idx]
    domain = context.user_data["stat_domain"]
    creds = store.get_domain(domain)
    if creds is None:
        await update.callback_query.edit_message_text("Domain was removed.", reply_markup=menu_markup())
        return MENU
    try:
        async with CloudflareClient(creds["api_token"]) as client:
            zone_id = creds.get("zone_id") or await client.resolve_zone_id(domain)
            cur = await client.get_custom_hostname(zone_id, custom_id)
    except CFApiError as exc:
        await update.callback_query.edit_message_text(f"❌ {exc}", reply_markup=menu_markup())
        return MENU
    lines = [
        f"{status_icon(cur.get('status', '?'), (cur.get('ssl') or {}).get('status', '?'))} `{name}`",
        f"Hostname: `{cur.get('status', '?')}`",
        f"Certificate: `{(cur.get('ssl') or {}).get('status', '?')}`",
    ]
    for rec in ((cur.get("ssl") or {}).get("validation_records") or []):
        label = rec.get("txt_name") or rec.get("http_url") or "validation"
        lines.append(f"• `{label}`: {rec.get('status', '?')}")
    if cur.get("created_at"):
        lines.append(f"Created: `{cur['created_at']}`")
    context.user_data.clear()
    await update.callback_query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                                   reply_markup=menu_markup())
    return MENU


async def unknown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()


def build_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler(["start", "manage_domain"], show_manage),
            CommandHandler("menu", show_menu),
            CallbackQueryHandler(show_manage, pattern="^manage$"),
            CallbackQueryHandler(show_menu, pattern="^menu$"),
        ],
        states={
            MANAGE: [
                CallbackQueryHandler(add_domain_start, pattern="^add_domain$"),
                CallbackQueryHandler(del_domain_start, pattern="^del_domain$"),
                CallbackQueryHandler(set_start, pattern="^set_domain$"),
            ],
            ASK_DOMAIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_domain_name)],
            ASK_ACCOUNT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_account_id)],
            ASK_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_token)],
            SELECT_DEL_DOMAIN: [CallbackQueryHandler(del_domain_picked, pattern="^deldom:")],
            CONFIRM_DEL_DOMAIN: [CallbackQueryHandler(del_domain_confirmed, pattern="^confirm_deldomain$")],
            SELECT_ACTIVE_DOMAIN: [CallbackQueryHandler(set_picked, pattern="^setdom:")],
            MENU: [
                CallbackQueryHandler(add_host_start, pattern="^add_host$"),
                CallbackQueryHandler(status_start, pattern="^status$"),
            ],
            ASK_HOSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_hostname)],
            ASK_CNAME: [
                CallbackQueryHandler(cname_answer, pattern="^cname_yes$"),
                CallbackQueryHandler(cname_answer, pattern="^cname_no$"),
            ],
            SELECT_STATUS_HOST: [CallbackQueryHandler(status_host_picked, pattern="^stathost:")],
        },
        fallbacks=[
            CommandHandler(["start", "manage_domain"], show_manage),
            CommandHandler("menu", show_menu),
            CallbackQueryHandler(show_manage, pattern="^manage$"),
            CallbackQueryHandler(show_menu, pattern="^menu$"),
            CallbackQueryHandler(cancel, pattern="^cancel_op$"),
            CommandHandler("cancel", cancel),
        ],
        per_message=False,
    )


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS)


def build_application(token: str) -> Application:
    from telegram.ext import Application as _App

    app = _App.builder().token(token).post_init(_post_init).build()
    app.add_handler(build_conversation())
    return app
