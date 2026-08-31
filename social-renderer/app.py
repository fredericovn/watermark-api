from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, make_response, request

from renderer import RenderError, render_image, render_reel_package


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024


def _binary_response(rendered):
    response = make_response(rendered.body)
    response.headers["Content-Type"] = rendered.mime_type
    response.headers["Content-Disposition"] = f'inline; filename="{rendered.filename}"'
    response.headers["X-VCV-SHA256"] = rendered.sha256
    if rendered.width:
        response.headers["X-VCV-Width"] = str(rendered.width)
    if rendered.height:
        response.headers["X-VCV-Height"] = str(rendered.height)
    if rendered.duration_ms:
        response.headers["X-VCV-Duration-Ms"] = str(rendered.duration_ms)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return jsonify(status="ok", renderer="vcv-social-renderer", version="2.1.0", publication=False)


@app.post("/v1/render/image")
def image_endpoint():
    return _binary_response(render_image(request.get_json(silent=False)))


@app.post("/v1/render/reel-package")
def reel_endpoint():
    return _binary_response(render_reel_package(request.get_json(silent=False)))


@app.post("/v1/render/vertical-package")
def vertical_endpoint():
    return _binary_response(render_reel_package(request.get_json(silent=False)))


@app.errorhandler(RenderError)
def render_error(error):
    return jsonify(success=False, error=str(error)), 422


@app.errorhandler(413)
def too_large(_error):
    return jsonify(success=False, error="Payload excede o limite de 256 KB."), 413


@app.errorhandler(Exception)
def unexpected(error):
    app.logger.exception("Falha inesperada no renderer: %s", error)
    return jsonify(success=False, error="Falha interna ao renderizar."), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
