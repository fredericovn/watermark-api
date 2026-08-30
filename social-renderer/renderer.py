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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


ASSET_DIR = Path(os.getenv("ASSET_DIR", "/app/assets"))
LOGO_PATH = ASSET_DIR / "logo.png"
FONT_BODY = ASSET_DIR / "fonts/Poppins-Regular.ttf"
FONT_BODY_BOLD = ASSET_DIR / "fonts/Poppins-SemiBold.ttf"
FONT_TITLE = ASSET_DIR / "fonts/PlayfairDisplay.ttf"

CREAM = "#F4EFE5"
OLIVE = "#596047"
GOLD = "#C8A96A"
CHARCOAL = "#1D1D1D"
WHITE = "#FFFFFF"

TEMPLATES = {
    "IG_FEED_HERO_V1": (1080, 1350, "EDITORIAL_LIGHT"),
    "FB_FEED_PROPERTY_V1": (1080, 1350, "EDITORIAL_LIGHT"),
    "IG_CAROUSEL_V1": (1080, 1350, "EDITORIAL_LIGHT"),
    "STORY_PROPERTY_V1": (1080, 1920, "PHOTO_IMPACT"),
    "REEL_PROPERTY_V1": (1080, 1920, "PHOTO_IMPACT"),
}

ALLOWED_KEYS = {
    "template_code", "template_version", "template_status", "property_id", "content_id",
    "headline", "subheadline", "cta", "property_code", "show_price",
    "price", "assets", "scenes", "brand_version", "locale",
}


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


def validate_payload(payload: Any, *, video: bool = False) -> dict[str, Any]:
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
    if video and template != "REEL_PROPERTY_V1":
        raise RenderError("O endpoint de vídeo aceita somente REEL_PROPERTY_V1.")
    if not video and template == "REEL_PROPERTY_V1" and payload.get("scenes"):
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
            if not isinstance(scene, dict) or set(scene) - {"asset_order", "caption", "duration_ms"}:
                raise RenderError("Cena inválida.")
            if not isinstance(scene.get("asset_order"), int):
                raise RenderError("asset_order inválido.")
            duration = scene.get("duration_ms", 2800)
            if not isinstance(duration, int) or not 1500 <= duration <= 6000:
                raise RenderError("duration_ms deve ficar entre 1500 e 6000.")
            if len(str(scene.get("caption") or "")) > 70:
                raise RenderError("Legenda de cena excede 70 caracteres.")
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


def render_image(payload: dict[str, Any], image: Image.Image | None = None) -> Rendered:
    data = validate_payload(payload)
    width, height, family = TEMPLATES[data["template_code"]]
    source = image or fetch_image(sorted(data["assets"], key=lambda a: a.get("order", 999))[0]["url"])
    if family == "EDITORIAL_LIGHT":
        canvas = _render_editorial_light(data, source, width, height)
    else:
        canvas = _render_photo_impact(data, source, width, height)
    output = io.BytesIO()
    canvas.save(output, "WEBP", quality=90, method=6)
    return Rendered(output.getvalue(), "image/webp", f"content-{data['content_id']}-{data['template_code'].lower()}.webp", width, height)


