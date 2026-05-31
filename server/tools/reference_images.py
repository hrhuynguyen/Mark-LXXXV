"""Google image reference search and image-to-text analysis."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

import requests
from google import genai
from google.genai import types


class ReferenceImageSearchUnavailable(RuntimeError):
    """Raised when Google image search is not configured."""


@dataclass(frozen=True)
class ReferenceImage:
    title: str
    image_url: str
    context_url: str = ""
    thumbnail_url: str = ""
    snippet: str = ""

    def to_json(self) -> dict[str, str]:
        return asdict(self)


def search_reference_images(query: str, *, count: int = 5) -> list[ReferenceImage]:
    """Search Google Programmable Search for image references."""

    api_key = _google_search_api_key()
    cse_id = os.getenv("GOOGLE_CSE_ID") or os.getenv("GOOGLE_SEARCH_ENGINE_ID")
    if not api_key or not cse_id:
        raise ReferenceImageSearchUnavailable(
            "Google image references require GOOGLE_SEARCH_API_KEY and GOOGLE_CSE_ID "
            "(or GOOGLE_SEARCH_ENGINE_ID) in .env."
        )

    try:
        response = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": api_key,
                "cx": cse_id,
                "q": query,
                "searchType": "image",
                "num": max(1, min(10, int(count))),
                "safe": "active",
            },
            timeout=15,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        raise ReferenceImageSearchUnavailable(
            _search_auth_error_message(status)
        ) from exc
    except requests.RequestException as exc:
        raise ReferenceImageSearchUnavailable(
            "Google image search request failed. Check internet access, "
            "Custom Search JSON API enablement, and GOOGLE_SEARCH_API_KEY."
        ) from exc

    items = response.json().get("items") or []
    return [_reference_from_item(item) for item in items[:count]]


def build_reference_query(snapshot: dict[str, Any] | None, *, scope: str, query: str = "") -> str:
    """Build a useful image query from the selected Blender object metadata."""

    cleaned_query = query.strip()
    if cleaned_query:
        return cleaned_query

    terms: list[str] = []
    if snapshot:
        name = str(snapshot.get("name") or "").replace("_", " ").replace("-", " ")
        parent = str(snapshot.get("parent") or "").replace("_", " ").replace("-", " ")
        materials = " ".join(str(value) for value in snapshot.get("materials") or [])
        dimensions = snapshot.get("dimensions")
        if scope == "object" and parent:
            terms.append(parent)
        terms.append(name)
        if materials:
            terms.append(materials)
        if dimensions:
            terms.append("3d model reference")
    terms.append("real object reference image")
    return " ".join(term for term in terms if term).strip()


def describe_reference_image(
    reference: ReferenceImage,
    *,
    target_snapshot: dict[str, Any] | None = None,
) -> str:
    """Summarize a chosen reference image for downstream Blender regeneration."""

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        return _fallback_reference_description(reference)

    try:
        image_response = requests.get(reference.image_url, timeout=15)
        image_response.raise_for_status()
    except Exception:
        return _fallback_reference_description(reference)

    content_type = image_response.headers.get("content-type", "").split(";", 1)[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"

    target = _target_summary(target_snapshot)
    prompt = (
        "Analyze this reference image for a Blender refinement task. "
        "Describe only visual traits that matter for rebuilding or improving the selected "
        "part/object: shape, silhouette, proportions, materials, colors, and distinctive details. "
        "Avoid saying filenames or URLs. Keep it under 80 words.\n\n"
        f"Target context: {target}\n"
        f"Reference title: {reference.title}\n"
        f"Reference page snippet: {reference.snippet}"
    )
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=os.getenv("FORGE_REFERENCE_VISION_MODEL", "gemini-2.5-flash"),
        contents=[
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=image_response.content, mime_type=content_type),
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=220,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return (response.text or "").strip() or _fallback_reference_description(reference)


def _reference_from_item(item: dict[str, Any]) -> ReferenceImage:
    image_meta = item.get("image") or {}
    return ReferenceImage(
        title=str(item.get("title") or "Untitled image"),
        image_url=str(item.get("link") or ""),
        context_url=str(image_meta.get("contextLink") or item.get("displayLink") or ""),
        thumbnail_url=str(image_meta.get("thumbnailLink") or ""),
        snippet=str(item.get("snippet") or ""),
    )


def _google_search_api_key() -> str | None:
    return os.getenv("GOOGLE_SEARCH_API_KEY") or os.getenv("GOOGLE_CUSTOM_SEARCH_API_KEY")


def _search_auth_error_message(status: int | None) -> str:
    if status in {401, 403}:
        return (
            "Google image search was rejected. Create a Google Cloud API key with "
            "Custom Search JSON API enabled and set it as GOOGLE_SEARCH_API_KEY. "
            "Keep GOOGLE_API_KEY for Gemini."
        )
    if status is None:
        return "Google image search failed before Google returned a status code."
    return f"Google image search failed with HTTP {status}."


def _target_summary(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "No selected Blender target metadata."
    fields = {
        "name": snapshot.get("name"),
        "type": snapshot.get("type"),
        "dimensions": snapshot.get("dimensions"),
        "materials": snapshot.get("materials"),
        "parent": snapshot.get("parent"),
    }
    return ", ".join(f"{key}={value}" for key, value in fields.items() if value)


def _fallback_reference_description(reference: ReferenceImage) -> str:
    details = [reference.title]
    if reference.snippet:
        details.append(reference.snippet)
    if reference.context_url:
        details.append(f"Source page: {reference.context_url}")
    return " ".join(details)
