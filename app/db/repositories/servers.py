"""Репозиторий серверов."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Server, Subscription, SubStatus


class ServerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, server_id: int) -> Server | None:
        return await self.session.get(Server, server_id)

    async def get_by_code(self, code: str) -> Server | None:
        return await self.session.scalar(select(Server).where(Server.code == code))

    async def list_active(self) -> list[Server]:
        rows = await self.session.scalars(
            select(Server).where(Server.is_active.is_(True)).order_by(Server.sort_order, Server.id)
        )
        return list(rows)

    async def list_all(self) -> list[Server]:
        rows = await self.session.scalars(select(Server).order_by(Server.sort_order, Server.id))
        return list(rows)

    async def active_clients_count(self, server_id: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.server_id == server_id,
                    Subscription.status == SubStatus.ACTIVE,
                    Subscription.expires_at > func.now(),
                )
            )
            or 0
        )

    async def pick_best(self) -> Server | None:
        """Сервер страны с наименьшим числом активных ключей (и не переполненный)."""
        servers = await self.list_active()
        if not servers:
            return None
        best: tuple[int, Server] | None = None
        for srv in servers:
            count = await self.active_clients_count(srv.id)
            if srv.max_clients and count >= srv.max_clients:
                continue
            if best is None or count < best[0]:
                best = (count, srv)
        if best is not None:
            return best[1]
        return None  # все заполнены

    async def countries(self) -> list[tuple[str, str]]:
        """Уникальные страны активных серверов: (flag, name)."""
        seen: dict[str, str] = {}
        for srv in await self.list_active():
            seen.setdefault(srv.country_name, srv.country_flag)
        return [(flag, name) for name, flag in seen.items()]
