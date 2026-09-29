"""Личный код сайта: показ и перевыпуск из бота (ТЗ v2, ф-1)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.repositories.site_codes import SiteCodeRepo
from app.keyboards.callbacks import MenuCB, SiteCB
from app.locales.ru import t
from app.utils.logging import get_logger
from app.utils.time import fmt_dt

router = Router(name="site")
log = get_logger("site")


def site_kb(code: str):
    from aiogram.types import InlineKeyboardMarkup
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    cfg = get_config()
    ikb = InlineKeyboardBuilder()
    if cfg.site_base_url:
        ikb.button(text=t("site_code_open_btn"), url=cfg.site_base_url.rstrip("/") + "/activate")
    ikb.button(text=t("site_code_new_btn"), callback_data=SiteCB(action="reissue"))
    ikb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    ikb.adjust(1)
    markup: InlineKeyboardMarkup = ikb.as_markup()
    return markup


async def send_site_code(message: Message, session: AsyncSession, user) -> None:
    cfg = get_config()
    if not cfg.site_base_url:
        await message.answer(t("site_code_disabled"))
        return
    repo = SiteCodeRepo(session)
    try:
        code = await repo.ensure_code(user)
    except PermissionError:
        await message.answer(t("site_code_banned"))
        return
    log.info("site.code_issued", user_id=user.id, telegram_id=user.telegram_id)
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


@router.message(F.text.regexp(r"^/site$"))
async def cmd_site(message: Message, session: AsyncSession, user) -> None:
    await send_site_code(message, session, user)


@router.callback_query(SiteCB.filter(F.action == "info"))
async def cb_site_info(cb: CallbackQuery, session: AsyncSession, user) -> None:
    await send_site_code(cb.message, session, user)
    await cb.answer()


@router.callback_query(SiteCB.filter(F.action == "reissue"))
async def cb_site_reissue(cb: CallbackQuery, session: AsyncSession, user) -> None:
    cfg = get_config()
    if not cfg.site_base_url:
        await cb.answer(t("site_code_disabled"), show_alert=True)
        return
    code = await SiteCodeRepo(session).reissue(user)
    if code is None:
        await cb.answer(t("site_code_banned"), show_alert=True)
        return
    log.info("site.code_reissued", user_id=user.id, telegram_id=user.telegram_id)
    await cb.message.answer(
        t("site_code_reissued", code=code.code, expires=fmt_dt(code.expires_at, cfg.tz)),
        reply_markup=site_kb(code.code),
        disable_web_page_preview=True,
    )
    await cb.answer("Новый код готов")
