"""``python -m darwin_platform`` — CLI entry point.

Subcommands:

  ``serve``        Run the FastAPI supervisor (uvicorn).
  ``signing-key``  Print the supervisor's signing public key (PEM).

The ``serve`` subcommand wires the optional ``--db`` flag through to the
supervisor's persistence_path so a single command brings up a
restart-survivable platform without a separate config file. Auth is OFF
by default (the surface trusts every caller) — opt in by providing an
``--auth-tokens`` JSON file describing tokens + scopes + owned darwins.

Examples::

    python -m darwin_platform serve
    python -m darwin_platform serve --host 0.0.0.0 --port 8000 --db state.db
    python -m darwin_platform serve --auth-tokens tokens.json

The tokens.json shape is a list of objects, one per token::

    [
      {"token": "alice-tok", "scopes": ["owner"], "subject": "alice@x.com",
       "owned_darwins": []},
      {"token": "ops-tok",   "scopes": ["platform_operator"], "subject": "ops"}
    ]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _serve(args: argparse.Namespace) -> int:
    """Run the FastAPI app via uvicorn. Optional persistence + auth wiring."""
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print(
            "uvicorn is required to serve. Install with:\n"
            "  pip install 'darwin-dna[platform]' uvicorn",
            file=sys.stderr,
        )
        return 2

    from .api import build_app
    from .auth import InMemoryTokenStore
    from .supervisor import Supervisor

    sup = Supervisor(
        persistence_path=args.db if args.db else None,
    )

    token_store = None
    if args.auth_tokens:
        path = Path(args.auth_tokens).expanduser()
        try:
            tokens = json.loads(path.read_text())
        except Exception as exc:  # pragma: no cover - input validation
            print(f"failed to read {path}: {exc}", file=sys.stderr)
            return 2
        store = sup.persistent_token_store or InMemoryTokenStore()
        for row in tokens:
            store.issue(
                row["token"],
                scopes=set(row.get("scopes", [])),
                owned_darwins=set(row.get("owned_darwins", []) or []),
                subject=row.get("subject"),
            )
        token_store = store

    app = build_app(sup, token_store=token_store)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _signing_key(args: argparse.Namespace) -> int:
    """Print the platform's signing public key. Useful for ops verification
    that a manifest was signed by the right HSM-equivalent identity."""
    from .supervisor import Supervisor

    sup = Supervisor(persistence_path=args.db if args.db else None)
    print(sup.signing_public_key_pem().decode("ascii"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="darwin-platform",
        description=(
            "Darwin platform control plane CLI (docs/engineering/08). "
            "Runs the supervisor + FastAPI surface; optional SQLite "
            "persistence so audit log, billing, lineage, tickets, and "
            "alerts survive restarts."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the FastAPI supervisor")
    serve.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; set 0.0.0.0 for k8s)",
    )
    serve.add_argument(
        "--port", default=8000, type=int, help="Bind port (default: 8000)",
    )
    serve.add_argument(
        "--db", default=None,
        help="SQLite file for restart-survivable state. Omitted = in-memory.",
    )
    serve.add_argument(
        "--auth-tokens", default=None,
        help=(
            "JSON file of bearer tokens to seed the token store. "
            "Without this, the API trusts every caller — DO NOT expose "
            "publicly."
        ),
    )
    serve.add_argument(
        "--log-level", default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    serve.set_defaults(func=_serve)

    keycmd = sub.add_parser(
        "signing-key", help="Print the platform's signing public key (PEM)",
    )
    keycmd.add_argument(
        "--db", default=None,
        help="Optional SQLite file (only used so the supervisor opens "
        "consistently; the signing key is per-process)",
    )
    keycmd.set_defaults(func=_signing_key)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
