"""/start и главное меню."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.repositories.users import UserRepo
from app.handlers.common import channel_link, is_channel_member
from app.keyboards.callbacks import MenuCB
from app.keyboards.user import check_channel, main_menu
from app.locales.ru import t
from app.middlewares.user import UserContextMiddleware  # noqa: F401 (data["user"])
from app.utils.logging import get_logger
from app.utils.time import fmt_dt

router = Router(name="start")
log = get_logger("start")

REF_RE = re.compile(r"^ref(\d+)$")


async def show_main_menu(message: Message, user) -> None:
    cfg = get_config()
    await message.answer(
        t("menu_title"),
        reply_markup=main_menu(
            trial_used=user.trial_used,
            has_trial_enabled=cfg.trial_enabled,
            site_enabled=bool(cfg.site_base_url),
        ),
    )


async def send_main_menu(bot, chat_id: int, user) -> None:
    """Отправляет главное меню (для событий после оплаты/чека и т.п.)."""
    cfg = get_config()
    await bot.send_message(
        chat_id,
        t("menu_title"),
        reply_markup=main_menu(
            trial_used=user.trial_used,
            has_trial_enabled=cfg.trial_enabled,
            site_enabled=bool(cfg.site_base_url),
        ),
    )


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject | None,
    state: FSMContext,
    session: AsyncSession,
    user,
    is_new_user: bool,
    **kwargs,
) -> None:
    await state.clear()
    repo = UserRepo(session)

    # реферальный диплинк /start ref123456
    if command and command.args and (m := REF_RE.match(command.args.strip())):
        try:
            referrer_telegram_id = int(m.group(1))
            if referrer_telegram_id != user.telegram_id:
                referrer = await repo.get_by_telegram_id(referrer_telegram_id)
                if referrer is not None:
                    installed = await repo.set_referrer(user, referrer.id)
                    log.info("ref.set", referee=user.telegram_id, referrer=referrer.telegram_id, ok=installed)
        except ValueError:
            pass

    # обязательная подписка на канал
    bot = message.bot
    if not await is_channel_member(bot, user.telegram_id):
        await message.answer(
            t("need_channel", link=channel_link()),
            reply_markup=check_channel(channel_link()),
            disable_web_page_preview=True,
        )
        return

    if is_new_user or message.text == "/start":
        await show_main_menu(message, user)

    # личный код сайта: выдаём новому юзеру сразу после проверки канала (ТЗ v2)
    cfg = get_config()
    if cfg.site_base_url and is_new_user and not user.is_banned:
        from app.db.repositories.site_codes import SiteCodeRepo
        from app.handlers.site import site_kb

        code = await SiteCodeRepo(session).ensure_code(user)
        log.info("start.site_code_issued", telegram_id=user.telegram_id)
        await message.answer(
            t(
                "site_code_info",
                activate_url=cfg.site_base_url.rstrip("/") + "/activate",
                code=code.code,
                expires=fmt_dt(code.expires_at, cfg.tz),
            ),
            reply_markup=site_kb(code.code),
            disable_web_page_preview=True,
        )


@router.callback_query(MenuCB.filter(F.action == "check_sub"))
async def cb_check_subscription(cb: CallbackQuery, session: AsyncSession, user, state: FSMContext) -> None:
    if await is_channel_member(cb.bot, user.telegram_id):
        await cb.answer("Спасибо за подписку! 🎉", show_alert=True)
        await state.clear()
        if cb.message is not None:
            try:
                await cb.message.delete()
            except Exception:
                pass
        cfg = get_config()
        if cb.message is not None:
            await cb.message.answer(
                t("menu_title"),
                reply_markup=main_menu(
                    trial_used=user.trial_used,
                    has_trial_enabled=cfg.trial_enabled,
                    site_enabled=bool(cfg.site_base_url),
                ),
            )
    else:
        await cb.answer("Вы ещё не подписались на канал 🙃", show_alert=True)


@router.callback_query(MenuCB.filter(F.action == "back"))
async def cb_back_to_menu(cb: CallbackQuery, user, state: FSMContext) -> None:
    await state.clear()
    cfg = get_config()
    if cb.message is None:
        await cb.answer()
        return
    text = t("menu_title")
    markup = main_menu(
        trial_used=user.trial_used,
        has_trial_enabled=cfg.trial_enabled,
        site_enabled=bool(cfg.site_base_url),
    )
    from app.handlers.common import safe_edit

    await safe_edit(cb.message, text, markup)
    await cb.answer()
