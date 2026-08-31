from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renderer import (  # noqa: E402
    FONT_BODY,
    RenderError,
    _fit_single_line,
    render_image,
    render_reel_package,
    validate_payload,
)


def sample_payload(template="IG_FEED_HERO_V1"):
    return {
        "template_code": template,
        "template_version": 1,
        "template_status": "PUBLICADO",
        "property_id": 98,
        "content_id": 15,
        "headline": "Apartamento no Edifício Millenium",
        "subheadline": "3 suítes • 2 vagas • Birigui/SP",
        "cta": "Agende uma visita",
        "property_code": "APTO MILLENIUM 1",
        "show_price": False,
        "assets": [{"url": "https://images.example.test/photo.webp", "order": 1}],
        "brand_version": "VCV_BRAND_V1",
    }


def photo():
    image = Image.new("RGB", (1600, 1200), "#B8C1B1")
    for x in range(image.width):
        image.putpixel((x, x % image.height), (200, 169, 106))
    return image


def test_feed_dimensions_and_hash():
    rendered = render_image(sample_payload(), photo())
    assert rendered.mime_type == "image/webp"
    assert len(rendered.sha256) == 64
    output = Image.open(io.BytesIO(rendered.body))
    assert output.size == (1080, 1350)


def test_story_dimensions():
    rendered = render_image(sample_payload("STORY_PROPERTY_V1"), photo())
    assert Image.open(io.BytesIO(rendered.body)).size == (1080, 1920)


def test_all_six_registered_templates_render_at_contract_size():
    expected = {
        "IG_FEED_HERO_V1": (1080, 1350),
        "FB_FEED_PROPERTY_V1": (1080, 1350),
        "IG_CAROUSEL_V1": (1080, 1350),
        "STORY_PROPERTY_V1": (1080, 1920),
        "REEL_PROPERTY_V1": (1080, 1920),
        "MARKETPLACE_PACK_V1": (1080, 1350),
    }
    for template, dimensions in expected.items():
        rendered = render_image(sample_payload(template), photo())
        assert Image.open(io.BytesIO(rendered.body)).size == dimensions


def test_long_cta_and_property_code_fit_their_reserved_areas():
    from PIL import ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (1080, 1920)))
    cta, cta_font = _fit_single_line(
        draw, "SOLICITE MAIS INFORMAÇÕES COM A EQUIPE VCV", FONT_BODY, 29, 19, 856
    )
    code, code_font = _fit_single_line(
        draw, "APTO MILLENIUM I", FONT_BODY, 24, 18, 420
    )
    assert draw.textlength(cta, font=cta_font) <= 856
    assert draw.textlength(code, font=code_font) <= 420


def test_unknown_field_is_rejected():
    payload = sample_payload()
    payload["publish"] = True
    try:
        validate_payload(payload)
    except RenderError as error:
        assert "Campos desconhecidos" in str(error)
    else:
        raise AssertionError("Campo desconhecido foi aceito")


def test_draft_template_requires_explicit_homologation_flag(monkeypatch):
    payload = sample_payload()
    payload["template_status"] = "RASCUNHO"
    try:
        validate_payload(payload)
    except RenderError as error:
        assert "não publicado" in str(error)
    else:
        raise AssertionError("Template rascunho foi aceito sem autorização")
    monkeypatch.setenv("ALLOW_DRAFT_TEMPLATES", "true")
    assert validate_payload(payload)["template_status"] == "RASCUNHO"


def test_reel_package_contains_video_subtitles_cover_and_manifest():
    payload = sample_payload("REEL_PROPERTY_V1")
    payload["scenes"] = [{"asset_order": 1, "caption": "Ambientes amplos", "duration_ms": 1500}]
    rendered = render_reel_package(payload, {1: photo()})
    assert rendered.duration_ms == 1500
    with zipfile.ZipFile(io.BytesIO(rendered.body)) as archive:
        assert set(archive.namelist()) == {"reel.mp4", "captions.srt", "cover.webp", "manifest.json"}
        assert len(archive.read("reel.mp4")) > 1000


def test_reel_uses_transitions_motion_and_neutral_music():
    payload = sample_payload("REEL_PROPERTY_V1")
    payload["music_profile"] = "elegant_minimal"
    payload["assets"].append({"url": "https://images.example.test/photo-2.webp", "order": 2})
    payload["scenes"] = [
        {"asset_order": 1, "caption": "Sala integrada", "duration_ms": 1800, "motion": "push_in", "transition": "fade"},
        {"asset_order": 2, "caption": "Cozinha iluminada", "duration_ms": 1800, "motion": "pan_right", "transition": "smoothleft"},
    ]
    rendered = render_reel_package(payload, {1: photo(), 2: photo().transpose(Image.Transpose.FLIP_LEFT_RIGHT)})
    with zipfile.ZipFile(io.BytesIO(rendered.body)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["music_profile"] == "elegant_minimal"
        assert manifest["transition_seconds"] == 0.45


def test_story_can_use_the_vertical_video_pipeline():
    payload = sample_payload("STORY_PROPERTY_V1")
    payload["scenes"] = [
        {"asset_order": 1, "caption": "Veja os detalhes", "duration_ms": 1800, "motion": "push_in", "transition": "fade"},
    ]
    rendered = render_reel_package(payload, {1: photo()})
    assert rendered.mime_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(rendered.body)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["template_code"] == "STORY_PROPERTY_V1"
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            video.write(archive.read("reel.mp4"))
            video.flush()
            streams = subprocess.check_output([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", video.name,
            ], text=True).splitlines()
        assert "video" in streams
        assert "audio" in streams
