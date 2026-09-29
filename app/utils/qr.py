"""QR-коды ссылок подписки (PNG в BytesIO для send_photo)."""

from __future__ import annotations

import io

import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M


def make_qr_png(data: str, box_size: int = 10, border: int = 2) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img: Image.Image = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "qr.png"
    return buf
