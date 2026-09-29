"""Реализации крон-задач (регистрация в app/tasks/scheduler.py)."""

from __future__ import annotations

import html
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.config import get_config
from app.db.models import Server, Subscription, SubStatus
from app.db.repositories.payments import PaymentRepo
from app.db.session import session_scope
from app.services.notifications import notify_admins, notify_user
from app.services.provisioning import ProvisioningService
from app.utils.logging import get_logger
from app.utils.time import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

log = get_logger("jobs")


# ---------- sync_traffic: каждые 15 минут ----------

async def sync_traffic(session_factory=None, bot: Bot | None = None) -> int:
    """getClientTraffics по всем активным подпискам → traffic_used_bytes."""
    from app.db.repositories.subs import SubRepo
    from app.services.xui.factory import close_all, get_client

    updated = 0
    async with session_scope(session_factory) as session:
        pairs = await SubRepo(session).all_syncable()
        by_server: dict[int, list] = {}
        for sub, server in pairs:
            by_server.setdefault(server.id, []).append(sub)

        for subs in by_server.values():
            if not subs:
                continue
            client = get_client(subs[0].server)
            for sub in subs:
                try:
                    traffic = await client.get_client_traffic(sub.xui_email)
                except Exception as e:  # noqa: BLE001
                    log.warning("sync.traffic_failed", email=sub.xui_email, err=str(e))
                    continue
                if traffic is None:
                    continue
                sub.traffic_used_bytes = traffic.used_bytes
                sub.last_synced_at = utcnow()
                session.add(sub)
                updated += 1
    await close_all()
    if updated:
        log.info("sync_traffic.done", updated=updated)
    return updated


# ---------- expire_check: каждые 10 минут ----------

async def expire_check(session_factory=None, bot: Bot | None = None) -> int:
    """expires_at <= now → status=expired + enable=false в панели."""
    now = utcnow()
    disabled = 0
    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.status == SubStatus.ACTIVE, Subscription.expires_at <= now
                )
            )
        ).all()
        if not rows:
            return 0
        service = ProvisioningService(session)
        from app.services.xui.factory import get_client

        for sub in rows:
            sub.status = SubStatus.EXPIRED
            session.add(sub)
            try:
                payload = service.client_payload(sub, sub.plan)
                payload.enable = False
                client = get_client(sub.server)
                await client.update_client(sub.xui_email, payload)
            except Exception as e:  # noqa: BLE001
                log.warning("expire.disable_failed", email=sub.xui_email, err=str(e))
            disabled += 1
    if disabled:
        log.info("expire_check.done", expired=disabled)
    return disabled


# ---------- notify_expiring: каждый час ----------

async def notify_expiring(session_factory=None, bot: Bot | None = None) -> int:
    """Напоминания за 3 дня / 1 день / 3 часа (без повторов) с кнопкой «Продлить»."""
    cfg = get_config()
    now = utcnow()
    sent = 0
    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(select(Subscription).where(Subscription.status == SubStatus.ACTIVE))
        ).all()
        for sub in rows:
            delta: timedelta = sub.expires_at - now
            template = None
            flag_field: str | None = None
            if not sub.notified_3d and timedelta(hours=24) < delta <= timedelta(hours=72):
                template, flag_field = "⏳ Подписка {flag} {plan} истекает через 3 дня.", "notified_3d"
            elif not sub.notified_1d and timedelta(hours=3) < delta <= timedelta(hours=25):
                template, flag_field = "⚠️ Подписка {flag} {plan} истекает через 1 день!", "notified_1d"
            elif not sub.notified_expired and timedelta(seconds=0) < delta <= timedelta(hours=3):
                template, flag_field = (
                    "🔥 Подписка {flag} {plan} истекает через несколько часов! Успейте продлить.",
                    "notified_expired",
                )
            if template is None or flag_field is None:
                continue
            setattr(sub, flag_field, True)
            session.add(sub)
            text = (
                f"{template.format(flag=sub.server.country_flag, plan=html.escape(sub.plan.title))}\n\n"
                f"Действует до <b>{sub.expires_at.astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')}</b>.\n"
                f"Продлите, чтобы не терять доступ 👇"
            )
            from aiogram.utils.keyboard import InlineKeyboardBuilder

            from app.keyboards.callbacks import KeysCB

            ikb = InlineKeyboardBuilder()
            ikb.button(text="➕ Продлить", callback_data=KeysCB(action="renew", sub_id=sub.id))
            ikb.adjust(1)
            try:
                assert bot is not None
                await bot.send_message(sub.user.telegram_id, text, reply_markup=ikb.as_markup())
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("notify.expiring_failed", user_id=sub.user.telegram_id, err=str(e))
    if sent:
        log.info("notify_expiring.done", sent=sent)
    return sent


# ---------- cleanup_deleted: раз в день ----------

