from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


ASSET_DIR = Path(os.getenv("ASSET_DIR", "/app/assets"))
LOGO_PATH = ASSET_DIR / "logo.png"
FONT_BODY = ASSET_DIR / "fonts/Poppins-Regular.ttf"
FONT_BODY_BOLD = ASSET_DIR / "fonts/Poppins-SemiBold.ttf"
FONT_TITLE = ASSET_DIR / "fonts/PlayfairDisplay.ttf"

CREAM = "#F3EDE8"
SAND = "#D8C9BA"
SAGE = "#A8B0A3"
OLIVE = "#5D6355"
BROWN = "#5D5246"
GOLD = "#C9A56A"
CHARCOAL = "#232323"
WHITE = "#FFFFFF"

TEMPLATES = {
    "IG_FEED_HERO_V1": (1080, 1350, "FEED"),
    "FB_FEED_PROPERTY_V1": (1080, 1350, "FEED"),
    "IG_CAROUSEL_V1": (1080, 1350, "CAROUSEL_COVER"),
    "STORY_PROPERTY_V1": (1080, 1920, "STORY"),
    "REEL_PROPERTY_V1": (1080, 1920, "REEL"),
    "MARKETPLACE_PACK_V1": (1080, 1350, "MARKETPLACE"),
}

ALLOWED_KEYS = {
    "template_code", "template_version", "template_status", "property_id", "content_id",
    "headline", "subheadline", "cta", "property_code", "show_price",
    "price", "assets", "scenes", "slides", "brand_version", "locale", "music_profile",
}

MUSIC_PROFILES = {"none", "ambient_warm", "modern_soft", "elegant_minimal"}
MOTION_TYPES = {"push_in", "pull_out", "pan_left", "pan_right"}
TRANSITION_TYPES = {"fade", "smoothleft", "smoothright", "circleopen"}


class RenderError(ValueError):
    pass


@dataclass(frozen=True)
class Rendered:
    body: bytes
    mime_type: str
    filename: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def validate_payload(payload: Any, *, video: bool = False, carousel: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RenderError("O corpo deve ser um objeto JSON.")
    unknown = sorted(set(payload) - ALLOWED_KEYS)
    if unknown:
        raise RenderError(f"Campos desconhecidos: {', '.join(unknown)}.")
    template = payload.get("template_code")
    if template not in TEMPLATES:
        raise RenderError("Template inexistente ou não publicado.")
    template_status = str(payload.get("template_status") or "").upper()
    allow_drafts = os.getenv("ALLOW_DRAFT_TEMPLATES", "false").lower() in {"1", "true", "yes"}
    if template_status != "PUBLICADO" and not (allow_drafts and template_status == "RASCUNHO"):
        raise RenderError("Template inexistente ou não publicado.")
    if video and template not in {"REEL_PROPERTY_V1", "STORY_PROPERTY_V1"}:
        raise RenderError("O endpoint de vídeo aceita somente STORY_PROPERTY_V1 ou REEL_PROPERTY_V1.")
    if carousel and template != "IG_CAROUSEL_V1":
        raise RenderError("O endpoint de carrossel aceita somente IG_CAROUSEL_V1.")
    if not video and template in {"REEL_PROPERTY_V1", "STORY_PROPERTY_V1"} and payload.get("scenes"):
        raise RenderError("Use o endpoint de vídeo para renderizar cenas.")
    for key in ("property_id", "content_id"):
        if not isinstance(payload.get(key), int) or payload[key] <= 0:
            raise RenderError(f"{key} inválido.")
    limits = {"headline": 90, "subheadline": 120, "cta": 45, "property_code": 30, "price": 40}
    for key, limit in limits.items():
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or len(value.strip()) > limit):
            raise RenderError(f"{key} excede {limit} caracteres ou é inválido.")
    if not str(payload.get("headline") or "").strip():
        raise RenderError("headline é obrigatório.")
    if payload.get("show_price") is True and not str(payload.get("price") or "").strip():
        raise RenderError("price é obrigatório quando show_price=true.")
    music_profile = str(payload.get("music_profile") or "ambient_warm")
    if music_profile not in MUSIC_PROFILES:
        raise RenderError("music_profile inválido.")
    payload["music_profile"] = music_profile
    assets = payload.get("assets") or []
    if not isinstance(assets, list) or not assets:
        raise RenderError("Informe ao menos uma imagem em assets.")
    # O snapshot pode conter a galeria completa. O renderer usa no máximo as
    # 12 primeiras imagens ordenadas para manter custo, memória e tempo
    # previsíveis sem rejeitar imóveis que possuam uma galeria maior.
    if len(assets) > 12:
        assets = sorted(assets, key=lambda asset: asset.get("order", 999))[:12]
        payload["assets"] = assets
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) - {"url", "order", "alt"}:
            raise RenderError("Asset inválido.")
        if not isinstance(asset.get("url"), str):
            raise RenderError("URL de asset inválida.")
    if video:
        scenes = payload.get("scenes") or []
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 8:
            raise RenderError("Informe de 1 a 8 cenas.")
        for scene in scenes:
            if not isinstance(scene, dict) or set(scene) - {
                "asset_order", "caption", "duration_ms", "motion", "transition"
            }:
                raise RenderError("Cena inválida.")
            if not isinstance(scene.get("asset_order"), int):
                raise RenderError("asset_order inválido.")
            duration = scene.get("duration_ms", 2800)
            if not isinstance(duration, int) or not 1500 <= duration <= 6000:
                raise RenderError("duration_ms deve ficar entre 1500 e 6000.")
            if len(str(scene.get("caption") or "")) > 70:
                raise RenderError("Legenda de cena excede 70 caracteres.")
            if str(scene.get("motion") or "push_in") not in MOTION_TYPES:
                raise RenderError("Movimento de cena inválido.")
            if str(scene.get("transition") or "fade") not in TRANSITION_TYPES:
                raise RenderError("Transição de cena inválida.")
    if carousel:
        slides = payload.get("slides") or []
        allowed_slide_templates = {
            "cover_hero", "environment_caption", "benefit_split", "property_facts", "cta_final"
        }
        if not isinstance(slides, list) or not 2 <= len(slides) <= 10:
            raise RenderError("Informe de 2 a 10 slides no carrossel.")
        for slide in slides:
            if not isinstance(slide, dict) or set(slide) - {"asset_order", "title", "text", "slide_template"}:
                raise RenderError("Slide inválido.")
            if not isinstance(slide.get("asset_order"), int):
                raise RenderError("asset_order do slide é inválido.")
            if str(slide.get("slide_template") or "environment_caption") not in allowed_slide_templates:
                raise RenderError("Template de slide inválido.")
            if len(str(slide.get("title") or "")) > 70 or len(str(slide.get("text") or "")) > 240:
                raise RenderError("Texto do slide excede o limite.")
    return payload


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in os.getenv("SOURCE_IMAGE_HOSTS", "").split(",") if h.strip()}


