"""Vault seal/unseal operational drill (docs/engineering/09, 16).

Exercises the Vault seal/unseal procedure end-to-end:

1. Confirm Vault is **unsealed** and the configured token works
   (baseline). Optionally probe the platform's ``/readyz`` to capture
   the baseline state — many deployments will surface a Vault outage
   via the platform's persistence health when the platform's sensitive
   tier can't read.
2. **Seal** Vault via ``sys/seal``.
3. Confirm the seal stuck (``sys/seal-status`` reports sealed).
4. **Unseal** with the operator-supplied unseal key(s).
5. Confirm Vault is unsealed again.
6. (Optional) re-probe the platform ``/readyz``.

Requires the ``[hvac]`` extra. The point of the drill is to practice
the seal/unseal mechanics in a controlled exercise, including verifying
the operator actually has the unseal keys to hand.

Usage::

    # Dev Vault — single root token, single unseal key:
    vault server -dev -dev-root-token-id=devroot \\
        -dev-listen-address=127.0.0.1:8200 &
    # Capture the printed unseal key + use it below.

    uv run python -m tools.drills.vault_seal \\
        --vault-addr http://127.0.0.1:8200 \\
        --vault-token devroot \\
        --unseal-key <KEY>

    # Production-style — Shamir 3-of-5: pass --unseal-key three times.
    uv run python -m tools.drills.vault_seal \\
        --vault-addr https://vault.example.com:8200 \\
        --vault-token "$VAULT_ADMIN_TOKEN" \\
        --unseal-key K1 --unseal-key K2 --unseal-key K3 \\
        --platform-url https://darwin-dev.internal:8000

Safety: this drill **seals a live Vault**. Run it against a dev or
staging Vault, not production, unless that's the explicit exercise.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx


def _probe_platform_readyz(url: str, token: str | None) -> str:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.get(f"{url.rstrip('/')}/readyz", headers=headers, timeout=5.0)
    except Exception as exc:
        return f"probe-error: {exc}"
    return f"{r.status_code} — {r.text[:200]}"


def run_drill(
    vault_addr: str,
    vault_token: str,
    unseal_keys: list[str],
    *,
    platform_url: str | None = None,
    platform_token: str | None = None,
    seal_settle_seconds: float = 1.0,
) -> int:
    try:
        import hvac  # type: ignore[import-not-found]
    except ImportError:
        print("[drill] vault-seal drill requires the [hvac] extra: "
              "uv pip install 'darwin-dna[hvac]'", file=sys.stderr)
        return 2

    client = hvac.Client(url=vault_addr, token=vault_token)
    if not client.is_authenticated():
        print(f"[drill] FAIL — token does not authenticate to {vault_addr}",
              file=sys.stderr)
        return 1

    status = client.sys.read_seal_status()
    if status.get("sealed", True):
        print(f"[drill] FAIL — Vault at {vault_addr} is already sealed; "
              "unseal first, then re-run the drill", file=sys.stderr)
        return 1
    print(f"[drill] baseline: unsealed, version={status.get('version')}")

    if platform_url:
        print(f"[drill] baseline platform /readyz: "
              f"{_probe_platform_readyz(platform_url, platform_token)}")

    # 2-3. Seal.
    print("[drill] sealing Vault...")
    client.sys.seal()
    time.sleep(seal_settle_seconds)
    status = client.sys.read_seal_status()
    if not status.get("sealed", False):
        print("[drill] FAIL — Vault did not report sealed after sys/seal",
              file=sys.stderr)
        return 1
    print("[drill] sealed confirmed")

    if platform_url:
        print(f"[drill] sealed platform /readyz: "
              f"{_probe_platform_readyz(platform_url, platform_token)}")

    # 4-5. Unseal.
    print(f"[drill] unsealing Vault with {len(unseal_keys)} key share(s)...")
    for k in unseal_keys:
        client.sys.submit_unseal_key(k)
    time.sleep(seal_settle_seconds)
    status = client.sys.read_seal_status()
    if status.get("sealed", True):
        print(f"[drill] FAIL — Vault still sealed after {len(unseal_keys)} keys "
              f"(progress={status.get('progress')}/{status.get('t')})",
              file=sys.stderr)
        return 1
    print("[drill] unsealed confirmed")

    if platform_url:
        print(f"[drill] post-unseal platform /readyz: "
              f"{_probe_platform_readyz(platform_url, platform_token)}")

    print("[drill] vault_seal: PASS — seal/unseal cycle completed cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Vault seal/unseal operational drill (docs 09, 16).",
    )
    p.add_argument("--vault-addr", required=True, metavar="URL")
    p.add_argument("--vault-token", required=True, metavar="TOKEN")
    p.add_argument("--unseal-key", action="append", default=[], required=True,
                   metavar="KEY",
                   help="Unseal key share. Pass once for dev, T times for "
                        "a Shamir T-of-N config.")
    p.add_argument("--platform-url", default=None, metavar="URL",
                   help="Optional — also probes the platform's /readyz "
                        "at baseline / sealed / post-unseal so the drill "
                        "captures how the platform reacted.")
    p.add_argument("--platform-token", default=None, metavar="TOKEN")
    p.add_argument("--seal-settle-seconds", type=float, default=1.0)
    args = p.parse_args(argv)
    return run_drill(
        args.vault_addr,
        args.vault_token,
        args.unseal_key,
        platform_url=args.platform_url,
        platform_token=args.platform_token,
        seal_settle_seconds=args.seal_settle_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
