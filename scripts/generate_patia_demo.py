"""Generate PATIA's bilingual landing demo videos.

The renderer uses only local brand assets and fictional business data. It
creates an original soundtrack, then encodes browser-safe H.264/AAC MP4 files.
Run with the same Python environment used for development:

    python scripts/generate_patia_demo.py
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "static" / "videos"
LOGO_PATH = ROOT / "app" / "static" / "img" / "logo-patia.png"
WIDTH, HEIGHT = 1280, 720
FPS = 24
SCENE_SECONDS = 6
SCENES = 7
DURATION = SCENE_SECONDS * SCENES

INK = "#171827"
MUTED = "#62697c"
LINE = "#e2e5ee"
SURFACE = "#ffffff"
SOFT = "#f4f6fb"
PURPLE = "#6246e5"
PURPLE_DARK = "#422eb8"
BLUE = "#1689e8"
GREEN = "#15956a"
AMBER = "#d86a2f"
RED = "#bd4055"

FONT_REGULAR = Path(r"C:\Windows\Fonts\segoeui.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\segoeuib.ttf")


COPY = {
    "es": {
        "opening_title": "Tu negocio, bajo control.",
        "opening_body": "Ventas, inventario y decisiones conectadas en un solo lugar.",
        "sale_eyebrow": "VENDE SIN PERDER EL RITMO",
        "sale_title": "Registra cada venta en segundos",
        "sale_body": "PATIA actualiza existencias y conserva un ticket profesional.",
        "inventory_eyebrow": "INVENTARIO QUE SE MUEVE CONTIGO",
        "inventory_title": "Sabe qué tienes y qué hace falta",
        "inventory_body": "Detecta productos bajos y recibe mercancía sin perder el historial.",
        "cash_eyebrow": "CIERRA EL DÍA CON CLARIDAD",
        "cash_title": "Caja y ventas siempre conectadas",
        "cash_body": "Conoce cuánto debería haber y detecta diferencias a tiempo.",
        "reports_eyebrow": "ENTIENDE EL NEGOCIO",
        "reports_title": "Convierte la operación en respuestas",
        "reports_body": "Ventas, utilidad y productos rentables explicados con claridad.",
        "pro_eyebrow": "PATIA PRO",
        "pro_title": "No solo mires datos. Decide qué hacer.",
        "pro_body": "PATIA explica qué pasó, por qué importa y cuál es la siguiente acción.",
        "closing_title": "Administra hoy. Decide mejor mañana.",
        "closing_body": "Empieza gratis durante 14 días.",
        "closing_price": "Planes desde $199 MXN al mes · Cancela cuando quieras",
        "cta": "Comenzar prueba gratis",
        "sale": "Venta registrada",
        "cash": "Efectivo",
        "stock": "Inventario actualizado",
        "low": "2 productos requieren atención",
        "receive": "Recibir mercancía",
        "expected": "En caja debería haber",
        "counted": "Efectivo contado",
        "difference": "Diferencia",
        "sales": "Ventas",
        "profit": "Utilidad",
        "margin": "Margen",
        "top": "Productos más rentables",
        "alert": "El agua puede agotarse esta semana",
        "evidence": "Quedan 6 piezas y vendes 2 por día.",
        "action": "Preparar reposición",
    },
    "en": {
        "opening_title": "Your business, under control.",
        "opening_body": "Sales, inventory and decisions connected in one place.",
        "sale_eyebrow": "SELL WITHOUT LOSING MOMENTUM",
        "sale_title": "Record every sale in seconds",
        "sale_body": "PATIA updates inventory and keeps a professional receipt.",
        "inventory_eyebrow": "INVENTORY THAT MOVES WITH YOU",
        "inventory_title": "Know what you have and what you need",
        "inventory_body": "Spot low stock and receive goods without losing history.",
        "cash_eyebrow": "CLOSE THE DAY WITH CLARITY",
        "cash_title": "Cash and sales stay connected",
        "cash_body": "Know what should be in the register and spot differences early.",
        "reports_eyebrow": "UNDERSTAND THE BUSINESS",
        "reports_title": "Turn daily operations into answers",
        "reports_body": "Sales, profit and profitable products explained clearly.",
        "pro_eyebrow": "PATIA PRO",
        "pro_title": "Do more than review data. Know what to do.",
        "pro_body": "PATIA explains what happened, why it matters and the next action.",
        "closing_title": "Run today. Decide better tomorrow.",
        "closing_body": "Start free for 14 days.",
        "closing_price": "Plans from MXN $199/month · Cancel anytime",
        "cta": "Start free trial",
        "sale": "Sale recorded",
        "cash": "Cash",
        "stock": "Inventory updated",
        "low": "2 products need attention",
        "receive": "Receive goods",
        "expected": "Expected cash",
        "counted": "Cash counted",
        "difference": "Difference",
        "sales": "Sales",
        "profit": "Profit",
        "margin": "Margin",
        "top": "Most profitable products",
        "alert": "Water may run out this week",
        "evidence": "6 units left and you sell 2 per day.",
        "action": "Prepare restock",
    },
}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return 1 - (1 - value) ** 4


def scene_alpha(local: float) -> float:
    enter = ease(local / 0.65)
    leave = ease((SCENE_SECONDS - local) / 0.55)
    return min(enter, leave)


def rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return (
        int(color[0:2], 16),
        int(color[2:4], 16),
        int(color[4:6], 16),
        alpha,
    )


def text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    value: str,
    size: int,
    color: str = INK,
    *,
    bold: bool = False,
    anchor: str | None = None,
) -> None:
    draw.text(xy, value, font=font(size, bold), fill=rgba(color), anchor=anchor)


def wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: str,
    width: int,
    size: int,
    color: str = MUTED,
    *,
    bold: bool = False,
    spacing: int = 8,
) -> None:
    words = value.split()
    lines: list[str] = []
    current = ""
    face = font(size, bold)
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=face) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    draw.multiline_text(
        xy,
        "\n".join(lines),
        font=face,
        fill=rgba(color),
        spacing=spacing,
    )


def card(
    layer: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int = 24,
    fill: str = SURFACE,
    outline: str = LINE,
    shadow: bool = True,
) -> ImageDraw.ImageDraw:
    if shadow:
        shadow_layer = Image.new("RGBA", layer.size)
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shifted = (box[0] + 4, box[1] + 12, box[2] + 4, box[3] + 12)
        shadow_draw.rounded_rectangle(shifted, radius, fill=(25, 28, 48, 35))
        layer.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(18)))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        box,
        radius,
        fill=rgba(fill),
        outline=rgba(outline),
        width=1,
    )
    return draw


def badge(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: str,
    *,
    fill: str = "#eeebff",
    color: str = PURPLE_DARK,
) -> None:
    face = font(18, True)
    width = int(draw.textlength(value, font=face)) + 34
    draw.rounded_rectangle(
        (xy[0], xy[1], xy[0] + width, xy[1] + 38),
        19,
        fill=rgba(fill),
    )
    draw.text(
        (xy[0] + 17, xy[1] + 19),
        value,
        font=face,
        fill=rgba(color),
        anchor="lm",
    )


def background(frame_index: int) -> Image.Image:
    y = np.linspace(0, 1, HEIGHT, dtype=np.float32)[:, None]
    top = np.array([247, 248, 253], dtype=np.float32)
    bottom = np.array([237, 240, 249], dtype=np.float32)
    rgb = top * (1 - y[..., None]) + bottom * y[..., None]
    rgb = np.repeat(rgb, WIDTH, axis=1)
    image = Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")
    glow = Image.new("RGBA", image.size)
    gd = ImageDraw.Draw(glow)
    drift = math.sin(frame_index / FPS * 0.45) * 35
    gd.ellipse(
        (780 + drift, -220, 1420 + drift, 420),
        fill=(102, 70, 229, 28),
    )
    gd.ellipse((-260 - drift, 430, 380 - drift, 1020), fill=(22, 137, 232, 22))
    return Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(75)))


def brand_mark(layer: Image.Image, *, small: bool = True) -> None:
    logo = Image.open(LOGO_PATH).convert("RGB")
    target = (164, 59) if small else (245, 88)
    logo.thumbnail(target, Image.Resampling.LANCZOS)
    layer.paste(logo, (54, 42), logo.convert("RGBA"))


def browser_shell(
    layer: Image.Image,
    box: tuple[int, int, int, int],
    title_value: str,
) -> tuple[ImageDraw.ImageDraw, tuple[int, int, int, int]]:
    draw = card(layer, box, radius=24, outline="#daddE8")
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(
        (x1, y1, x2, y1 + 54),
        24,
        fill=rgba("#fbfcfe"),
        outline=rgba("#daddE8"),
    )
    draw.rectangle((x1, y1 + 28, x2, y1 + 54), fill=rgba("#fbfcfe"))
    for index, color in enumerate(("#f07878", "#efbd5b", "#55bd91")):
        draw.ellipse(
            (x1 + 24 + index * 20, y1 + 22, x1 + 34 + index * 20, y1 + 32),
            fill=rgba(color),
        )
    text(draw, ((x1 + x2) / 2, y1 + 27), title_value, 15, MUTED, bold=True, anchor="mm")
    return draw, (x1 + 24, y1 + 74, x2 - 24, y2 - 24)


def scene_header(
    layer: Image.Image,
    copy: dict[str, str],
    prefix: str,
    progress: float,
) -> None:
    draw = ImageDraw.Draw(layer)
    offset = int((1 - ease(progress / 0.8)) * 28)
    badge(draw, (64, 166 + offset), copy[f"{prefix}_eyebrow"])
    wrapped(
        draw,
        (64, 222 + offset),
        copy[f"{prefix}_title"],
        485,
        42,
        INK,
        bold=True,
        spacing=2,
    )
    wrapped(
        draw,
        (64, 340 + offset),
        copy[f"{prefix}_body"],
        455,
        24,
        MUTED,
        spacing=7,
    )


def opening(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(layer)
    logo = Image.open(LOGO_PATH).convert("RGB")
    logo.thumbnail((290, 105), Image.Resampling.LANCZOS)
    x = (WIDTH - logo.width) // 2
    layer.paste(logo, (x, 112), logo.convert("RGBA"))
    reveal = ease(max(0, local - 0.35) / 1.0)
    y_offset = int((1 - reveal) * 24)
    text(draw, (WIDTH / 2, 312 + y_offset), copy["opening_title"], 58, INK, bold=True, anchor="mm")
    wrapped(draw, (335, 374 + y_offset), copy["opening_body"], 610, 27, MUTED, spacing=8)
    line_width = int(330 * ease(max(0, local - 1.1) / 1.0))
    draw.rounded_rectangle(
        (WIDTH // 2 - line_width // 2, 482, WIDTH // 2 + line_width // 2, 488),
        3,
        fill=rgba(PURPLE),
    )
    text(draw, (WIDTH / 2, 555), "PATIA", 18, PURPLE, bold=True, anchor="mm")
    return layer


def sale(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    brand_mark(layer)
    scene_header(layer, copy, "sale", local)
    draw, content = browser_shell(layer, (560, 92, 1216, 628), "Punto de venta")
    x1, y1, x2, y2 = content
    draw.rounded_rectangle((x1, y1, x1 + 345, y2), 16, fill=rgba(SOFT))
    products = (("Agua mineral", "$18.00"), ("Café 500 g", "$92.00"), ("Pan integral", "$48.00"))
    for index, (name, price) in enumerate(products):
        y = y1 + 20 + index * 88
        draw.rounded_rectangle((x1 + 16, y, x1 + 329, y + 70), 14, fill=rgba(SURFACE), outline=rgba(LINE))
        draw.ellipse((x1 + 30, y + 18, x1 + 64, y + 52), fill=rgba("#e9e5ff"))
        text(draw, (x1 + 80, y + 22), name, 18, INK, bold=True)
        text(draw, (x1 + 80, y + 47), price, 16, MUTED)
    cart_x = x1 + 367
    text(draw, (cart_x, y1 + 8), "Ticket", 20, INK, bold=True)
    text(draw, (cart_x, y1 + 54), "Agua mineral × 2", 17, MUTED)
    text(draw, (x2 - 4, y1 + 54), "$36.00", 17, INK, bold=True, anchor="ra")
    text(draw, (cart_x, y1 + 97), "Pan integral × 1", 17, MUTED)
    text(draw, (x2 - 4, y1 + 97), "$48.00", 17, INK, bold=True, anchor="ra")
    draw.line((cart_x, y1 + 135, x2, y1 + 135), fill=rgba(LINE), width=2)
    text(draw, (cart_x, y1 + 160), "TOTAL", 17, MUTED, bold=True)
    text(draw, (x2, y1 + 157), "$84.00", 31, INK, bold=True, anchor="ra")
    button_fill = GREEN if local > 3.2 else PURPLE
    draw.rounded_rectangle((cart_x, y1 + 222, x2, y1 + 278), 14, fill=rgba(button_fill))
    text(draw, ((cart_x + x2) / 2, y1 + 250), copy["sale"] if local > 3.2 else "Cobrar", 18, "#ffffff", bold=True, anchor="mm")
    if local > 3.2:
        draw.ellipse((x2 - 52, y1 + 294, x2 - 14, y1 + 332), fill=rgba("#e1f6ed"))
        text(draw, (x2 - 33, y1 + 313), "✓", 21, GREEN, bold=True, anchor="mm")
        text(draw, (cart_x, y1 + 304), copy["stock"], 16, GREEN, bold=True)
    return layer


def inventory(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    brand_mark(layer)
    scene_header(layer, copy, "inventory", local)
    draw, content = browser_shell(layer, (560, 92, 1216, 628), "Inventario")
    x1, y1, x2, y2 = content
    badge(draw, (x1, y1), copy["low"], fill="#fff0e7", color=AMBER)
    headers = ("Producto", "Stock", "Mínimo", "Estado")
    positions = (x1, x1 + 270, x1 + 370, x1 + 470)
    for label, x in zip(headers, positions):
        text(draw, (x, y1 + 74), label, 15, MUTED, bold=True)
    rows = (
        ("Agua mineral", "6", "12", "Bajo"),
        ("Café 500 g", "18", "8", "En orden"),
        ("Pan integral", "4", "10", "Bajo"),
    )
    for index, row in enumerate(rows):
        y = y1 + 112 + index * 72
        draw.rounded_rectangle((x1 - 8, y - 12, x2, y + 45), 12, fill=rgba("#fbfcfe" if index != 0 else "#fff8f4"))
        for value, x in zip(row, positions):
            color = AMBER if value == "Bajo" else (GREEN if value == "En orden" else INK)
            text(draw, (x, y), value, 17, color, bold=value in {"Bajo", "En orden"})
    pulse = 1 + math.sin(local * 4) * 0.025
    bx1, by1, bx2, by2 = x1 + 340, y2 - 64, x2, y2 - 8
    center_x = (bx1 + bx2) / 2
    half = (bx2 - bx1) * pulse / 2
    draw.rounded_rectangle((center_x - half, by1, center_x + half, by2), 14, fill=rgba(PURPLE))
    text(draw, (center_x, (by1 + by2) / 2), copy["receive"], 18, "#ffffff", bold=True, anchor="mm")
    return layer


def cash_scene(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    brand_mark(layer)
    scene_header(layer, copy, "cash", local)
    draw, content = browser_shell(layer, (560, 92, 1216, 628), "Caja del día")
    x1, y1, x2, y2 = content
    cards = (
        (copy["expected"], "$3,480", PURPLE),
        (copy["counted"], "$3,480", BLUE),
        (copy["difference"], "$0.00", GREEN),
    )
    for index, (label, value, accent) in enumerate(cards):
        left = x1 + index * 196
        right = left + 178
        draw.rounded_rectangle((left, y1, right, y1 + 126), 16, fill=rgba(SOFT), outline=rgba(LINE))
        draw.rounded_rectangle((left, y1, left + 6, y1 + 126), 3, fill=rgba(accent))
        text(draw, (left + 18, y1 + 24), label, 15, MUTED, bold=True)
        text(draw, (left + 18, y1 + 64), value, 26, INK, bold=True)
    text(draw, (x1, y1 + 174), "Movimientos de hoy", 20, INK, bold=True)
    movements = (("Venta TKT-000042", "+ $84.00"), ("Venta TKT-000043", "+ $126.00"), ("Retiro documentado", "− $500.00"))
    for index, (label, value) in enumerate(movements):
        y = y1 + 220 + index * 60
        draw.line((x1, y + 42, x2, y + 42), fill=rgba(LINE))
        text(draw, (x1, y), label, 17, MUTED)
        text(draw, (x2, y), value, 17, GREEN if value.startswith("+") else AMBER, bold=True, anchor="ra")
    return layer


def reports(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    brand_mark(layer)
    scene_header(layer, copy, "reports", local)
    draw, content = browser_shell(layer, (560, 92, 1216, 628), "Reportes")
    x1, y1, x2, y2 = content
    metrics = ((copy["sales"], "$18,430"), (copy["profit"], "$7,280"), (copy["margin"], "39.5%"))
    for index, (label, value) in enumerate(metrics):
        left = x1 + index * 196
        draw.rounded_rectangle((left, y1, left + 178, y1 + 100), 14, fill=rgba(SOFT), outline=rgba(LINE))
        text(draw, (left + 16, y1 + 18), label, 15, MUTED, bold=True)
        text(draw, (left + 16, y1 + 50), value, 24, INK, bold=True)
    chart = (x1, y1 + 142, x1 + 350, y2)
    draw.rounded_rectangle(chart, 14, fill=rgba("#fbfcfe"), outline=rgba(LINE))
    points = [(chart[0] + 20 + i * 51, chart[3] - 34 - value) for i, value in enumerate((32, 74, 52, 112, 90, 148, 130))]
    visible = max(2, int(ease(local / 2.6) * len(points)))
    draw.line(points[:visible], fill=rgba(PURPLE), width=5, joint="curve")
    for point in points[:visible]:
        draw.ellipse((point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5), fill=rgba(SURFACE), outline=rgba(PURPLE), width=3)
    list_x = x1 + 378
    text(draw, (list_x, y1 + 148), copy["top"], 17, INK, bold=True)
    for index, (name, amount) in enumerate((("Café 500 g", "$2,480"), ("Agua mineral", "$1,940"), ("Pan integral", "$1,320"))):
        y = y1 + 194 + index * 72
        draw.ellipse((list_x, y, list_x + 34, y + 34), fill=rgba("#ece8ff"))
        text(draw, (list_x + 17, y + 17), str(index + 1), 15, PURPLE, bold=True, anchor="mm")
        text(draw, (list_x + 48, y), name, 16, INK, bold=True)
        text(draw, (x2, y), amount, 16, GREEN, bold=True, anchor="ra")
    return layer


def pro_scene(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    brand_mark(layer)
    scene_header(layer, copy, "pro", local)
    draw, content = browser_shell(layer, (560, 92, 1216, 628), "PATIA Pro")
    x1, y1, x2, y2 = content
    draw.rounded_rectangle((x1, y1, x2, y1 + 220), 18, fill=rgba("#fff8f3"), outline=rgba("#f1d8c8"))
    draw.ellipse((x1 + 22, y1 + 22, x1 + 64, y1 + 64), fill=rgba("#ffe1cf"))
    text(draw, (x1 + 43, y1 + 43), "!", 22, AMBER, bold=True, anchor="mm")
    text(draw, (x1 + 82, y1 + 20), copy["alert"], 21, INK, bold=True)
    wrapped(draw, (x1 + 82, y1 + 60), copy["evidence"], 430, 18, MUTED)
    draw.rounded_rectangle((x1 + 82, y1 + 122, x1 + 300, y1 + 174), 13, fill=rgba(PURPLE))
    text(draw, (x1 + 191, y1 + 148), copy["action"], 17, "#ffffff", bold=True, anchor="mm")
    text(draw, (x1, y1 + 258), "Qué pasó", 16, MUTED, bold=True)
    text(draw, (x1, y1 + 292), "El producto mantiene una salida constante.", 17, INK)
    text(draw, (x1, y1 + 342), "Por qué importa", 16, MUTED, bold=True)
    text(draw, (x1, y1 + 376), "Podrías perder ventas antes de tu próxima compra.", 17, INK)
    return layer


def closing(copy: dict[str, str], local: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(layer)
    logo = Image.open(LOGO_PATH).convert("RGB")
    logo.thumbnail((270, 98), Image.Resampling.LANCZOS)
    layer.paste(logo, ((WIDTH - logo.width) // 2, 86), logo.convert("RGBA"))
    text(draw, (WIDTH / 2, 286), copy["closing_title"], 52, INK, bold=True, anchor="mm")
    text(draw, (WIDTH / 2, 357), copy["closing_body"], 29, MUTED, anchor="mm")
    text(draw, (WIDTH / 2, 405), copy["closing_price"], 20, MUTED, bold=True, anchor="mm")
    pulse = 1 + 0.012 * math.sin(local * 3)
    button_width = 330 * pulse
    draw.rounded_rectangle(
        (WIDTH / 2 - button_width / 2, 475, WIDTH / 2 + button_width / 2, 545),
        18,
        fill=rgba(PURPLE),
    )
    text(draw, (WIDTH / 2, 510), copy["cta"], 22, "#ffffff", bold=True, anchor="mm")
    text(draw, (WIDTH / 2, 615), "patiaapp.com", 18, PURPLE, bold=True, anchor="mm")
    return layer


SCENE_RENDERERS = (opening, sale, inventory, cash_scene, reports, pro_scene, closing)


def render_frame(language: str, frame_index: int) -> np.ndarray:
    current_time = frame_index / FPS
    scene_index = min(int(current_time // SCENE_SECONDS), SCENES - 1)
    local = current_time - scene_index * SCENE_SECONDS
    base = background(frame_index)
    scene = SCENE_RENDERERS[scene_index](COPY[language], local)
    opacity = scene_alpha(local)
    scene.putalpha(
        scene.getchannel("A").point(lambda value: int(value * opacity))
    )
    base.alpha_composite(scene)
    return np.asarray(base.convert("RGB"))


def soundtrack(path: Path) -> None:
    sample_rate = 44100
    total = int(DURATION * sample_rate)
    time = np.arange(total, dtype=np.float64) / sample_rate
    audio = np.zeros(total, dtype=np.float64)
    progression = ((220.00, 277.18, 329.63), (196.00, 246.94, 293.66), (174.61, 220.00, 261.63), (196.00, 246.94, 329.63))
    for beat in np.arange(0, DURATION, 0.5):
        chord = progression[int(beat // 4) % len(progression)]
        start = int(beat * sample_rate)
        length = min(int(1.15 * sample_rate), total - start)
        envelope = np.exp(-np.arange(length) / sample_rate * 3.4)
        tone_time = np.arange(length) / sample_rate
        note = chord[int((beat * 2) % len(chord))]
        pluck = np.sin(2 * np.pi * note * tone_time)
        pluck += 0.28 * np.sin(2 * np.pi * note * 2 * tone_time)
        audio[start : start + length] += 0.027 * pluck * envelope
    for scene_start in range(0, DURATION, SCENE_SECONDS):
        start = int(scene_start * sample_rate)
        length = min(int(0.8 * sample_rate), total - start)
        tone_time = np.arange(length) / sample_rate
        sweep = np.sin(2 * np.pi * (420 + 520 * tone_time) * tone_time)
        envelope = np.sin(np.pi * np.arange(length) / max(length, 1)) ** 2
        audio[start : start + length] += 0.018 * sweep * envelope
    fade = int(1.2 * sample_rate)
    audio[:fade] *= np.linspace(0, 1, fade)
    audio[-fade:] *= np.linspace(1, 0, fade)
    audio = np.clip(audio, -0.85, 0.85)
    pcm = (audio * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def encode(language: str, ffmpeg: str, audio_path: Path) -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    filename = "patia-demo.mp4" if language == "es" else "patia-demo-en.mp4"
    final_path = OUTPUT / filename
    with tempfile.TemporaryDirectory(prefix=f"patia-demo-{language}-") as temp:
        silent_path = Path(temp) / "silent.mp4"
        writer = imageio_ffmpeg.write_frames(
            str(silent_path),
            (WIDTH, HEIGHT),
            fps=FPS,
            codec="libx264",
            quality=7,
            pix_fmt_in="rgb24",
            output_params=[
                "-preset",
                "medium",
                "-profile:v",
                "high",
                "-level",
                "4.0",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ],
        )
        writer.send(None)
        for frame_index in range(DURATION * FPS):
            writer.send(render_frame(language, frame_index).tobytes())
        writer.close()
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(silent_path),
                "-i",
                str(audio_path),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(final_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return final_path


def main() -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="patia-demo-audio-") as temp:
        audio_path = Path(temp) / "soundtrack.wav"
        soundtrack(audio_path)
        spanish = encode("es", ffmpeg, audio_path)
        english = encode("en", ffmpeg, audio_path)
    for path in (spanish, english):
        print(f"{path.relative_to(ROOT)} {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