def fetch_image(url: str) -> Image.Image:
    parsed = urlparse(url)
    hosts = _allowed_hosts()
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in hosts:
        raise RenderError("Host de imagem não autorizado.")
    limit = int(os.getenv("MAX_SOURCE_BYTES", "26214400"))
    with requests.get(url, stream=True, timeout=(5, 30), allow_redirects=False) as response:
        response.raise_for_status()
        if int(response.headers.get("content-length", "0") or 0) > limit:
            raise RenderError("Imagem excede o limite configurado.")
        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise RenderError("Tipo de imagem não autorizado.")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise RenderError("Imagem excede o limite configurado.")
    image = Image.open(io.BytesIO(data))
    image.load()
    return ImageOps.exif_transpose(image).convert("RGB")


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _fit_cover(image: Image.Image, size: tuple[int, int], focus_y: float = 0.5) -> Image.Image:
    sw, sh = image.size
    tw, th = size
    scale = max(tw / sw, th / sh)
    resized = image.resize((math.ceil(sw * scale), math.ceil(sh * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - tw) // 2)
    top = max(0, min(resized.height - th, round((resized.height - th) * focus_y)))
    return resized.crop((left, top, left + tw, top + th))


def _logo(max_width: int, max_height: int) -> Image.Image:
    logo = Image.open(LOGO_PATH).convert("RGBA")
    bbox = logo.getchannel("A").point(lambda v: 255 if v >= 8 else 0).getbbox()
    if bbox:
        logo = logo.crop(bbox)
    logo.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return logo


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
    words = text.strip().split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines - 1:
                break
    if current and len(lines) < max_lines:
        remaining = " ".join(words[len(" ".join(lines + [current]).split()):])
        line = f"{current} {remaining}".strip()
        while draw.textlength(line + "…", font=font) > width and " " in line:
            line = line.rsplit(" ", 1)[0]
        lines.append(line + ("…" if line != f"{current} {remaining}".strip() else ""))
    return lines


def _fit_single_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    max_size: int,
    min_size: int,
    max_width: int,
) -> tuple[str, ImageFont.FreeTypeFont]:
    """Dimensiona uma linha sem permitir que ela ultrapasse sua área."""
    value = " ".join(text.strip().split())
    for size in range(max_size, min_size - 1, -1):
        font = _font(font_path, size)
        if draw.textlength(value, font=font) <= max_width:
            return value, font
    font = _font(font_path, min_size)
    suffix = "…"
    while value and draw.textlength(value + suffix, font=font) > max_width:
        value = value[:-1].rstrip()
    return value + suffix, font


def _fit_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    max_size: int,
    min_size: int,
    max_width: int,
    max_height: int,
    max_lines: int,
) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """Ajusta fonte e entrelinha até o bloco caber em largura e altura."""
    for size in range(max_size, min_size - 1, -2):
        font = _font(font_path, size)
        lines = _wrap(draw, text, font, max_width, max_lines)
        line_height = round(size * 1.18)
        if lines and len(lines) * line_height <= max_height:
            return lines, font, line_height
    font = _font(font_path, min_size)
    return _wrap(draw, text, font, max_width, max_lines), font, round(min_size * 1.18)


