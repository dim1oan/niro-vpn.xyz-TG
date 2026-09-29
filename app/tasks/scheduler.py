"""Регистрация APScheduler-задач с локами и алертами админу."""

from __future__ import annotations

import asyncio
import functools
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.services.notifications import notify_admins
from app.utils.logging import get_logger

log = get_logger("scheduler")

_running: dict[str, asyncio.Lock] = {}


def with_lock_and_alert(name: str, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Лок от параллельного запуска + алерт админу при исключении."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        lock = _running.setdefault(name, asyncio.Lock())
        if lock.locked():
            log.warning("job.skipped_locked", job=name)
            return None
        async with lock:
            try:
                return await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc()[-1500:]
                log.error("job.failed", job=name, err=str(e), tb=tb)
                bot = kwargs.get("bot") or (args[0] if args else None)
                if bot is not None:
                    try:
                        await notify_admins(bot, f"🔥 Job <b>{name}</b> упал:\n<pre>{tb[-800:]}</pre>")
                    except Exception:  # noqa: BLE001
                        pass
                raise

    return wrapper


def setup_scheduler(session_factory, bot) -> AsyncIOScheduler:
    from app.tasks.jobs import (
        cleanup_deleted,
        expire_check,
        notify_expiring,
        notify_web_manual_payments,
        reconcile_panel,
        reconcile_payments,
        retry_provisioning,
        sync_traffic,
    )

    scheduler = AsyncIOScheduler(timezone="UTC")
    jobs: list[tuple[str, object, Callable]] = [
        ("sync_traffic", IntervalTrigger(minutes=15), sync_traffic),
        ("expire_check", IntervalTrigger(minutes=10), expire_check),
        ("notify_expiring", IntervalTrigger(hours=1), notify_expiring),
        ("cleanup_deleted", CronTrigger(hour=4, minute=30), cleanup_deleted),
        ("reconcile_payments", IntervalTrigger(minutes=5), reconcile_payments),
        ("reconcile_panel", IntervalTrigger(hours=6), reconcile_panel),
        ("retry_provisioning", IntervalTrigger(minutes=2), retry_provisioning),
        ("notify_web_manual", IntervalTrigger(minutes=10), notify_web_manual_payments),
    ]
    for name, trigger, fn in jobs:
        wrapped = with_lock_and_alert(name, fn)
        scheduler.add_job(
            wrapped,
            trigger=trigger,
            id=name,
            max_instances=1,
            coalesce=True,
            kwargs={"session_factory": session_factory, "bot": bot},
            misfire_grace_time=300,
        )
        log.info("job.registered", job=name)
    return scheduler


async def run_all_once(scheduler: AsyncIOScheduler) -> None:
    """Разовый прогон всех задач (для тестов/CLI)."""
    for job in scheduler.get_jobs():
        await job.func(**(job.kwargs or {}))
