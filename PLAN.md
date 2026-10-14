# PLAN.md — cf-auto-custom-hostname

Decisions (user-confirmed): **Python** bot (lightweight + fast dev) · **JSON file** state store · user never builds images — everything runs via **`docker-compose.yml` / `docker-swarm.yml` + `entrypoint.sh`** (installer + starter).

## 1. Architecture

```
Telegram user ──► bot (python-telegram-bot, long polling)
                    ├─► state: /data/state.json (mounted volume, chmod 600)
                    └─► Cloudflare v4 API (httpx): zones, DNS records, custom hostnames
```

- Base runtime: `python:3-alpine` pulled by compose — no image build step for the user.
- `entrypoint.sh` (idempotent): if app source missing → `git clone/pull $APP_REPO_URL`; `pip install -r requirements.txt`; `exec python -m bot`.
- `BOT_TOKEN` + `ALLOWED_CHAT_IDS` live inline in `docker-compose.yml` / `docker-swarm.yml` (single-file deploy, no `.env`). Everything else (per-domain account ID + API token) is entered through the bot chat and kept only in `/data/state.json` — never in code or the image. `APP_REPO_URL` defaults to the public repo URL baked into `entrypoint.sh`.
- Restart policy (`unless-stopped` / swarm `any-node`) replaces systemd for power-outage recovery; the JSON store on a volume preserves state.

## 2. Repo layout (to create)

```
bot/__main__.py        # entry, polling setup
bot/handlers.py        # /start /menu + 6-button flows + conversation states
bot/cf.py              # Cloudflare API client (zones, dns_records, custom_hostnames)
bot/store.py           # JSON load/save, domain CRUD
requirements.txt       # python-telegram-bot, httpx
entrypoint.sh          # installer + starter, idempotent
.gitignore             # data/state.json
docker-compose.yml     # image: python:3-alpine, volumes ./:/app ./data:/data, secrets inline
docker-swarm.yml       # swarm variant, secrets inline (no env_file: stack ignores it)
data/.gitkeep          # state.json lives here at runtime (git-ignored)
```

## 3. Data model (`/data/state.json`)

```json
{"domains": {"example.com": {"zone_id": "...", "account_id": "...", "api_token": "...", "added_at": "..."}},
 "active": {"<chat_id>": "example.com"}}
```

## 4. Bot flows (per `desired-goal.txt`)

- `/start` → `/manage_domain`: Add domain (name → account id → token, zone-validated; first domain auto-activates), Delete domain (pick → confirm, clears active use), Set/Switch domain (sticky active domain per chat).
- `/menu` (no active domain → redirects to manage): Add Custom Hostname (ensure fallback origin → hostname → CNAME choice upfront → create + all TXT → background poll + notify, auto-CNAME if chosen), Status (pick hostname on active domain → hostname + cert + DCV statuses).
- Hamburger commands `/start /manage_domain /menu /cancel` registered via `set_my_commands` at startup.
- Deleted vs original spec: List Domains, List Custom Hostnames, Delete Custom Hostname (deliberate), per-action domain picking.
- After successful add → "Also add CNAME?" yes/no; yes → create proxied CNAME mapping the leading part to `@` (apex).

## 5. Cloudflare API mapping

- Zone resolve: `GET /zones?name={domain}` → `zone_id`.
- Custom hostname: `POST /zones/{z}/custom_hostnames {hostname, ssl:{method:"txt"}}`; status via `GET .../custom_hostnames/{id}`; delete via `DELETE`.
- TXT verify: add EVERY TXT pair from create+details responses — `ownership_verification` {name,value} plus each `ssl.validation_records[]` {txt_name,txt_value} — skipping any already present (idempotent); then poll in a background task until `status`+`ssl.status` are `active` and notify the chat (menu usable meanwhile).
- CNAME: `POST /zones/{z}/dns_records {type:CNAME, name:<sub>, content:"@", proxied:true}`.

## 6. Build phases

1. Scaffold layout + `store.py` + echo bot; `data/` git-ignored, `chmod 600` on state file.
2. Domain CRUD handlers with zone validation.
3. `cf.py` + Add flow (create → TXT → poll) + List/Delete custom hostnames.
4. CNAME follow-up prompt.
5. `entrypoint.sh` + compose/swarm files; `APP_REPO_URL` default = public GitHub URL.
6. Verify: `python -m py_compile`, mocked handler tests, `host-spawn podman compose up` smoke test (real run needs `BOT_TOKEN` + CF creds).

## 7. Findings (confirmed live 2026-10-14, zone `rohcuan.dpdns.org` Free plan)

- Token needs Zone Read + DNS Edit + SSL Edit; narrower tokens fail with CF `10000 Authentication error` on `dns_records`/`custom_hostnames` (`bot/cf.py` maps this to a hint).
- Free-plan zone returns CF `1404` (no SSL-for-SaaS quota) on custom-hostname list AND create — the sole remaining blocker; endpoint/payload verified correct (API parses the request, rejects only on quota). Needs SaaS enrollment via the form in the error message.
- Unblock steps (docs-verified, dashboard-only): zone → SSL/TLS → Custom Hostnames → Enable (non-Enterprise must enter payment info; Free includes 100 hostnames, $0.10/additional) → set fallback origin (proxied A/AAAA/CNAME + designate). No API-only path.
- Fallback origin auto-init implemented in Add Custom Hostname: checks status, creates proxied `fallback.<domain>` → apex CNAME if missing, designates via `PUT fallback_origin` (idempotent). Verified live: zone has no fallback configured yet, so the bot will create it on first run (still gated on SaaS enablement).
- Note: `GET /user/tokens/verify` falsely reports `Invalid API Token` for this token while zone/DNS calls succeed — do not trust it; test a real endpoint instead.
- `ownership_verification` response shape still unverified (blocked on the above).
