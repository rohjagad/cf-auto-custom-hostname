# AGENTS.md

## State
Scaffolded Python bot (`bot/`) + `entrypoint.sh` + compose/swarm files per `PLAN.md`. Live-tested: bot + token scopes + DNS writes all work; custom-hostname flow blocked by CF-side missing SaaS quota (1404), not by code.

## Sources of truth (read first)
- `project-information/by-user-dev/desired-goal.txt` — product spec (bot menu, Cloudflare TXT-verify + optional CNAME flow).
- `project-information/by-ai-agents/` — currently empty; check contents before writing agent notes there.

## Hard constraint
- NEVER edit anything under `project-information/by-user-dev/` — read-only, preserves user authenticity.

## Intent (from spec)
- `/manage_domain`: Add/Delete/Set-Switch domain, sticky active domain per chat. `/menu` (needs active domain): Add Custom Hostname + Status. Delete/List hostnames, List domains, per-action domain picking removed per revised spec.
- Add-custom-hostname flow: ensure fallback origin, create, add all TXT (ownership + DCV), background poll + notify; after add, prompt for optional orange-clouded CNAME.

## Build constraints (from spec — no stack chosen yet)
- Keep Docker image lightweight (e.g. Alpine/slim base).
- Ship `entrypoint.sh` as install + start handler: pulls app files from the public GitHub repo and (re)starts the app so it recovers after power outage without systemd.
- Repo will be public: no tokens, account IDs, or chat IDs in code/images — runtime env vars or bot input only. Keep `entrypoint.sh` idempotent.

## Verify
- No docker daemon here — test with podman via `distrobox-host-exec` (`host-spawn` is broken in this sandbox). `python:3-alpine` pulls/runs fine.
- SELinux enforcing: bind mounts need `:z` (use a `/tmp` copy of the repo, not the project dir). Smoke test proven: mount + dummy `BOT_TOKEN` → entrypoint installs deps, inits `/data/state.json`, bot runs up to Telegram auth (`InvalidToken` = success).
