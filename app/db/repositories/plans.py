"""Репозиторий тарифов."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan


class PlanRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, plan_id: int) -> Plan | None:
        return await self.session.get(Plan, plan_id)

    async def get_by_code(self, code: str) -> Plan | None:
        return await self.session.scalar(select(Plan).where(Plan.code == code))

    async def list_active(self, *, include_trial: bool = False) -> list[Plan]:
        q = select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.price_kopeks)
        plans = list((await self.session.scalars(q)).all())
        return [p for p in plans if include_trial or not p.is_trial]

    async def get_trial_plan(self) -> Plan | None:
        return await self.session.scalar(
            select(Plan).where(Plan.is_trial.is_(True), Plan.is_active.is_(True))
        )

    async def upsert(
        self,
        *,
        code: str,
        title: str,
        duration_days: int,
        price_kopeks: int,
        price_stars: int,
        device_limit: int,
        is_trial: bool = False,
        sort_order: int = 100,
    ) -> Plan:
        plan = await self.get_by_code(code)
        if plan is None:
            plan = Plan(code=code)
            self.session.add(plan)
        plan.title = title
        plan.duration_days = duration_days
        plan.price_kopeks = price_kopeks
        plan.price_stars = price_stars
        plan.device_limit = device_limit
        plan.is_trial = is_trial
        plan.sort_order = sort_order
        await self.session.flush()
        return plan
