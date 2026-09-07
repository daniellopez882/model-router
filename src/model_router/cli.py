"""Command line: `model-router serve | check | create-tenant | migrate`."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from model_router import __version__, db
from model_router.config import ConfigError, Settings, load_router_config
from model_router.tenancy import generate_key, hash_key, key_hint


def cmd_check(settings: Settings) -> int:
    """Validate the routing file and report which providers have credentials."""
    try:
        config = load_router_config(settings.ROUTER_CONFIG)
    except ConfigError as error:
        print(f"invalid: {error}", file=sys.stderr)
        return 2
    print(f"config ok: {len(config.providers)} providers, {len(config.routes)} routes")
    for provider in config.providers:
        state = (
            "key present"
            if provider.api_key()
            else (
                "no key needed" if provider.api_key_env is None else f"{provider.api_key_env} unset"
            )
        )
        print(
            f"  provider {provider.name:<16} {provider.kind.value:<10} "
            f"{provider.base_url}  [{state}]"
        )
    for route in config.routes:
        chain = " -> ".join(f"{c.provider}/{c.model}" for c in route.candidates)
        print(f"  route    {route.alias:<16} {route.strategy.value:<16} {chain}")
    return 0


def cmd_migrate(settings: Settings) -> int:
    db.init_schema(settings.DATABASE_URL)
    print("schema up to date")
    return 0


def cmd_create_tenant(settings: Settings, name: str, budget: Decimal | None) -> int:
    db.init_schema(settings.DATABASE_URL)
    factory = db.make_session_factory(db.make_engine(settings.DATABASE_URL))
    raw = generate_key()
    with factory() as session:
        tenant = db.Tenant(name=name, monthly_budget_usd=budget)
        session.add(tenant)
        session.flush()
        session.add(
            db.ApiKey(
                tenant_id=tenant.id, key_hash=hash_key(raw), hint=key_hint(raw), label="initial"
            )
        )
        session.commit()
        print(
            json.dumps(
                {
                    "tenant_id": tenant.id,
                    "name": name,
                    "api_key": raw,
                    "note": "the key is not stored; copy it now",
                }
            )
        )
    return 0


def cmd_serve(settings: Settings) -> int:
    import uvicorn

    uvicorn.run(
        "model_router.api.app:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model-router", description="OpenAI-compatible multi-provider gateway"
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP server")
    sub.add_parser("check", help="validate the routing configuration")
    sub.add_parser("migrate", help="apply database migrations")
    create = sub.add_parser("create-tenant", help="create a tenant and print its API key once")
    create.add_argument("name")
    create.add_argument("--budget-usd", type=Decimal, default=None)
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "serve":
        return cmd_serve(settings)
    if args.command == "check":
        return cmd_check(settings)
    if args.command == "migrate":
        return cmd_migrate(settings)
    return cmd_create_tenant(settings, args.name, args.budget_usd)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