def render_image(payload: dict[str, Any], image: Image.Image | None = None) -> Rendered:
    data = validate_payload(payload)
    width, height, family = TEMPLATES[data["template_code"]]
    source = image or fetch_image(sorted(data["assets"], key=lambda a: a.get("order", 999))[0]["url"])
    renderers = {
        "FEED": _render_feed,
        "CAROUSEL_COVER": _render_carousel_cover,
        "STORY": _render_story,
        "REEL": _render_reel_frame,
        "MARKETPLACE": _render_marketplace,
    }
    canvas = renderers[family](data, source, width, height)
    output = io.BytesIO()
    canvas.save(output, "WEBP", quality=90, method=6)
    return Rendered(output.getvalue(), "image/webp", f"content-{data['content_id']}-{data['template_code'].lower()}.webp", width, height)


def _paste_logo(canvas: Image.Image, position: tuple[int, int], size: int = 155) -> None:
    logo = _logo(size, size)
    if canvas.mode == "RGBA":
        canvas.alpha_composite(logo, position)
    else:
        canvas.paste(logo, position, logo)


def _draw_footer(draw: ImageDraw.ImageDraw, data: dict[str, Any], y: int, width: int) -> None:
    code, code_font = _fit_single_line(
        draw, str(data.get("property_code") or f"ID {data['property_id']}").upper(),
        FONT_BODY, 24, 18, 330,
    )
    cta, cta_font = _fit_single_line(
        draw, str(data.get("cta") or "AGENDE UMA VISITA").upper(),
        FONT_BODY_BOLD, 25, 18, 500,
    )
    draw.text((72, y), cta, font=cta_font, fill=BROWN)
    code_width = draw.textlength(code, font=code_font)
    draw.text((width - 72 - code_width, y + 2), code, font=code_font, fill=BROWN)