async def cleanup_deleted(session_factory=None, bot: Bot | None = None) -> int:
    """Истёкшие > N дней → delClient в панели, status=deleted."""
    cfg = get_config()
    cutoff = utcnow() - timedelta(days=cfg.delete_expired_after_days)
    deleted = 0
    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.expires_at < cutoff,
                    Subscription.status.in_([SubStatus.EXPIRED, SubStatus.DISABLED]),
                )
            )
        ).all()
        if not rows:
            return 0
        service = ProvisioningService(session)
        for sub in rows:
            await service.delete_from_panel(sub)
            sub.status = SubStatus.DELETED  # помечаем даже если клиента уже нет в панели
            session.add(sub)
            deleted += 1
    if deleted:
        log.info("cleanup_deleted.done", deleted=deleted)
    return deleted


# ---------- reconcile_payments: каждые 5 минут ----------

async def reconcile_payments(session_factory=None, bot: Bot | None = None) -> tuple[int, int]:
    """Добить pending-платежи ЮKassa (страховка от потерянных вебхуков)."""
    from app.handlers.payments_yookassa import reconcile_pending

    return await reconcile_pending(session_factory, bot)


# ---------- reconcile_panel: каждые 6 часов ----------

async def reconcile_panel(session_factory=None, bot: Bot | None = None) -> dict[str, list[str]]:
    """Сверка БД ↔ панель: сироты в обе стороны → отчёт админу."""
    from app.services.xui.factory import get_client

    report: dict[str, list[str]] = {"panel_orphans": [], "db_orphans": []}
    async with session_scope(session_factory) as session:
        servers = (await session.scalars(select(Server).where(Server.is_active.is_(True)))).all()
        db_emails_by_server: dict[int, set[str]] = {}
        for sub in (await session.scalars(select(Subscription))).all():
            db_emails_by_server.setdefault(sub.server_id, set()).add(sub.xui_email)

        for srv in servers:
            try:
                inbound = await get_client(srv).get_inbound(srv.inbound_id)
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile.panel_unreachable", server=srv.code, err=str(e))
                continue
            panel_emails = {str(c.get("email")) for c in inbound.settings.get("clients") or []}
            db_emails = db_emails_by_server.get(srv.id, set())
            report["panel_orphans"].extend(f"{srv.code}:{e}" for e in sorted(panel_emails - db_emails))
            report["db_orphans"].extend(f"{srv.code}:{e}" for e in sorted(db_emails - panel_emails))

    problems = len(report["panel_orphans"]) + len(report["db_orphans"])
    if problems and bot is not None:
        orphan_lines = "\n".join(f"• {html.escape(p)}" for p in report["panel_orphans"][:30]) or "—"
        await notify_admins(
            bot,
            f"🔍 <b>Расхождение БД ↔ панель</b>\n\n"
            f"Сироты в панели ({len(report['panel_orphans'])}):\n{orphan_lines}\n\n"
            f"Нет в панели ({len(report['db_orphans'])}): "
            f"{html.escape(', '.join(report['db_orphans'][:10])) or '—'}",
        )
    return report


# ---------- retry_provisioning: каждые 2 минуты ----------

async def retry_provisioning(session_factory=None, bot: Bot | None = None) -> int:
    """Повтор выдачи упавших платежей (до 3 попыток)."""
    fixed = 0
    async with session_scope(session_factory) as session:
        payments = await PaymentRepo(session).failed_provisioning()
        if not payments:
            return 0
        service = ProvisioningService(session)
        for payment in payments:
            sub = await service.retry_failed(payment)
            if sub is not None:
                fixed += 1
                from app.handlers.buy import build_vless_url

                url = await build_vless_url(sub)
                await notify_user(bot, payment.user.telegram_id, f"🎉 Ваш доступ готов!\n\n<code>{url}</code>")
        await session.commit()
    if fixed:
        log.info("retry_provisioning.fixed", count=fixed)
    return fixed


# ---------- notify_web_manual: каждые 10 минут (страховка ТЗ v2) ----------

async def notify_web_manual_payments(session_factory=None, bot: Bot | None = None) -> int:
    """Досылает админам уведомления о веб-ручных оплатах, если сайт не смог их доставить
    (бот был выключен в момент загрузки чека). Идемпотентно: notify_manual_payment
    сам проверяет статус платежа и payload.admin_notified_at."""
    from app.db.models import Payment, PaymentProvider, PaymentStatus

    cfg = get_config()
    if bot is None or not cfg.internal_api_token:
        return 0

    from app.internal_api import notify_manual_payment

    # 1) собираем кандидатов (payment_id) короткой сессией
    async with session_factory() as db:
        rows = (
            await db.scalars(
                select(Payment.id).where(
                    Payment.provider == PaymentProvider.MANUAL,
                    Payment.status == PaymentStatus.PENDING,
                )
            )
        ).all()

    # 2) notify_manual_payment сам управляет сессией и идемпотентностью
    sent = 0
    for payment_id in rows:
        try:
            status, _ = await notify_manual_payment(session_factory, bot, payment_id)
            if status == 204:
                sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("notify.web_manual_failed", payment_id=payment_id, err=str(e))
    if sent:
        log.info("notify_web_manual.done", sent=sent)
    return sent
