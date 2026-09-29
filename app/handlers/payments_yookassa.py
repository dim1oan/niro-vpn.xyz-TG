"""ЮKassa webhook (обрабатывается в aiohttp-приложении, см. app/web/server.py).

Здесь — чистая бизнес-логика подтверждения платежа, вызывается из web-слоя.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import PaymentProvider, PaymentStatus
from app.db.repositories.payments import PaymentRepo
from app.db.repositories.servers import ServerRepo
from app.services.billing import BillingService
from app.services.notifications import notify_admins, notify_user
from app.services.referral import credit_referral
from app.utils.logging import get_logger

log = get_logger("yookassa_webhook")


def is_yookassa_ip(remote: str) -> bool:
    try:
        addr = ipaddress.ip_address(remote)
    except ValueError:
        return False
    from app.services.payments.yookassa import YOOKASSA_WEBHOOK_NETWORKS

    for net in YOOKASSA_WEBHOOK_NETWORKS:
        if addr in ipaddress.ip_network(net, strict=False):
            return True
    return False


async def handle_payment_succeeded(session_factory: async_sessionmaker[AsyncSession], event: dict[str, Any]) -> bool:
    """Вебхук payment.succeeded: всегда перезапрашиваем платёж по API."""
    api_obj = event.get("object") or {}
    api_id = api_obj.get("id")
    if not api_id:
        return False

    from app.services.payments.yookassa import YooKassaClient

    yk = YooKassaClient()
    succeeded, data = await yk.is_succeeded(str(api_id))
    await yk.close()
    if not succeeded:
        log.warning("webhook.not_succeeded_after_refetch", yk_id=api_id)
        return False

    metadata = data.get("metadata") or {}
    payment_id = int(metadata.get("payment_id", 0) or 0)
    amount_value = float((data.get("amount") or {}).get("value", 0))

    async with session_factory() as session:
        repo = PaymentRepo(session)
        payment = await repo.get(payment_id)
        if payment is None or payment.provider != PaymentProvider.YOOKASSA:
            log.warning("webhook.unknown_payment", payment_id=payment_id)
            return True  # 200, чтобы ЮKassa не ретраила чужое

        if payment.status == PaymentStatus.PAID:
            return True

        billing = BillingService(session)
        paid = await billing.settle(payment.id, external_id=str(api_id), expected_amount=int(amount_value * 100))
        if paid is None or paid.status != PaymentStatus.PAID:
            await notify_admins(event.get("bot"), f"⚠️ Не удалось подтвердить платёж №{payment_id} (ЮKassa {api_id})")
            return False

        server = await ServerRepo(session).pick_best()
        if server is None:
            await notify_admins(event.get("bot"), f"⚠️ Платёж №{paid.id} оплачен, но нет активных серверов!")
            return True

        from app.handlers.buy import build_vless_url

        try:
            sub = await billing.grant_for_payment(server, paid)
            url = await build_vless_url(sub)
            await notify_user(
                event.get("bot"),
                paid.user.telegram_id,
                "🎉 Оплата прошла! Ваш доступ готов:\n\n" + f"<code>{url}</code>",
            )
            await credit_referral(session, paid)
        except Exception as e:  # noqa: BLE001
            log.error("webhook.provision_failed", err=str(e), payment_id=paid.id)
            await notify_user(event.get("bot"), paid.user.telegram_id, "✅ Оплата получена! Доступ выдадим в течение нескольких минут.")
        await session.commit()
    return True


async def reconcile_pending(session_factory: async_sessionmaker[AsyncSession], bot=None) -> tuple[int, int]:
    """Фоновая реконсиляция pending-платежей ЮKassa старше 2 минут."""
    from app.services.payments.yookassa import YooKassaClient

    yk = YooKassaClient()
    checked = granted = 0
    async with session_factory() as session:
        pending = await PaymentRepo(session).pending_older_than(minutes=2, provider=PaymentProvider.YOOKASSA)
        for payment in pending:
            checked += 1
            api_id = payment.payload.get("yookassa_id")
            if not api_id:
                continue
            succeeded, _data = await yk.is_succeeded(str(api_id))
            if not succeeded:
                continue
            billing = BillingService(session)
            paid = await billing.settle(payment.id, external_id=str(api_id))
            if paid is not None and paid.status == PaymentStatus.PAID:
                # сервер — из payload платежа (выбор страны на сайте/в боте), иначе лучший свободный
                server_code = (payment.payload or {}).get("server_code")
                server = await ServerRepo(session).get_by_code(server_code) if server_code else None
                if server is None:
                    server = await ServerRepo(session).pick_best()
                if server is not None:
                    try:
                        sub = await billing.grant_for_payment(server, paid)
                        await credit_referral(session, paid)
                        from app.handlers.buy import build_vless_url

                        url = await build_vless_url(sub)
                        await notify_user(bot, paid.user.telegram_id, "🎉 Оплата подтверждена! Ваш ключ:\n\n" + f"<code>{url}</code>")
                        granted += 1
                        await session.commit()
                        continue
                    except Exception as e:  # noqa: BLE001
                        log.error("reconcile.provision_failed", err=str(e), payment_id=paid.id)
                else:
                    await notify_admins(bot, f"⚠️ Reconcile: платёж №{paid.id} оплачен, нет серверов!")
        await session.commit()
    await yk.close()
    return checked, granted
