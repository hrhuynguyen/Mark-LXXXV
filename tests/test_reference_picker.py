from __future__ import annotations

from types import SimpleNamespace

from client.reference_picker import _build_picker_html, _trim_title, best_reference_image_url


def test_best_reference_image_url_prefers_thumbnail():
    reference = SimpleNamespace(
        thumbnail_url="https://example.com/thumb.jpg",
        image_url="https://example.com/full.jpg",
    )

    assert best_reference_image_url(reference) == "https://example.com/thumb.jpg"


def test_best_reference_image_url_falls_back_to_full_image():
    reference = SimpleNamespace(thumbnail_url="", image_url="https://example.com/full.jpg")

    assert best_reference_image_url(reference) == "https://example.com/full.jpg"


def test_trim_title_collapses_whitespace_and_shortens_long_titles():
    assert _trim_title("  Falcon   9 landing leg  ") == "Falcon 9 landing leg"
    assert _trim_title("abcdefghijklmnopqrstuvwxyz", limit=12) == "abcdefghi..."


def test_build_picker_html_contains_clickable_image_cards():
    reference = SimpleNamespace(
        title="Landing <leg>",
        thumbnail_url="https://example.com/thumb.jpg",
        image_url="https://example.com/full.jpg",
        context_url="https://example.com/page",
    )

    html = _build_picker_html([reference], title="Forge <refs>")

    assert "Forge &lt;refs&gt;" in html
    assert "/choose?index=0" in html
    assert "https://example.com/thumb.jpg" in html
    assert "Landing &lt;leg&gt;" in html
    assert "example.com" in html
