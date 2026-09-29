"""Поддержка: FSM сообщения юзера → админ-чат, ответ админа → юзеру."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.keyboards.admin import support_reply_kb
from app.keyboards.callbacks import MenuCB, SupportReplyCB
from app.locales.ru import t
from app.services.notifications import esc
from app.utils.logging import get_logger

router = Router(name="support")
log = get_logger("support")


class SupportFSM(StatesGroup):
    waiting_message = State()
    admin_reply = State()


@router.callback_query(MenuCB.filter(F.action == "support"))
async def cb_support(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportFSM.waiting_message)
    if cb.message is not None:
        from app.handlers.common import safe_edit

        await safe_edit(cb.message, t("support_prompt"))
    await cb.answer()


@router.message(SupportFSM.waiting_message, F.text | F.photo | F.document | F.voice | F.video)
async def forward_to_admins(message: Message, state: FSMContext, user) -> None:
    await state.clear()
    chat_id = _resolve_admin_chat()
    header = (
        f"🆘 <b>Обращение в поддержку</b>\n"
        f"От: {esc(user.display_name())} (<a href='tg://user?id={user.telegram_id}'>{user.telegram_id}</a>)\n\n"
    )
    sent = False
    try:
        if message.content_type == "text":
            await message.bot.send_message(chat_id, header + f"💬 {esc(message.html_text or '')}", parse_mode="HTML")
        else:
            await message.bot.send_message(chat_id, header, parse_mode="HTML")
            await message.copy_to(chat_id=chat_id)
        sent = True
    except Exception as e:  # noqa: BLE001
        log.warning("support.forward_failed", err=str(e))


    if sent:
        try:
            await message.bot.send_message(
                chat_id,
                f"↩️ Ответить пользователю <code>{user.telegram_id}</code>:",
                reply_markup=support_reply_kb(user.telegram_id),
                parse_mode="HTML",
            )
            from app.keyboards.user import to_menu_kb

            await message.answer(t("support_sent"), reply_markup=to_menu_kb())
        except Exception as e:  # noqa: BLE001
            log.warning("support.kb_failed", err=str(e))
            await message.answer(t("error_user"))
    else:
        await message.answer(t("error_user"))


def _resolve_admin_chat() -> int:
    from app.config import get_config

    return get_config().admin_chat()


@router.callback_query(SupportReplyCB.filter())
async def cb_admin_reply(cb: CallbackQuery, callback_data: SupportReplyCB, state: FSMContext) -> None:
    from app.config import get_config

    cfg = get_config()
    if str(cb.from_user.id) not in {str(i) for i in cfg.admin_ids}:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(SupportFSM.admin_reply)
    await state.update_data(reply_user_id=callback_data.user_id)
    await cb.message.answer(f"✍️ Введите ответ для пользователя <code>{callback_data.user_id}</code>:")
    await cb.answer()


@router.message(SupportFSM.admin_reply, F.text)
async def send_admin_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = int(data.get("reply_user_id", 0))
    await state.clear()
    from app.services.notifications import notify_user

    delivered = await notify_user(message.bot, user_id, f"💬 <b>Поддержка:</b>\n{esc(message.text or '')}")
    if delivered:
        await message.answer("✅ Ответ доставлен.")
    else:
        await message.answer("⚠️ Не удалось доставить сообщение пользователю.")


@router.message(SupportFSM.waiting_message)
async def unsupported_support_content(message: Message) -> None:
    await message.answer("Пришлите текст или вложение одним сообщением.")
