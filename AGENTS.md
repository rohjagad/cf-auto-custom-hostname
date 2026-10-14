# AGENTS.md

## Must-read first
- `project-information/by-user-dev/desired-goal.txt` — product spec (source of truth for bot flows).
- `PLAN.md` §4–§5, §7 — flow decisions, CF API mapping, confirmed live findings. Trust executable code over PLAN prose when they conflict.
- NEVER edit anything under `project-information/by-user-dev/` (read-only).

## Layout
- `bot/__main__.py` — entry (`python -m bot`, long polling). `bot/handlers.py` — all flows. `bot/cf.py` — CF v4 client. `bot/store.py` — JSON store at `$DATA_DIR/state.json` (`{"domains": {...}, "active": {chat_id: domain}}`, chmod 600).
- `entrypoint.sh` — idempotent installer+starter: inits state file → refuses `PASTE_`-placeholder `BOT_TOKEN` → clones `$APP_REPO_URL` if source missing → `pip install` → `exec python -m bot`. Restart policy (not systemd) handles power-outage recovery.
- `docker-compose.yml` (service `bot`, bind mounts `./:/app`, `./data:/data`) vs `docker-swarm.yml` (service `telebot`, named volumes `telebot-app`/`telebot-data`, external net `common-overlay`, pinned `node.hostname == stb3`). Self-bootstrapping: swarm command wget-fetches `entrypoint.sh` when the app volume is empty, entrypoint clones the rest. Secrets are inline `BOT_TOKEN`/`ALLOWED_CHAT_IDS` with `PASTE_..._HERE` placeholders — no `.env` file. Keep quotes: token contains `:` and chat IDs must stay strings.

## Bot behavior (not obvious from filenames)
- `/manage_domain`: Add/Delete/Set-Switch domain, sticky active domain per chat. `/menu` redirects to manage when no active domain. List-domains / delete-hostname flows were deliberately removed.
- Add Custom Hostname: ensure fallback origin → ask hostname → ask CNAME **upfront** (`ASK_CNAME`) → create + add ALL TXT (`ownership_verification` + every `ssl.validation_records[]`, skip-if-present) → background poll → single notify (status + CNAME result). POST often omits DCV data, so code re-GETs details after create — keep that.
- Status list fetches each hostname's detail in parallel and prefixes `🟢` (both active) / `🟠` (one active) / `🔴` (neither) / `❓` (fetch failed).

## Cloudflare gotchas (live-verified 2026-10-14)
- Custom-hostname list/create return CF `1404` = zone lacks SaaS quota (dashboard-only fix: SSL/TLS → Custom Hostnames → Enable + payment; Free incl. 100). Not a code bug.
- Token needs Zone Read + DNS Edit + SSL Edit; `bot/cf.py` maps `10000` to a scope hint. `GET /user/tokens/verify` falsely reports invalid for good tokens — test a real endpoint instead.

## Verify (sandbox quirks)
- No docker daemon: use `distrobox-host-exec podman ...` (`host-spawn` is broken). SELinux enforcing: bind mounts need `:z`, and always test on a `/tmp` copy, never the project dir.
- Fast checks: `python3 -m py_compile bot/*.py`, `sh -n entrypoint.sh`, `distrobox-host-exec podman compose -f docker-compose.yml config`.
- Smoke test: dummy `BOT_TOKEN` → entrypoint installs deps, inits state, bot stops at Telegram `InvalidToken` (= success).
- arm64 is proven (`--arch arm64` under host qemu-aarch64 binfmt; image has `linux/arm64/v8`, all deps pure-Python). Do NOT pin `platform:` in compose files.

## Known traps
- `bot/__main__.py` error message still says "copy .env.example" but `.env.example` was deleted — point users at the compose files instead when touching it.
- `bot/cf.py::_check` has a duplicated `return body["result"]` line — harmless; delete on next touch.
