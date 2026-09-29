"""Атомарная выдача доступа: БД-транзакция + вызовы панели 3x-ui."""

from __future__ import annotations

import secrets
import uuid as uuid_lib
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import Payment, Plan, Server, Subscription, SubStatus, User
from app.db.repositories.subs import gen_xui_email
from app.services.xui.exceptions import XuiApiError, XuiClientNotFound
from app.services.xui.factory import get_client
from app.utils.logging import get_logger
from app.utils.time import to_ms, utcnow

log = get_logger("provisioning")


def gen_uuid() -> str:
    return str(uuid_lib.uuid4())


def gen_sub_id() -> str:
    return secrets.token_hex(8)


class ProvisioningService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.cfg = get_config()

    # ---------- публичные операции ----------

    async def grant_access(
        self, user: User, plan: Plan, server: Server, payment: Payment | None,
        *, bonus_days: int = 0,
    ) -> Subscription:
        """Выдача новой подписки по оплаченному платежу. Идемпотентно по payment.id.

        Шаги:
          1) транзакционно резервируем платёж (PAID) и создаём подписку;
          2) создаём клиента в панели;
          3) при ошибке панели помечаем payment.provision_failed (retry-job доделает).
        """
        existing = await self._subscription_for_payment(payment)
        if existing is not None:
            log.info("grant.idempotent_hit", payment_id=payment.id if payment else None)
            await self.ensure_panel_client(existing)
            return existing

        email = gen_xui_email(user.telegram_id)
        client_uuid = gen_uuid()
        sub_id = gen_sub_id()
        expires_at = utcnow() + timedelta(days=plan.duration_days + max(0, bonus_days))

        from app.db.models import PaymentStatus

        if payment is not None:
            payment.status = PaymentStatus.PAID
            if payment.paid_at is None:
                payment.paid_at = utcnow()
        else:
            payment = Payment(
                user_id=user.id,
                plan_id=plan.id,
                provider="manual",
                amount_kopeks=plan.price_kopeks,
                currency="RUB",
                status=PaymentStatus.PAID,
                paid_at=utcnow(),
            )
            self.session.add(payment)
            await self.session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            plan_id=plan.id,
            xui_client_uuid=client_uuid,
            xui_email=email,
            xui_sub_id=sub_id,
            xui_inbound_id=server.inbound_id,
            status=SubStatus.ACTIVE,
            started_at=utcnow(),
            expires_at=expires_at,
        )
        self.session.add(sub)
        await self.session.flush()  # получаем sub.id
        payment.subscription_id = sub.id
        await self.session.flush()

        await self._create_in_panel(sub, plan)
        await self._replicate_to_all_servers(sub, plan)

        if plan.is_trial:
            user.trial_used = True
            self.session.add(user)

        log.info("grant.ok", sub_id=sub.id, email=email, server=server.code, plan=plan.code)
        return sub

    async def _replicate_to_all_servers(self, sub: Subscription, plan: Plan) -> None:
        """Копирует клиента (тот же UUID/subId) на все остальные активные серверы.
        Нужно для единой подписки: один ключ = все страны. best-effort."""
        from sqlalchemy import select as _sel

        others = (await self.session.execute(
            _sel(Server).where(Server.is_active == True, Server.id != sub.server_id)  # noqa: E712
        )).scalars().all()
        if not others:
            return
        payload = self.client_payload(sub, plan)
        for srv in others:
            client = get_client(srv)
            try:
                await client.add_client(srv.inbound_id, payload)
                log.info("panel.replicated", sub_id=sub.id, server=srv.code)
            except Exception as e:  # noqa: BLE001 — не валим выдачу из-за второго сервера
                log.warning("panel.replicate_failed", sub_id=sub.id, server=srv.code, err=str(e))

    async def _subscription_for_payment(self, payment: Payment | None) -> Subscription | None:
        if payment is None or payment.id is None:
            return None
        await self.session.refresh(payment)
        if payment.subscription_id is not None:
            return await self.session.get(Subscription, payment.subscription_id)
        return None

    async def ensure_panel_client(self, sub: Subscription) -> None:
        """Гарантирует наличие клиента в панели (после сбоя между БД и API)."""
        from app.services.xui.factory import get_client

        client = get_client(sub.server)
        try:
            traffic = await client.get_client_traffic(sub.xui_email)
            if traffic is not None:
                return
        except Exception:  # noqa: BLE001 — ошибка чтения не мешает попытке создания
            pass
        payload = self.client_payload(sub, sub.plan)
        try:
            await client.add_client(sub.server.inbound_id, payload)
            log.warning("ensure.recreated", email=sub.xui_email)
        except Exception as e:  # noqa: BLE001
            raise ProvisioningError(str(e)) from e

    async def _create_in_panel(self, sub: Subscription, plan: Plan) -> None:
        """Создаёт клиента в панели. Ошибка -> provision_failed на платеже."""
        server = sub.server
        client = get_client(server)
        payload = self.client_payload(sub, plan)
        try:
            await client.add_client(server.inbound_id, payload)
        except Exception as e:  # noqa: BLE001 — любая ошибка панели => retry
            log.error("panel.add_client_failed", email=sub.xui_email, err=str(e))
            await self._mark_provision_failed(sub)
            raise ProvisioningError(str(e)) from e

    async def move_to_inbound(self, sub: Subscription, new_inbound_id: int) -> None:
        """Переносит клиента подписки на новый inbound панели.

        1) выдаёт клиенту новую identity (uuid/email/subId) — чтобы не ловить
           дубликаты email/uuid, пока старый клиент ещё существует;
        2) создаёт клиента в новом inbound с прежними лимитами/сроком;
        3) при успехе удаляет клиента из старого inbound;
        4) при неудаче откатывает identity и прокидывает ошибку.
        """
        client = get_client(sub.server)
        old_uuid, old_email = sub.xui_client_uuid, sub.xui_email
        old_sub_id, old_inbound = sub.xui_sub_id, sub.xui_inbound_id
        sub.xui_client_uuid = gen_uuid()
        sub.xui_email = gen_xui_email(sub.user.telegram_id if sub.user else None)
        sub.xui_sub_id = gen_sub_id()
        sub.xui_inbound_id = new_inbound_id
        self.session.add(sub)
        await self.session.flush()
        try:
            await client.add_client(new_inbound_id, self.client_payload(sub, sub.plan))
        except Exception as e:  # noqa: BLE001
            log.error("panel.move_add_failed", sub_id=sub.id, err=str(e))
            sub.xui_client_uuid, sub.xui_email = old_uuid, old_email
            sub.xui_sub_id, sub.xui_inbound_id = old_sub_id, old_inbound
            self.session.add(sub)
            await self.session.flush()
            raise ProvisioningError(str(e)) from e
        # Старый клиент в панели живёт до успешного создания нового; гасим best-effort:
        # если не выйдет — можно удалить вручную, на работу подписки он не влияет.
        try:
            await client.delete_client(old_email)
        except Exception as e:  # noqa: BLE001
            log.warning("panel.move_delete_old_failed", sub_id=sub.id, email=old_email, err=str(e))
        log.info("panel.moved", sub_id=sub.id, inbound=new_inbound_id)

    @staticmethod
    def client_payload(sub: Subscription, plan: Plan):
        from app.services.xui.models import XuiClientConfig

        # REALITY-инбаунды требуют xtls-rprx-vision; для остальных (vless+tcp/tls/grpc)
        # flow обязан быть пустым — иначе клиент не подключится.
        flow = "xtls-rprx-vision" if sub.server.protocol == "vless-reality" else ""
        tg_id = sub.user.telegram_id if sub.user and sub.user.telegram_id is not None else 0
        return XuiClientConfig(
            id=sub.xui_client_uuid,
            flow=flow,
            email=sub.xui_email,
            limitIp=plan.device_limit,
            totalGB=0,
            expiryTime=to_ms(sub.expires_at),
            enable=(sub.status == SubStatus.ACTIVE),
            tgId=tg_id,
            subId=sub.xui_sub_id,
        )

    async def _mark_provision_failed(self, sub: Subscription) -> None:
        from sqlalchemy import select

        from app.db.models import Payment

        payment = await self.session.scalar(
            select(Payment).where(Payment.subscription_id == sub.id).order_by(Payment.id.desc()).limit(1)
        )
        if payment is not None:
            payment.provision_failed = True
            payment.provision_attempts += 1
            self.session.add(payment)
        sub.status = SubStatus.DISABLED
        self.session.add(sub)
        await self.session.flush()

    async def retry_failed(self, payment: Payment) -> Subscription | None:
        """Повтор выдачи для payment со provision_failed."""
        if payment.subscription_id is None:
            return None
        sub = await self.session.get(Subscription, payment.subscription_id)
        if sub is None:
            return None
        plan = sub.plan
        server = sub.server
        client = get_client(server)
        try:
            await client.add_client(server.inbound_id, self.client_payload(sub, plan))
        except Exception as e:  # noqa: BLE001
            payment.provision_attempts += 1
            self.session.add(payment)
            log.warning("retry.failed", payment_id=payment.id, attempts=payment.provision_attempts, err=str(e))
            return None
        payment.provision_failed = False
        sub.status = SubStatus.ACTIVE if sub.expires_at > utcnow() else SubStatus.EXPIRED
        self.session.add_all([payment, sub])
        log.info("retry.ok", payment_id=payment.id, email=sub.xui_email)
        return sub

    # ---------- продление / перевыпуск / отключение ----------

    async def renew(self, sub: Subscription, days: int) -> Subscription:
        """Продление: активная — += duration, истёкшая — от now. Тот же uuid/email."""
        from app.db.repositories.subs import compute_renewal

        new_expires, was_active = compute_renewal(sub.expires_at, days)
        old_expires = sub.expires_at
        sub.expires_at = new_expires
        if sub.status == SubStatus.EXPIRED and new_expires > utcnow():
            sub.status = SubStatus.ACTIVE
            sub.notified_expired = False
            sub.notified_3d = sub.notified_1d = False
        elif was_active:
            sub.notified_3d = sub.notified_1d = False
        self.session.add(sub)
        await self.session.flush()

        client = get_client(sub.server)
        plan = sub.plan
        payload = self.client_payload(sub, plan)
        payload.expiry_time = to_ms(new_expires)
        try:
            await client.update_client(sub.xui_email, payload)
        except XuiApiError as e:
            # клиент мог быть удалён вручную — пересоздаём с тем же uuid
            log.warning("renew.recreate_missing", email=sub.xui_email, err=e.msg)
            try:
                await client.add_client(sub.server.inbound_id, payload)
                log.info("renew.recreated", email=sub.xui_email)
            except XuiApiError as e2:
                sub.expires_at = old_expires
                self.session.add(sub)
                raise ProvisioningError(f"renew failed: {e2.msg}") from e2
        # продление на всех остальных серверах (единая подписка)
        from sqlalchemy import select as _sel
        others = (await self.session.execute(
            _sel(Server).where(Server.is_active == True, Server.id != sub.server_id)  # noqa: E712
        )).scalars().all()
        for srv in others:
            try:
                await get_client(srv).update_client(sub.xui_email, payload)
                log.info("renew.replicated", sub_id=sub.id, server=srv.code)
            except Exception as e:  # noqa: BLE001
                log.warning("renew.replicate_failed", sub_id=sub.id, server=srv.code, err=str(e))

        log.info("renew.ok", sub_id=sub.id, until=new_expires.isoformat())
        return sub

    async def reissue(self, sub: Subscription) -> Subscription:
        """Новый uuid + subId (старые ссылки перестают работать). Не чаще 1 раза в 24ч."""
        sub.xui_client_uuid = gen_uuid()
        sub.xui_sub_id = gen_sub_id()
        self.session.add(sub)
        await self.session.flush()

        client = get_client(sub.server)
        payload = self.client_payload(sub, sub.plan)
        inbound_id = sub.server.inbound_id
        try:
            await client.update_client(sub.xui_email, payload)
        except XuiApiError:
            # старый uuid удалён из панели — создаём новый
            try:
                await client.add_client(inbound_id, payload)
            except XuiApiError as e:
                raise ProvisioningError(f"reissue failed: {e.msg}") from e
        log.info("reissue.ok", sub_id=sub.id)
        return sub

    async def disable_in_panel(self, sub: Subscription, *, enable: bool = False) -> None:
        """enable=false в панели (при истечении) или обратно true."""
        payload = self.client_payload(sub, sub.plan)
        payload.enable = enable
        client = get_client(sub.server)
        try:
            await client.update_client(sub.xui_email, payload)
        except XuiApiError as e:
            if enable is False:
                log.warning("disable.skipped", email=sub.xui_email, err=e.msg)
                return
            try:
                await client.add_client(sub.server.inbound_id, payload)
            except XuiApiError as e2:
                raise ProvisioningError(f"enable failed: {e2.msg}") from e2

    async def delete_from_panel(self, sub: Subscription) -> bool:
        client = get_client(sub.server)
        try:
            await client.delete_client(sub.xui_email)
            return True
        except (XuiApiError, XuiClientNotFound) as e:
            log.info("delete.absent", email=sub.xui_email, err=str(e))
            return False

    async def sync_traffic_one(self, sub: Subscription) -> int | None:
        client = get_client(sub.server)
        traffic = await client.get_client_traffic(sub.xui_email)
        if traffic is None:
            return None
        sub.traffic_used_bytes = traffic.used_bytes
        sub.last_synced_at = utcnow()
        self.session.add(sub)
        return traffic.used_bytes


class ProvisioningError(Exception):
    pass