def _render_feed(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (width, height), CREAM)
    photo_h = 760
    photo = _fit_cover(source, (width, photo_h), 0.48)
    canvas.paste(photo, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((690, -430, 1370, 300), fill=SAGE)
    draw.ellipse((-320, 650, 520, 1440), fill=CREAM)
    draw.rounded_rectangle((64, 706, 390, 770), radius=32, fill=SAGE)
    tag_font = _font(FONT_BODY_BOLD, 25)
    draw.text((92, 724), "IMÓVEL À VENDA", font=tag_font, fill=BROWN)
    title_lines, title_font, title_line_height = _fit_wrapped_text(
        draw, data["headline"], FONT_BODY_BOLD, 64, 44, 890, 190, 3
    )
    y = 814
    for line in title_lines:
        draw.text((72, y), line, font=title_font, fill=BROWN)
        y += title_line_height
    sub = str(data.get("subheadline") or "").strip()
    if sub:
        sub_text, sub_font = _fit_single_line(draw, sub, FONT_BODY, 31, 22, 900)
        draw.text((76, y + 14), sub_text, font=sub_font, fill=BROWN)
        y += 62
    if data.get("show_price"):
        price_text, price_font = _fit_single_line(draw, data["price"], FONT_BODY_BOLD, 36, 24, 500)
        draw.text((76, y + 10), price_text, font=price_font, fill=BROWN)
    draw.line((72, 1236, width - 72, 1236), fill=GOLD, width=4)
    _draw_footer(draw, data, 1268, width)
    _paste_logo(canvas, (56, 42), 150)
    return canvas


def _render_carousel_cover(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = _fit_cover(source, (width, height), 0.48)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle((0, 730, width, height), fill=(*ImageColor.getrgb(CREAM), 249))
    draw.rectangle((0, 730, width, 742), fill=(*ImageColor.getrgb(GOLD), 255))
    draw.rounded_rectangle((64, 690, 350, 754), radius=32, fill=(*ImageColor.getrgb(SAGE), 255))
    tag_font = _font(FONT_BODY_BOLD, 23)
    draw.text((94, 708), "IMÓVEL À VENDA", font=tag_font, fill=BROWN)
    lines, font, line_height = _fit_wrapped_text(draw, data["headline"], FONT_BODY_BOLD, 58, 42, 900, 176, 3)
    y = 812
    for line in lines:
        draw.text((72, y), line, font=font, fill=BROWN)
        y += line_height
    sub = str(data.get("subheadline") or "").strip()
    if sub:
        sub_lines, sub_font, sub_height = _fit_wrapped_text(draw, sub, FONT_BODY, 29, 22, 900, 96, 2)
        y += 14
        for line in sub_lines:
            draw.text((76, y), line, font=sub_font, fill=BROWN)
            y += sub_height
    if data.get("show_price"):
        price_text, price_font = _fit_single_line(draw, data["price"], FONT_BODY_BOLD, 34, 23, 500)
        draw.text((76, 1112), price_text, font=price_font, fill=BROWN)
    draw.rounded_rectangle((712, 1150, 1008, 1216), radius=33, fill=(*ImageColor.getrgb(GOLD), 255))
    draw.text((770, 1168), "VEJA OS DETALHES  ›", font=tag_font, fill=BROWN)
    code, code_font = _fit_single_line(draw, str(data.get("property_code") or "").upper(), FONT_BODY, 22, 17, 420)
    draw.text((72, 1270), code, font=code_font, fill=BROWN)
    return canvas.convert("RGB")


def _draw_carousel_number(draw: ImageDraw.ImageDraw, index: int, total: int) -> None:
    font = _font(FONT_BODY_BOLD, 20)
    label = f"{index:02} / {total:02}"
    draw.rounded_rectangle((64, 56, 194, 102), radius=23, fill=SAGE)
    draw.text((84, 67), label, font=font, fill=BROWN)


def _render_carousel_environment(data: dict[str, Any], source: Image.Image, index: int, total: int) -> Image.Image:
    canvas = Image.new("RGBA", (1080, 1350), CREAM)
    canvas.paste(_fit_cover(source, (1080, 920), 0.48), (0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle((0, 920, 1080, 1350), fill=(*ImageColor.getrgb(CREAM), 255))
    draw.rectangle((64, 920, 180, 930), fill=(*ImageColor.getrgb(GOLD), 255))
    _draw_carousel_number(draw, index, total)
    lines, font, line_height = _fit_wrapped_text(draw, data["headline"], FONT_BODY_BOLD, 52, 36, 900, 132, 2)
    y = 978
    for line in lines:
        draw.text((72, y), line, font=font, fill=BROWN)
        y += line_height
    text = str(data.get("subheadline") or "").strip()
    if text:
        body_lines, body_font, body_height = _fit_wrapped_text(draw, text, FONT_BODY, 29, 22, 900, 108, 3)
        y += 12
        for line in body_lines:
            draw.text((76, y), line, font=body_font, fill=BROWN)
            y += body_height
    code, code_font = _fit_single_line(draw, str(data.get("property_code") or "").upper(), FONT_BODY, 20, 16, 390)
    draw.text((72, 1290), code, font=code_font, fill=BROWN)
    return canvas.convert("RGB")


def _render_carousel_benefit(data: dict[str, Any], source: Image.Image, index: int, total: int) -> Image.Image:
    canvas = Image.new("RGB", (1080, 1350), CREAM)
    canvas.paste(_fit_cover(source, (1080, 790), 0.48), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((720, 650, 1250, 1160), fill=SAGE)
    _draw_carousel_number(draw, index, total)
    lines, font, line_height = _fit_wrapped_text(draw, data["headline"], FONT_TITLE, 64, 42, 850, 190, 3)
    y = 850
    for line in lines:
        draw.text((72, y), line, font=font, fill=BROWN)
        y += line_height
    body = str(data.get("subheadline") or "").strip()
    if body:
        body_lines, body_font, body_height = _fit_wrapped_text(draw, body, FONT_BODY, 30, 22, 850, 125, 3)
        y += 16
        for line in body_lines:
            draw.text((76, y), line, font=body_font, fill=CHARCOAL)
            y += body_height
    draw.line((72, 1260, 1008, 1260), fill=GOLD, width=4)
    return canvas


def _render_carousel_facts(data: dict[str, Any], source: Image.Image, index: int, total: int) -> Image.Image:
    canvas = Image.new("RGB", (1080, 1350), CREAM)
    canvas.paste(_fit_cover(source, (1080, 650), 0.48), (0, 0))
    draw = ImageDraw.Draw(canvas)
    _draw_carousel_number(draw, index, total)
    draw.rounded_rectangle((56, 590, 1024, 1280), radius=54, fill=CREAM, outline=SAND, width=3)
    lines, font, line_height = _fit_wrapped_text(draw, data["headline"], FONT_BODY_BOLD, 58, 40, 850, 165, 3)
    y = 690
    for line in lines:
        draw.text((104, y), line, font=font, fill=BROWN)
        y += line_height
    body = str(data.get("subheadline") or data.get("subheadline") or "").strip()
    body_lines, body_font, body_height = _fit_wrapped_text(draw, body, FONT_BODY, 32, 23, 850, 245, 6)
    y += 28
    for line in body_lines:
        draw.text((108, y), line, font=body_font, fill=CHARCOAL)
        y += body_height
    _draw_footer(draw, data, 1210, 1080)
    return canvas


def _render_carousel_cta(data: dict[str, Any], source: Image.Image, index: int, total: int) -> Image.Image:
    canvas = _photo_scrim(source, 1080, 1350)
    draw = ImageDraw.Draw(canvas, "RGBA")
    _draw_carousel_number(draw, index, total)
    draw.rounded_rectangle((64, 640, 1016, 1215), radius=56, fill=(*ImageColor.getrgb(CREAM), 248))
    lines, font, line_height = _fit_wrapped_text(draw, data["headline"], FONT_BODY_BOLD, 58, 40, 820, 165, 3)
    y = 724
    for line in lines:
        draw.text((112, y), line, font=font, fill=BROWN)
        y += line_height
    body = str(data.get("subheadline") or "").strip()
    if body:
        body_lines, body_font, body_height = _fit_wrapped_text(draw, body, FONT_BODY, 29, 21, 820, 96, 3)
        y += 18
        for line in body_lines:
            draw.text((116, y), line, font=body_font, fill=BROWN)
            y += body_height
    cta, cta_font = _fit_single_line(draw, str(data.get("cta") or "FALE COM A VCVARGAS").upper(), FONT_BODY_BOLD, 25, 17, 760)
    draw.rounded_rectangle((112, 1050, 968, 1128), radius=39, fill=(*ImageColor.getrgb(GOLD), 255))
    cta_width = draw.textlength(cta, font=cta_font)
    draw.text(((1080 - cta_width) / 2, 1073), cta, font=cta_font, fill=CHARCOAL)
    code, code_font = _fit_single_line(draw, str(data.get("property_code") or "").upper(), FONT_BODY, 20, 16, 390)
    draw.text((112, 1160), code, font=code_font, fill=BROWN)
    return canvas.convert("RGB")


def _photo_scrim(source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = _fit_cover(source, (width, height), 0.48).convert("RGBA")
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    alpha = Image.new("L", (1, height))
    alpha.putdata([max(0, min(220, int((y / height) ** 2.2 * 235))) for y in range(height)])
    alpha = alpha.resize((width, height))
    black = Image.new("RGBA", (width, height), (10, 12, 10, 255))
    overlay.paste(black, (0, 0), alpha)
    return Image.alpha_composite(canvas, overlay)


def _render_story(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = _photo_scrim(source, width, height)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.ellipse((700, -460, 1510, 390), fill=(*ImageColor.getrgb(SAGE), 238))
    draw.rounded_rectangle((64, 1090, 1016, 1635), radius=64, fill=(*ImageColor.getrgb(CREAM), 238))
    draw.rounded_rectangle((104, 1135, 430, 1202), radius=33, fill=(*ImageColor.getrgb(SAGE), 255))
    tag_font = _font(FONT_BODY_BOLD, 28)
    draw.text((137, 1153), "IMÓVEL À VENDA", font=tag_font, fill=BROWN)
    title_lines, title_font, title_line_height = _fit_wrapped_text(
        draw, data["headline"], FONT_BODY_BOLD, 76, 50, 830, 240, 3
    )
    y = 1250
    for line in title_lines:
        draw.text((106, y), line, font=title_font, fill=BROWN)
        y += title_line_height
    sub = str(data.get("subheadline") or "").strip()
    if sub:
        sub_text, sub_font = _fit_single_line(draw, sub, FONT_BODY, 33, 23, 820)
        draw.text((108, min(y + 18, 1548)), sub_text, font=sub_font, fill=BROWN)
    _paste_logo(canvas, (64, 64), 180)
    _draw_vertical_cta(draw, data, width)
    return canvas.convert("RGB")


def _draw_vertical_cta(draw: ImageDraw.ImageDraw, data: dict[str, Any], width: int) -> None:
    code, code_font = _fit_single_line(
        draw, str(data.get("property_code") or f"ID {data['property_id']}").upper(),
        FONT_BODY, 24, 18, 420,
    )
    code_width = draw.textlength(code, font=code_font)
    draw.text((width - 72 - code_width, 1680), code, font=code_font, fill=WHITE)
    cta_box = (72, 1740, 1008, 1830)
    draw.rounded_rectangle(cta_box, radius=44, fill=GOLD)
    cta_text, cta_font = _fit_single_line(
        draw,
        str(data.get("cta") or "AGENDE UMA VISITA").upper(),
        FONT_BODY_BOLD,
        29,
        19,
        cta_box[2] - cta_box[0] - 80,
    )
    cta_width = draw.textlength(cta_text, font=cta_font)
    cta_bbox = draw.textbbox((0, 0), cta_text, font=cta_font)
    cta_height = cta_bbox[3] - cta_bbox[1]
    draw.text(
        ((width - cta_width) / 2, cta_box[1] + (cta_box[3] - cta_box[1] - cta_height) / 2 - cta_bbox[1]),
        cta_text,
        font=cta_font,
        fill=CHARCOAL,
    )


def _render_reel_frame(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = _photo_scrim(source, width, height)
    draw = ImageDraw.Draw(canvas, "RGBA")
    # Vanessa's best-performing tours keep the room dominant and use a short,
    # contextual caption instead of a large information card over the image.
    lines, font, line_height = _fit_wrapped_text(
        draw, data["headline"], FONT_BODY_BOLD, 58, 40, 850, 190, 3
    )
    text_height = len(lines) * line_height
    y = 1435 - text_height
    padding_x, padding_y = 38, 26
    widest = max(draw.textlength(line, font=font) for line in lines)
    box_left = max(64, int((width - widest) / 2) - padding_x)
    box_right = min(width - 64, int((width + widest) / 2) + padding_x)
    draw.rounded_rectangle(
        (box_left, y - padding_y, box_right, y + text_height + padding_y),
        radius=34,
        fill=(20, 20, 18, 132),
    )
    for line in lines:
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) / 2
        draw.text((x + 2, y + 3), line, font=font, fill=(0, 0, 0, 170))
        draw.text((x, y), line, font=font, fill=WHITE)
        y += line_height
    _paste_logo(canvas, (width - 200, 58), 140)
    if data.get("_is_final_scene", True):
        _draw_vertical_cta(draw, data, width)
    return canvas.convert("RGB")


def _render_marketplace(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (width, height), CREAM)
    canvas.paste(_fit_cover(source, (width, 850), 0.48), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 850, width, height), fill=CREAM)
    draw.ellipse((760, -390, 1370, 270), fill=SAGE)
    draw.rounded_rectangle((64, 804, 380, 870), radius=33, fill=SAGE)
    tag_font = _font(FONT_BODY_BOLD, 25)
    draw.text((94, 823), "IMÓVEL À VENDA", font=tag_font, fill=BROWN)
    lines, font, line_height = _fit_wrapped_text(draw, data["headline"], FONT_BODY_BOLD, 60, 42, 900, 160, 2)
    y = 918
    for line in lines:
        draw.text((72, y), line, font=font, fill=BROWN)
        y += line_height
    sub = str(data.get("subheadline") or "").strip()
    if sub:
        sub_text, sub_font = _fit_single_line(draw, sub, FONT_BODY, 30, 22, 900)
        draw.text((76, y + 14), sub_text, font=sub_font, fill=BROWN)
    if data.get("show_price"):
        price_text, price_font = _fit_single_line(draw, data["price"], FONT_BODY_BOLD, 38, 25, 520)
        draw.text((76, 1160), price_text, font=price_font, fill=BROWN)
    draw.line((72, 1236, width - 72, 1236), fill=GOLD, width=4)
    _draw_footer(draw, data, 1268, width)
    _paste_logo(canvas, (56, 42), 150)
    return canvas


def _srt_time(milliseconds: int) -> str:
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _motion_filter(motion: str, frames: int) -> str:
    common = "scale=1200:2134:force_original_aspect_ratio=increase"
    if motion == "pull_out":
        zoom = "if(eq(on,1),1.08,max(zoom-0.0009,1.0))"
        position = "iw/2-(iw/zoom/2):ih/2-(ih/zoom/2)"
    elif motion == "pan_left":
        zoom = "1.06"
        position = f"(iw-iw/zoom)*(1-on/{frames}):ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        zoom = "1.06"
        position = f"(iw-iw/zoom)*on/{frames}:ih/2-(ih/zoom/2)"
    else:
        zoom = "min(zoom+0.0009,1.08)"
        position = "iw/2-(iw/zoom/2):ih/2-(ih/zoom/2)"
    return (
        f"{common},zoompan=z='{zoom}':x='{position.split(':')[0]}':"
        f"y='{position.split(':')[1]}':d={frames}:s=1080x1920:fps=30,setsar=1"
    )


def _music_source(profile: str, duration_seconds: float) -> str:
    # Pequenos arranjos determinísticos e instrumentais. Cada perfil combina
    # progressão harmônica, arpejo e pulso suave; não usa gravações de terceiros.
    profiles = {
        "ambient_warm": {
            "bpm": 104,
            "chords": ((174.61, 220.00, 261.63), (130.81, 164.81, 220.00),
                       (146.83, 196.00, 246.94), (130.81, 174.61, 220.00)),
            "arp": (349.23, 440.00, 523.25, 440.00, 392.00, 440.00, 523.25, 659.25),
        },
        "modern_soft": {
            "bpm": 116,
            "chords": ((196.00, 246.94, 293.66), (146.83, 196.00, 246.94),
                       (164.81, 220.00, 261.63), (130.81, 164.81, 220.00)),
            "arp": (392.00, 493.88, 587.33, 493.88, 440.00, 523.25, 659.25, 523.25),
        },
        "elegant_minimal": {
            "bpm": 94,
            "chords": ((164.81, 207.65, 246.94), (123.47, 164.81, 207.65),
                       (138.59, 185.00, 220.00), (146.83, 196.00, 246.94)),
            "arp": (329.63, 415.30, 493.88, 415.30, 369.99, 440.00, 554.37, 440.00),
        },
    }
    config = profiles[profile]
    beat = 60.0 / config["bpm"]
    bar = beat * 4
    loop = bar * len(config["chords"])
    pads = []
    for index, chord in enumerate(config["chords"]):
        gate = f"between(mod(t,{loop:.6f}),{index * bar:.6f},{(index + 1) * bar:.6f})"
        tone = "+".join(f"sin(2*PI*{frequency:.2f}*t)" for frequency in chord)
        pads.append(f"{gate}*0.010*({tone})")
    arpeggio = []
    step = beat / 2
    arp_loop = step * len(config["arp"])
    for index, frequency in enumerate(config["arp"]):
        gate = f"between(mod(t,{arp_loop:.6f}),{index * step:.6f},{(index + 1) * step:.6f})"
        envelope = f"exp(-7*mod(t,{step:.6f}))"
        arpeggio.append(f"{gate}*0.024*{envelope}*sin(2*PI*{frequency:.2f}*t)")
    pulse = (
        f"0.020*exp(-15*mod(t,{beat:.6f}))*sin(2*PI*72*t)+"
        f"0.006*exp(-28*mod(t,{beat / 2:.6f}))*sin(2*PI*3200*t)"
    )
    signal = "+".join([*pads, *arpeggio, pulse])
    fade_out = max(0.0, duration_seconds - 1.8)
    return (
        f"aevalsrc='{signal}':s=48000:d={duration_seconds:.3f},"
        "highpass=f=55,lowpass=f=5200,aecho=0.8:0.28:85:0.12,"
        f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.3f}:d=1.8,"
        "loudnorm=I=-23:TP=-3:LRA=8"
    )


def render_carousel_package(payload: dict[str, Any], images: dict[int, Image.Image] | None = None) -> Rendered:
    data = validate_payload(payload, carousel=True)
    assets = {int(asset.get("order", index + 1)): asset for index, asset in enumerate(data["assets"])}
    resolved = images or {order: fetch_image(asset["url"]) for order, asset in assets.items()}
    slides = data["slides"]
    first_title = str(slides[0].get("title") or "").strip().casefold()
    package = io.BytesIO()
    manifest_slides: list[dict[str, Any]] = []
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, slide in enumerate(slides, start=1):
            order = slide["asset_order"]
            if order not in resolved:
                raise RenderError(f"Slide {index} referencia asset_order inexistente.")
            slide_template = str(slide.get("slide_template") or "environment_caption")
            slide_data = dict(data)
            slide_data["headline"] = str(slide.get("title") or data["headline"])
            slide_data["subheadline"] = str(slide.get("text") or "")
            if slide_template == "cta_final" and slide_data["headline"].strip().casefold() == first_title:
                slide_data["headline"] = "Agende sua visita"
                slide_data["subheadline"] = str(data.get("cta") or "Fale com a equipe VCVargas para conhecer este imóvel.")
            if slide_template == "cover_hero":
                canvas = _render_carousel_cover(slide_data, resolved[order], 1080, 1350)
            elif slide_template == "benefit_split":
                canvas = _render_carousel_benefit(slide_data, resolved[order], index, len(slides))
            elif slide_template == "property_facts":
                canvas = _render_carousel_facts(slide_data, resolved[order], index, len(slides))
            elif slide_template == "cta_final":
                canvas = _render_carousel_cta(slide_data, resolved[order], index, len(slides))
            else:
                canvas = _render_carousel_environment(slide_data, resolved[order], index, len(slides))
            output = io.BytesIO()
            canvas.save(output, "WEBP", quality=90, method=6)
            filename = f"slide-{index:02}.webp"
            archive.writestr(filename, output.getvalue())
            manifest_slides.append({
                "order": index, "filename": filename, "asset_order": order,
                "slide_template": slide_template, "title": slide_data["headline"],
            })
        archive.writestr("manifest.json", json.dumps({
            "content_id": data["content_id"], "template_code": data["template_code"],
            "brand_version": data.get("brand_version", "VCV_BRAND_V1"),
            "slide_count": len(slides), "slides": manifest_slides,
        }, ensure_ascii=False, indent=2))
    return Rendered(
        package.getvalue(), "application/zip", f"content-{data['content_id']}-carousel-package.zip",
        width=1080, height=1350,
    )


def render_reel_package(payload: dict[str, Any], images: dict[int, Image.Image] | None = None) -> Rendered:
    data = validate_payload(payload, video=True)
    assets = {int(a.get("order", i + 1)): a for i, a in enumerate(data["assets"])}
    resolved = images or {order: fetch_image(asset["url"]) for order, asset in assets.items()}
    with tempfile.TemporaryDirectory(prefix="vcv-render-") as tmp:
        temp = Path(tmp)
        frame_paths: list[Path] = []
        durations: list[float] = []
        subtitles: list[str] = []
        elapsed = 0
        for index, scene in enumerate(data["scenes"], start=1):
            order = scene["asset_order"]
            if order not in resolved:
                raise RenderError(f"Cena {index} referencia asset_order inexistente.")
            scene_payload = dict(data)
            scene_payload["headline"] = str(scene.get("caption") or data["headline"])
            scene_payload["subheadline"] = "" if index > 1 else str(data.get("subheadline") or "")
            scene_payload["_is_final_scene"] = index == len(data["scenes"])
            frame = _render_reel_frame(scene_payload, resolved[order], 1080, 1920)
            frame_path = temp / f"scene-{index:02}.png"
            frame.save(frame_path, "PNG")
            frame_paths.append(frame_path)
            duration = scene.get("duration_ms", 2800)
            durations.append(duration / 1000)
            caption = str(scene.get("caption") or data["headline"]).strip()
            subtitles.extend([str(index), f"{_srt_time(elapsed)} --> {_srt_time(elapsed + duration)}", caption, ""])
            elapsed += duration
        srt_path = temp / "captions.srt"
        srt_path.write_text("\n".join(subtitles), encoding="utf-8")
        video_path = temp / "reel.mp4"
        transition_seconds = 0.45 if len(frame_paths) > 1 else 0.0
        ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for frame_path, duration in zip(frame_paths, durations):
            ffmpeg.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(frame_path)])

        total_seconds = sum(durations) - transition_seconds * max(0, len(durations) - 1)
        music_profile = data.get("music_profile", "ambient_warm")
        if music_profile != "none":
            ffmpeg.extend(["-f", "lavfi", "-i", _music_source(music_profile, total_seconds)])

        filters: list[str] = []
        for index, (scene, duration) in enumerate(zip(data["scenes"], durations)):
            frames = max(1, round(duration * 30))
            motion = str(scene.get("motion") or ("push_in" if index % 2 == 0 else "pull_out"))
            filters.append(f"[{index}:v]{_motion_filter(motion, frames)},format=yuv420p[v{index}]")

        final_video = "v0"
        cumulative = durations[0]
        for index in range(1, len(durations)):
            output_label = f"vx{index}"
            transition = str(data["scenes"][index].get("transition") or "fade")
            offset = cumulative - transition_seconds * index
            filters.append(
                f"[{final_video}][v{index}]xfade=transition={transition}:"
                f"duration={transition_seconds:.2f}:offset={offset:.3f}[{output_label}]"
            )
            final_video = output_label
            cumulative += durations[index]

        ffmpeg.extend(["-filter_complex", ";".join(filters), "-map", f"[{final_video}]"])
        if music_profile != "none":
            ffmpeg.extend(["-map", f"{len(frame_paths)}:a", "-c:a", "aac", "-b:a", "160k"])
        else:
            ffmpeg.append("-an")
        ffmpeg.extend([
            "-c:v", "libx264", "-preset", "medium", "-r", "30", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-shortest", str(video_path),
        ])
        subprocess.run(ffmpeg, check=True, timeout=240)
        cover = render_image({**data, "scenes": []}, resolved[data["scenes"][0]["asset_order"]])
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("reel.mp4", video_path.read_bytes())
            archive.writestr("captions.srt", srt_path.read_bytes())
            archive.writestr("cover.webp", cover.body)
            archive.writestr("manifest.json", json.dumps({
                "content_id": data["content_id"], "template_code": data["template_code"],
                "duration_ms": round(total_seconds * 1000),
                "brand_version": data.get("brand_version", "VCV_BRAND_V1"),
                "music_profile": music_profile,
                "transition_seconds": transition_seconds,
            }, ensure_ascii=False, indent=2))
        return Rendered(
            package.getvalue(), "application/zip", f"content-{data['content_id']}-reel-package.zip",
            duration_ms=round(total_seconds * 1000),
        )
