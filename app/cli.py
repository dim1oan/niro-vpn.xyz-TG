"""CLI утилиты: сид сервера из env, сид тарифов, разовый прогон задач.

python -m app.cli seed_server
python -m app.cli seed_plans
python -m app.cli run_job <name>
"""

from __future__ import annotations

import asyncio
import sys

import sqlalchemy as sa

from app.config import get_config
from app.db.models import Server
from app.db.session import make_engine, make_session_factory
from app.utils.crypto import encrypt


async def seed_server() -> None:
    """Сидирует первый сервер (Франция) из .env."""
    cfg = get_config()
    if not cfg.xui_base_url or not cfg.server_host:
        print("XUI_BASE_URL/SERVER_HOST не заданы в .env")
        sys.exit(1)

    engine = make_engine(cfg)
    factory = make_session_factory(engine)
    async with factory() as session:
        existing = await session.scalar(sa.select(Server).where(Server.code == cfg.server_code))
        if existing is not None:
            print(f"Сервер {cfg.server_code} уже существует (id={existing.id})")
            return
        server = Server(
            code=cfg.server_code,
            country_name=cfg.server_country,
            country_flag=cfg.server_flag,
            xui_base_url=cfg.xui_base_url.rstrip("/"),
            xui_username_enc=encrypt(cfg.xui_username, cfg.secret_key),
            xui_password_enc=encrypt(cfg.xui_password, cfg.secret_key),
            server_host=cfg.server_host,
            inbound_id=cfg.xui_inbound_id,
            sub_url=cfg.xui_sub_url or None,
            max_clients=cfg.server_max_clients,
            is_active=True,
            sort_order=100,
        )
        session.add(server)
        await session.commit()
        print(f"✅ Сервер {server.code} создан (id={server.id})")
    await engine.dispose()


ADD_SERVER_USAGE = (
    "Usage: python -m app.cli add_server <code> <country> <flag> <base_url> "
    "<username> <password> <host> <inbound_id> [--protocol vless|vless-reality] "
    "[--max-clients N] [--sort-order N] [--sub-url URL]"
)


async def add_server(args: list[str]) -> None:
    """Добавляет сервер вручную. Креды панели шифруются SECRET_KEY из .env."""
    import argparse

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("code")
    p.add_argument("country")
    p.add_argument("flag")
    p.add_argument("base_url")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("host")
    p.add_argument("inbound_id", type=int)
    p.add_argument("--protocol", default="vless-reality")
    p.add_argument("--max-clients", type=int, default=300)
    p.add_argument("--sort-order", type=int, default=100)
    p.add_argument("--sub-url", default="")
    try:
        a = p.parse_args(args)
    except SystemExit:
        print(ADD_SERVER_USAGE)
        return

    # проверяем панель до записи в БД
    from app.services.xui.client import XuiClient

    client = XuiClient(base_url=a.base_url.rstrip("/"), username=a.username, password=a.password)
    try:
        inbound = await client.get_inbound(a.inbound_id)
        print(f"Панель доступна, inbound {a.inbound_id}: порт {inbound.port}, {inbound.protocol}")
    finally:
        await client.close()

    cfg = get_config()
    engine = make_engine(cfg)
    factory = make_session_factory(engine)
    async with factory() as session:
        existing = await session.scalar(sa.select(Server).where(Server.code == a.code))
        if existing is not None:
            print(f"Сервер {a.code} уже существует (id={existing.id})")
            await engine.dispose()
            return
        server = Server(
            code=a.code,
            country_name=a.country,
            country_flag=a.flag,
            xui_base_url=a.base_url.rstrip("/"),
            xui_username_enc=encrypt(a.username, cfg.secret_key),
            xui_password_enc=encrypt(a.password, cfg.secret_key),
            server_host=a.host,
            inbound_id=a.inbound_id,
            sub_url=a.sub_url or None,
            max_clients=a.max_clients,
            is_active=True,
            sort_order=a.sort_order,
            protocol=a.protocol,
        )
        session.add(server)
        await session.commit()
        print(f"✅ Сервер {server.code} создан (id={server.id}, inbound={server.inbound_id})")
    await engine.dispose()


DEFAULT_PLANS = [
    ("trial", "Пробный", 3, 0, 0, 1, True),
    ("1m", "1 месяц", 30, 19900, 150, 3, False),
    ("3m", "3 месяца", 90, 49900, 375, 3, False),
    ("6m", "6 месяцев", 180, 89900, 675, 3, False),
    ("12m", "1 год", 365, 149000, 1120, 5, False),
]


async def seed_plans() -> None:
    from app.db.repositories.plans import PlanRepo

    cfg = get_config()
    engine = make_engine(cfg)
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = PlanRepo(session)
        for i, (code, title, days, price_kopeks, stars, devices, is_trial) in enumerate(DEFAULT_PLANS):
            await repo.upsert(
                code=code,
                title=title,
                duration_days=days,
                price_kopeks=price_kopeks,
                price_stars=stars,
                device_limit=devices,
                is_trial=is_trial,
                sort_order=i * 10,
            )
        await session.commit()
        print("✅ Тарифы засидированы")
    await engine.dispose()


async def run_job(name: str) -> None:
    cfg = get_config()
    engine = make_engine(cfg)
    factory = make_session_factory(engine)
    from app.tasks import jobs

    fn = getattr(jobs, name, None)
    if fn is None:
        available = ", ".join(j for j in dir(jobs) if not j.startswith("_") and callable(getattr(jobs, j)))
        print(f"Задача '{name}' не найдена. Доступны: {available}")
        return
    result = await fn(session_factory=factory, bot=None)
    print(f"✅ {name}: {result}")
    await engine.dispose()


def main() -> None:
    commands: dict = {
        "seed_server": seed_server,
        "seed_plans": seed_plans,
        "run_job": run_job,
        "add_server": add_server,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print("Usage: python -m app.cli {seed_server|seed_plans|run_job <name>|add_server ...}")
        sys.exit(1)
    cmd = commands[sys.argv[1]]
    args = sys.argv[2:]
    if cmd is add_server:
        asyncio.run(add_server(args))
    elif args:
        asyncio.run(cmd(args[0]))
    else:
        asyncio.run(cmd())


if __name__ == "__main__":
    main()
