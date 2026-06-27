"""ILLUSTRATIVE — a spec, not wired-in code. Mirrors a NEW file: scripts/seed_keys.py
(run via `make seed`, see examples/Makefile.snippet).

Idempotent virtual-key provisioning. Run AFTER the stack is up:

    uv run python -m scripts.seed_keys           # generate-if-absent, write .env
    uv run python -m scripts.seed_keys --rotate  # delete the alias and re-mint

Design points (see ../design.md §"Idempotent seeding"):
  * /key/generate mints a NEW key every call, so a naive seed is not re-runnable.
    We give the key a STABLE key_alias and generate only if that alias is absent.
  * LiteLLM stores only the *hashed* token, so an existing alias whose plaintext
    we no longer have in .env cannot be recovered — we refuse (or --rotate) rather
    than mint a duplicate or write a broken value.
  * .env is written only when LITELLM_VIRTUAL_KEY is unset/blank; a working value
    is never clobbered.
  * Uses the stdlib only (urllib/json) so the seed has no extra dependency.

Admin calls use the MASTER key — this is the one place the master key is used at
runtime. The app itself never uses it (see examples/app_config.py).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GATEWAY_BASE_URL", "http://localhost:4000")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-sandbox-master")
ENV_VAR = "LITELLM_VIRTUAL_KEY"
KEY_ALIAS = "app-runtime"          # stable identity that makes the seed idempotent
ENV_PATH = Path(os.environ.get("ENV_FILE", ".env"))

# Tiny budget on purpose: one cheap real call crosses it, so the budget test costs
# fractions of a cent. Low RPM so the rate-limit test trips in a couple of calls.
KEY_SPEC = {
    "key_alias": KEY_ALIAS,
    "models": ["chat", "embeddings"],   # scoped to the aliases, never provider keys
    "max_budget": float(os.environ.get("SEED_MAX_BUDGET", "0.01")),
    "budget_duration": os.environ.get("SEED_BUDGET_DURATION", "30d"),
    "rpm_limit": int(os.environ.get("SEED_RPM_LIMIT", "3")),
    "tpm_limit": int(os.environ.get("SEED_TPM_LIMIT", "10000")),
}


def _api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:   # VERIFY: admin paths/shapes on the pin
        return json.loads(resp.read() or b"{}")


def _alias_exists() -> bool:
    """True if a key with our alias is already registered (plaintext unknown)."""
    info = _api("GET", "/key/list")          # VERIFY: /key/list response shape across versions
    keys = info.get("keys", info if isinstance(info, list) else [])
    return any((k.get("key_alias") if isinstance(k, dict) else None) == KEY_ALIAS for k in keys)


def _key_is_valid(key: str) -> bool:
    try:
        _api("GET", f"/key/info?key={key}")  # 200 => still valid (not revoked)
        return True
    except urllib.error.HTTPError:
        return False


def _read_env_value() -> str | None:
    if os.environ.get(ENV_VAR):
        return os.environ[ENV_VAR]
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            s = line.strip()
            if s.startswith(f"{ENV_VAR}=") and not s.startswith("#"):
                return s.split("=", 1)[1].strip() or None
    return None


def _write_env_value(value: str) -> None:
    """Set LITELLM_VIRTUAL_KEY in .env without clobbering an existing real value."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, replaced = [], False
    for line in lines:
        s = line.strip()
        # replace only a commented/blank placeholder, never a populated value
        if s.startswith(f"{ENV_VAR}=") or s == f"# {ENV_VAR}=":
            out.append(f"{ENV_VAR}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{ENV_VAR}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def _rotate() -> None:
    _api("POST", "/key/delete", {"key_aliases": [KEY_ALIAS]})  # VERIFY: delete-by-alias support on the pin


def main(argv: list[str]) -> int:
    rotate = "--rotate" in argv

    existing = _read_env_value()
    if existing and not rotate:
        if _key_is_valid(existing):
            print(f"{ENV_VAR} already set and valid — nothing to do.")
            return 0
        print(f"{ENV_VAR} is set but the gateway rejects it; re-run with --rotate.", file=sys.stderr)
        return 1

    if rotate:
        _rotate()
    elif _alias_exists():
        # alias present but we have no plaintext locally — minting again would
        # create a duplicate under the same alias and we still couldn't recover it.
        print(f"key alias '{KEY_ALIAS}' exists but its value is not in {ENV_PATH}; "
              f"re-run with --rotate to replace it.", file=sys.stderr)
        return 2

    resp = _api("POST", "/key/generate", KEY_SPEC)
    key = resp["key"]                       # VERIFY: response field is `key`
    _write_env_value(key)
    print(f"minted virtual key (alias={KEY_ALIAS}, budget=${KEY_SPEC['max_budget']}) "
          f"and wrote {ENV_VAR} to {ENV_PATH}.")
    print("Restart the app so it picks up the new key:  docker compose up -d app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
