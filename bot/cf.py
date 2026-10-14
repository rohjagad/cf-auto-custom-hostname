"""Minimal async Cloudflare v4 API client: zones, DNS records, custom hostnames."""
from __future__ import annotations

import httpx

BASE = "https://api.cloudflare.com/client/v4"


class CFApiError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


_HINTS = {
    # No SSL-for-SaaS quota on the zone/account (e.g. Free plan without purchased quota).
    1404: "hint: this zone/account has no SSL-for-SaaS quota — custom hostnames need purchased quota/entitlement",
    # Generic auth rejection: usually a token-scope problem on these endpoints.
    10000: "hint: the API token likely lacks the required scope (needs Zone Read + DNS Edit + SSL Edit)",
}


def _check(resp: httpx.Response, what: str):
    try:
        body = resp.json()
    except ValueError:
        raise CFApiError(f"{what}: HTTP {resp.status_code} (non-JSON response)")
    if not body.get("success"):
        errs = body.get("errors") or [{"message": "unknown error"}]
        detail = "; ".join(e.get("message", str(e)) for e in errs)
        hints = "; ".join(_HINTS[c] for e in errs if (c := e.get("code")) in _HINTS)
        codes = [e.get("code") for e in errs if e.get("code") is not None]
        raise CFApiError(
            f"{what}: {detail}" + (f" ({hints})" if hints else ""),
            code=codes[0] if codes else None,
        )
    return body["result"]
    return body["result"]


class CloudflareClient:
    def __init__(self, api_token: str):
        self._client = httpx.AsyncClient(
            base_url=BASE,
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CloudflareClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def resolve_zone_id(self, domain: str) -> str:
        result = _check(await self._client.get("/zones", params={"name": domain}), "zone lookup")
        if not result:
            raise CFApiError(f"zone lookup: no zone found for {domain}")
        return result[0]["id"]

    async def create_custom_hostname(self, zone_id: str, hostname: str) -> dict:
        return _check(
            await self._client.post(
                f"/zones/{zone_id}/custom_hostnames",
                json={"hostname": hostname, "ssl": {"method": "txt", "type": "dv"}},
            ),
            "create custom hostname",
        )

    async def get_custom_hostname(self, zone_id: str, custom_id: str) -> dict:
        return _check(
            await self._client.get(f"/zones/{zone_id}/custom_hostnames/{custom_id}"),
            "custom hostname status",
        )

    async def list_custom_hostnames(self, zone_id: str) -> list:
        return _check(
            await self._client.get(f"/zones/{zone_id}/custom_hostnames", params={"per_page": 100}),
            "list custom hostnames",
        )

    async def delete_custom_hostname(self, zone_id: str, custom_id: str) -> None:
        _check(
            await self._client.delete(f"/zones/{zone_id}/custom_hostnames/{custom_id}"),
            "delete custom hostname",
        )

    async def add_txt_record(self, zone_id: str, name: str, content: str) -> dict:
        return _check(
            await self._client.post(
                f"/zones/{zone_id}/dns_records",
                json={"type": "TXT", "name": name, "content": content, "ttl": 120},
            ),
            "add TXT record",
        )

    async def add_cname_record(self, zone_id: str, name: str, target: str, proxied: bool = True) -> dict:
        return _check(
            await self._client.post(
                f"/zones/{zone_id}/dns_records",
                json={"type": "CNAME", "name": name, "content": target, "proxied": proxied, "ttl": 1},
            ),
            "add CNAME record",
        )

    async def list_dns_records(self, zone_id: str, name: str, rtype: str) -> list:
        return _check(
            await self._client.get(f"/zones/{zone_id}/dns_records", params={"name": name, "type": rtype}),
            "list DNS records",
        )

    async def get_fallback_origin(self, zone_id: str) -> dict:
        return _check(
            await self._client.get(f"/zones/{zone_id}/custom_hostnames/fallback_origin"),
            "fallback origin status",
        )

    async def set_fallback_origin(self, zone_id: str, origin: str) -> dict:
        return _check(
            await self._client.put(
                f"/zones/{zone_id}/custom_hostnames/fallback_origin", json={"origin": origin}
            ),
            "set fallback origin",
        )
