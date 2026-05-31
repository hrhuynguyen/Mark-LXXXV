from __future__ import annotations

import pytest
import requests

from server.tools import reference_images
from server.tools.reference_images import (
    ReferenceImage,
    ReferenceImageSearchUnavailable,
    build_reference_query,
    describe_reference_image,
    search_reference_images,
)


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self.payload = payload or {}
        self.status_code = status_code
        self.url = "https://www.googleapis.com/customsearch/v1?key=secret"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Client Error")
            error.response = self
            raise error
        return None

    def json(self) -> dict:
        return self.payload


def test_build_reference_query_uses_selected_part_context():
    query = build_reference_query(
        {
            "name": "landing_leg_1",
            "parent": "Falcon_9",
            "materials": ["dark_metal", "carbon_black"],
            "dimensions": [0.3, 0.4, 3.0],
        },
        scope="part",
    )

    assert "landing leg 1" in query
    assert "dark_metal carbon_black" in query
    assert "3d model reference" in query
    assert "real object reference image" in query
    assert "Falcon 9" not in query


def test_build_reference_query_can_target_whole_object_or_explicit_query():
    snapshot = {"name": "landing_leg_1", "parent": "Falcon_9"}

    object_query = build_reference_query(snapshot, scope="object")

    assert "Falcon 9" in object_query
    assert build_reference_query(snapshot, scope="object", query="rocket landing gear") == (
        "rocket landing gear"
    )


def test_search_reference_images_uses_google_custom_search(monkeypatch):
    seen: dict = {}

    def fake_get(url: str, *, params: dict, timeout: int) -> _FakeResponse:
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return _FakeResponse(
            {
                "items": [
                    {
                        "title": "Rocket landing leg closeup",
                        "link": "https://example.com/leg.jpg",
                        "snippet": "A deployable landing leg.",
                        "image": {
                            "contextLink": "https://example.com/page",
                            "thumbnailLink": "https://example.com/thumb.jpg",
                        },
                    }
                ]
            }
        )

    monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "search-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "gemini-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    monkeypatch.setattr(reference_images.requests, "get", fake_get)

    results = search_reference_images("rocket leg", count=12)

    assert seen["url"] == "https://www.googleapis.com/customsearch/v1"
    assert seen["params"]["key"] == "search-key"
    assert seen["params"]["q"] == "rocket leg"
    assert seen["params"]["searchType"] == "image"
    assert seen["params"]["num"] == 10
    assert seen["params"]["safe"] == "active"
    assert seen["timeout"] == 15
    assert results == [
        ReferenceImage(
            title="Rocket landing leg closeup",
            image_url="https://example.com/leg.jpg",
            context_url="https://example.com/page",
            thumbnail_url="https://example.com/thumb.jpg",
            snippet="A deployable landing leg.",
        )
    ]


def test_search_reference_images_requires_google_cse(monkeypatch):
    for key in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_SEARCH_API_KEY",
        "GOOGLE_CUSTOM_SEARCH_API_KEY",
        "GOOGLE_CSE_ID",
        "GOOGLE_SEARCH_ENGINE_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ReferenceImageSearchUnavailable):
        search_reference_images("rocket leg")


def test_search_reference_images_does_not_use_gemini_key_for_search(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "gemini-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_API_KEY", raising=False)

    with pytest.raises(ReferenceImageSearchUnavailable) as exc_info:
        search_reference_images("rocket leg")

    assert "GOOGLE_SEARCH_API_KEY" in str(exc_info.value)


def test_search_reference_images_sanitizes_rejected_search_key(monkeypatch):
    def fake_get(url: str, *, params: dict, timeout: int) -> _FakeResponse:
        return _FakeResponse(status_code=401)

    monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "secret-search-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    monkeypatch.setattr(reference_images.requests, "get", fake_get)

    with pytest.raises(ReferenceImageSearchUnavailable) as exc_info:
        search_reference_images("rocket leg")

    message = str(exc_info.value)
    assert "Custom Search JSON API" in message
    assert "secret-search-key" not in message
    assert "googleapis.com" not in message


def test_search_reference_images_sanitizes_request_errors(monkeypatch):
    def fake_get(url: str, *, params: dict, timeout: int) -> _FakeResponse:
        raise requests.RequestException(
            "failed for https://www.googleapis.com/customsearch/v1?key=secret-search-key"
        )

    monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "secret-search-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    monkeypatch.setattr(reference_images.requests, "get", fake_get)

    with pytest.raises(ReferenceImageSearchUnavailable) as exc_info:
        search_reference_images("rocket leg")

    message = str(exc_info.value)
    assert "secret-search-key" not in message
    assert "googleapis.com" not in message
    assert "internet access" in message


def test_describe_reference_image_falls_back_without_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    description = describe_reference_image(
        ReferenceImage(
            title="Landing leg",
            image_url="https://example.com/leg.jpg",
            context_url="https://example.com/page",
            snippet="A hinged support leg.",
        )
    )

    assert "Landing leg" in description
    assert "A hinged support leg." in description
    assert "https://example.com/page" in description
