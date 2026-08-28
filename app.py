from __future__ import annotations

import io
import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener


register_heif_opener()

APP_DIR = Path(__file__).resolve().parent
LOGO_PATH = APP_DIR / "logo.png"
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def error_response(message: str, status: int):
    return jsonify(success=False, error=message), status


@app.get("/health")
def health():
    return jsonify(status="ok", heic=True, output="webp")


@app.post("/watermark")
def watermark():
    uploaded = request.files.get("foto")
    if uploaded is None or not uploaded.filename:
        return error_response("Envie a imagem no campo 'foto'.", 400)

    mime_type = (uploaded.mimetype or "").lower()
    if mime_type and mime_type not in ALLOWED_MIME_TYPES:
        return error_response(f"Formato não suportado: {mime_type}.", 415)

    try:
        with Image.open(uploaded.stream) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            photo = oriented.convert("RGBA")

        with Image.open(LOGO_PATH) as source_logo:
            logo = source_logo.convert("RGBA")

        alpha = logo.getchannel("A")
        visible_mask = alpha.point(lambda value: 255 if value >= 8 else 0)
        visible_bbox = visible_mask.getbbox()
        if visible_bbox:
            left, top, right, bottom = visible_bbox
            padding = max(1, round(max(right - left, bottom - top) * 0.05))
            logo = logo.crop(
                (
                    max(0, left - padding),
                    max(0, top - padding),
                    min(logo.width, right + padding),
                    min(logo.height, bottom + padding),
                )
            )

        max_logo_width = max(1, round(photo.width * 0.18))
        max_logo_height = max(1, round(photo.height * 0.18))
        logo.thumbnail((max_logo_width, max_logo_height), Image.Resampling.LANCZOS)

        position = (
            max(0, (photo.width - logo.width) // 2),
            max(0, (photo.height - logo.height) // 2),
        )
        photo.alpha_composite(logo, dest=position)

        output = io.BytesIO()
        photo.convert("RGB").save(output, format="WEBP", quality=85, method=6)
        output.seek(0)

        return send_file(
            output,
            mimetype="image/webp",
            download_name="watermarked.webp",
            as_attachment=False,
            max_age=0,
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        app.logger.warning(
            "Imagem recusada: filename=%s mime=%s error=%s",
            uploaded.filename,
            mime_type,
            exc,
        )
        return error_response("Não foi possível decodificar a imagem enviada.", 422)
    except Exception:
        app.logger.exception("Falha inesperada ao aplicar marca d'água.")
        return error_response("Falha interna ao processar a imagem.", 500)


@app.errorhandler(413)
def file_too_large(_error):
    return error_response("A imagem excede o limite de 25 MB.", 413)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=5000)
