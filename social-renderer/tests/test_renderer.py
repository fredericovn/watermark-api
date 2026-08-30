from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renderer import RenderError, render_image, render_reel_package, validate_payload  # noqa: E402


def sample_payload(template="IG_FEED_HERO_V1"):
    return {
        "template_code": template,
        "template_version": 1,
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


def test_unknown_field_is_rejected():
    payload = sample_payload()
    payload["publish"] = True
    try:
        validate_payload(payload)
    except RenderError as error:
        assert "Campos desconhecidos" in str(error)
    else:
        raise AssertionError("Campo desconhecido foi aceito")


def test_reel_package_contains_video_subtitles_cover_and_manifest():
    payload = sample_payload("REEL_PROPERTY_V1")
    payload["scenes"] = [{"asset_order": 1, "caption": "Ambientes amplos", "duration_ms": 1500}]
    rendered = render_reel_package(payload, {1: photo()})
    assert rendered.duration_ms == 1500
    with zipfile.ZipFile(io.BytesIO(rendered.body)) as archive:
        assert set(archive.namelist()) == {"reel.mp4", "captions.srt", "cover.webp", "manifest.json"}
        assert len(archive.read("reel.mp4")) > 1000
