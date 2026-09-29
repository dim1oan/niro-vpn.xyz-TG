"""Инструкция по подключению: индекс + рендер текстов из app/content/guides/*.md."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import MenuCB

router = Router(name="guides")

GUIDES_DIR = Path(__file__).resolve().parent.parent / "content" / "guides"

PLATFORMS = [
    ("ios", "📱 iOS"),
    ("android", "🤖 Android"),
    ("windows", "💻 Windows"),
    ("macos", "🖥 macOS"),
    ("linux", "🐧 Linux"),
]


def md_to_telegram_html(raw: str) -> str:
    """Минимальный Markdown → Telegram-HTML (# заголовки, **жирный**, `код`, ссылки)."""
    out_lines: list[str] = []
    in_code_block = False
    code_buf: list[str] = []

    def flush_code() -> None:
        if code_buf:
            out_lines.append(f"<pre>{escape(chr(10).join(code_buf))}</pre>")
            code_buf.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                flush_code()
                in_code_block = False
            else:
                flush_code()
                in_code_block = True
            continue
        if in_code_block:
            code_buf.append(line)
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            out_lines.append(f"\n<b>{escape(heading.group(2))}</b>")
            continue
        if not stripped:
            out_lines.append("")
            continue

        text = escape(stripped)
        # [text](url)
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
        # **bold** и `code`
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        out_lines.append(text)

    flush_code()
    rendered = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", rendered).strip()[:4000]


def render_guide(slug: str) -> str:
    path = GUIDES_DIR / f"{slug}.md"
    if not path.exists():
        return "Инструкция временно недоступна."
    return md_to_telegram_html(path.read_text(encoding="utf-8"))


@router.callback_query(MenuCB.filter(F.action == "guides"))
async def cb_guides(cb: CallbackQuery) -> None:
    if cb.message is not None:
        await send_guides_index(cb.message)
    await cb.answer()


async def send_guides_index(message: Message) -> None:
    ikb = InlineKeyboardBuilder()
    for slug, title in PLATFORMS:
        ikb.button(text=title, callback_data=MenuCB(action="guide", slug=slug))
    ikb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    ikb.adjust(2)
    from app.handlers.common import safe_edit

    await safe_edit(message, "📖 <b>Инструкция по подключению</b>\n\nВыберите вашу платформу:", ikb.as_markup())


@router.callback_query(MenuCB.filter(F.action == "guide"))
async def cb_guide_page(cb: CallbackQuery, callback_data: MenuCB) -> None:
    slug = callback_data.slug or ""
    text = render_guide(slug)
    ikb = InlineKeyboardBuilder()
    for other_slug, title in PLATFORMS:
        if other_slug != slug:
            ikb.button(text=title, callback_data=MenuCB(action="guide", slug=other_slug))
    ikb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    ikb.adjust(3, 1)
    if cb.message is None:
        await cb.answer()
        return
    from app.handlers.common import safe_edit

    await safe_edit(cb.message, text, ikb.as_markup(), disable_web_page_preview=True)
    await cb.answer()