def _render_editorial_light(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (width, height), CREAM)
    photo_h = 790
    photo = _fit_cover(source, (width, photo_h), 0.48)
    canvas.paste(photo, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((-260, 690, 630, 1430), fill=CREAM)
    draw.rounded_rectangle((70, 720, 335, 780), radius=30, fill=OLIVE)
    tag_font = _font(FONT_BODY_BOLD, 26)
    draw.text((96, 737), "IMÓVEL SELECIONADO", font=tag_font, fill=WHITE)
    title_font = _font(FONT_TITLE, 67)
    y = 820
    for line in _wrap(draw, data["headline"], title_font, 860, 2):
        draw.text((84, y), line, font=title_font, fill=CHARCOAL)
        y += 78
    sub = str(data.get("subheadline") or "").strip()
    if sub:
        sub_font = _font(FONT_BODY, 31)
        draw.text((88, y + 12), sub, font=sub_font, fill=OLIVE)
        y += 58
    if data.get("show_price"):
        price_font = _font(FONT_BODY_BOLD, 34)
        draw.text((88, y + 12), data["price"], font=price_font, fill=CHARCOAL)
    cta_font = _font(FONT_BODY_BOLD, 27)
    draw.text((88, 1248), str(data.get("cta") or "CONHEÇA ESTE IMÓVEL").upper(), font=cta_font, fill=OLIVE)
    draw.line((88, 1295, 992, 1295), fill=GOLD, width=4)
    code_font = _font(FONT_BODY, 23)
    draw.text((780, 1249), str(data.get("property_code") or f"ID {data['property_id']}").upper(), font=code_font, fill=CHARCOAL)
    logo = _logo(170, 170)
    canvas.paste(logo, (850, 35), logo)
    if data["template_code"] == "IG_CAROUSEL_V1":
        draw.rounded_rectangle((790, 690, 1015, 754), radius=28, fill=GOLD)
        draw.text((823, 708), "DESLIZE  →", font=tag_font, fill=CHARCOAL)
    return canvas


def _render_photo_impact(data: dict[str, Any], source: Image.Image, width: int, height: int) -> Image.Image:
    canvas = _fit_cover(source, (width, height), 0.48).convert("RGBA")
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    alpha = Image.new("L", (1, height))
    alpha.putdata([max(0, min(220, int((y / height) ** 2.2 * 235))) for y in range(height)])
    alpha = alpha.resize((width, height))
    black = Image.new("RGBA", (width, height), (10, 12, 10, 255))
    overlay.paste(black, (0, 0), alpha)
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)
    logo = _logo(210, 210)
    canvas.alpha_composite(logo, (70, 70))
    tag_font = _font(FONT_BODY_BOLD, 28)
    draw.rounded_rectangle((70, 1050, 365, 1118), radius=32, fill=OLIVE)
    draw.text((100, 1068), "EXCLUSIVIDADE VCV", font=tag_font, fill=WHITE)
    title_font = _font(FONT_TITLE, 78)
    y = 1165
    for line in _wrap(draw, data["headline"], title_font, 900, 3):
        draw.text((72, y), line, font=title_font, fill=WHITE, stroke_width=1, stroke_fill=(0, 0, 0, 100))
        y += 92
    sub_font = _font(FONT_BODY, 34)
    sub = str(data.get("subheadline") or "").strip()
    if sub:
        draw.text((76, min(y + 18, 1590)), sub, font=sub_font, fill=CREAM)
    cta_font = _font(FONT_BODY_BOLD, 29)
    draw.rounded_rectangle((72, 1740, 720, 1830), radius=44, fill=GOLD)
    draw.text((112, 1768), str(data.get("cta") or "AGENDE UMA VISITA").upper(), font=cta_font, fill=CHARCOAL)
    code_font = _font(FONT_BODY, 24)
    draw.text((780, 1770), str(data.get("property_code") or f"ID {data['property_id']}").upper(), font=code_font, fill=WHITE)
    return canvas.convert("RGB")


def _srt_time(milliseconds: int) -> str:
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def render_reel_package(payload: dict[str, Any], images: dict[int, Image.Image] | None = None) -> Rendered:
    data = validate_payload(payload, video=True)
    assets = {int(a.get("order", i + 1)): a for i, a in enumerate(data["assets"])}
    resolved = images or {order: fetch_image(asset["url"]) for order, asset in assets.items()}
    with tempfile.TemporaryDirectory(prefix="vcv-render-") as tmp:
        temp = Path(tmp)
        concat_lines: list[str] = []
        subtitles: list[str] = []
        elapsed = 0
        for index, scene in enumerate(data["scenes"], start=1):
            order = scene["asset_order"]
            if order not in resolved:
                raise RenderError(f"Cena {index} referencia asset_order inexistente.")
            scene_payload = dict(data)
            scene_payload["headline"] = str(scene.get("caption") or data["headline"])
            scene_payload["subheadline"] = "" if index > 1 else str(data.get("subheadline") or "")
            frame = _render_photo_impact(scene_payload, resolved[order], 1080, 1920)
            frame_path = temp / f"scene-{index:02}.png"
            frame.save(frame_path, "PNG")
            duration = scene.get("duration_ms", 2800)
            concat_lines.extend([f"file '{frame_path.as_posix()}'", f"duration {duration / 1000:.3f}"])
            caption = str(scene.get("caption") or data["headline"]).strip()
            subtitles.extend([str(index), f"{_srt_time(elapsed)} --> {_srt_time(elapsed + duration)}", caption, ""])
            elapsed += duration
        concat_lines.append(f"file '{frame_path.as_posix()}'")
        concat_path = temp / "concat.txt"
        concat_path.write_text("\n".join(concat_lines), encoding="utf-8")
        srt_path = temp / "captions.srt"
        srt_path.write_text("\n".join(subtitles), encoding="utf-8")
        video_path = temp / "reel.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_path), "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
            "-movflags", "+faststart", "-an", str(video_path),
        ], check=True, timeout=180)
        cover = render_image({**data, "scenes": []}, resolved[data["scenes"][0]["asset_order"]])
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("reel.mp4", video_path.read_bytes())
            archive.writestr("captions.srt", srt_path.read_bytes())
            archive.writestr("cover.webp", cover.body)
            archive.writestr("manifest.json", json.dumps({
                "content_id": data["content_id"], "template_code": data["template_code"],
                "duration_ms": elapsed, "brand_version": data.get("brand_version", "VCV_BRAND_V1"),
            }, ensure_ascii=False, indent=2))
        return Rendered(package.getvalue(), "application/zip", f"content-{data['content_id']}-reel-package.zip", duration_ms=elapsed)
