"""Resolve and cache PhyloPic silhouettes for species metadata."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


logger = logging.getLogger(__name__)

_API_BASE = "https://api.phylopic.org"
_JSON_HEADERS = {
    "Accept": "application/vnd.phylopic.v2+json, application/json",
}
_BUILD_CACHE_TTL_S = 6 * 60 * 60
_REQUEST_TIMEOUT_S = 8
_SESSION = requests.Session()
_BUILD_CACHE: dict[str, float | int | None] = {
    "value": None,
    "fetched_at": 0.0,
}
_RESULT_CACHE: dict[str, PhyloPicResult] = {}


@dataclass(frozen=True)
class PhyloPicResult:
    svg_url: str | None = None
    thumb_url: str | None = None
    attribution: str = ""
    uuid: str = ""


def reset_phylopic_caches() -> None:
    """Clear in-memory PhyloPic caches.

    Intended for tests and one-shot maintenance flows.
    """
    _RESULT_CACHE.clear()
    _BUILD_CACHE["value"] = None
    _BUILD_CACHE["fetched_at"] = 0.0


def normalize_name(value: str) -> str:
    """Normalize a species name for PhyloPic lookup and caching."""
    normalized = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9\s-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _get_title_candidates(entity: dict[str, Any]) -> list[str]:
    candidates: set[str] = set()

    def add_candidate(value: Any) -> None:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                candidates.add(stripped)

    links = entity.get("_links") or {}
    add_candidate((links.get("self") or {}).get("title"))

    names = entity.get("names")
    if isinstance(names, list):
        for group in names:
            if not isinstance(group, list):
                continue
            for part in group:
                if isinstance(part, dict):
                    add_candidate(part.get("text"))

    add_candidate((links.get("specificNode") or {}).get("title"))
    for link in links.get("nodes") or []:
        if isinstance(link, dict):
            add_candidate(link.get("title"))

    return list(candidates)


def _score_candidate_match(target: str, candidates: list[str]) -> int:
    normalized_target = normalize_name(target)
    if not normalized_target:
        return -1

    target_genus = normalized_target.split(" ")[0]
    best_score = -1

    for candidate in candidates:
        normalized_candidate = normalize_name(candidate)
        if not normalized_candidate:
            continue
        if normalized_candidate == normalized_target:
            return 1000
        if normalized_candidate.startswith(normalized_target):
            best_score = max(best_score, 700)
            continue
        if normalized_target in normalized_candidate:
            best_score = max(best_score, 500)
            continue
        if normalized_candidate == target_genus:
            best_score = max(best_score, 250)
            continue
        if normalized_candidate.startswith(target_genus):
            best_score = max(best_score, 150)

    return best_score


def _pick_best_match(items: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    if not items:
        return None

    best_item = items[0]
    best_score = _score_candidate_match(target, _get_title_candidates(best_item))

    for item in items[1:]:
        score = _score_candidate_match(target, _get_title_candidates(item))
        if score > best_score:
            best_item = item
            best_score = score

    return best_item


def _extract_embedded_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    items = embedded.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _image_to_result(image: dict[str, Any] | None) -> PhyloPicResult:
    if not image:
        return PhyloPicResult()

    links = image.get("_links") or {}
    thumb_files = links.get("thumbnailFiles") or []
    thumb_url = None
    if isinstance(thumb_files, list) and thumb_files:
        first_thumb = thumb_files[0]
        if isinstance(first_thumb, dict):
            thumb_url = first_thumb.get("href")

    vector_url = (links.get("vectorFile") or {}).get("href")
    source_url = (links.get("sourceFile") or {}).get("href")

    return PhyloPicResult(
        svg_url=vector_url or source_url,
        thumb_url=thumb_url,
        attribution=str(image.get("attribution") or ""),
        uuid=str(image.get("uuid") or ""),
    )


def _get_current_build(*, force_refresh: bool = False) -> int:
    cached_value = _BUILD_CACHE["value"]
    cached_at = float(_BUILD_CACHE["fetched_at"] or 0.0)
    if (
        not force_refresh
        and isinstance(cached_value, int)
        and time.time() - cached_at < _BUILD_CACHE_TTL_S
    ):
        return cached_value

    response = _SESSION.get(
        f"{_API_BASE}/",
        headers=_JSON_HEADERS,
        timeout=_REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()

    payload = response.json()
    build = payload.get("build")
    if not isinstance(build, int):
        raise RuntimeError("PhyloPic build number missing from API root response.")

    _BUILD_CACHE["value"] = build
    _BUILD_CACHE["fetched_at"] = time.time()
    return build


def _fetch_phylopic_json(path_or_url: str, *, force_refresh_build: bool = False) -> Any:
    build = _get_current_build(force_refresh=force_refresh_build)

    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}build={build}"
    else:
        url = f"{_API_BASE}{path_or_url}"
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}build={build}"

    response = _SESSION.get(url, headers=_JSON_HEADERS, timeout=_REQUEST_TIMEOUT_S)
    if response.status_code in {400, 410} and not force_refresh_build:
        return _fetch_phylopic_json(path_or_url, force_refresh_build=True)
    if not response.ok:
        return None
    return response.json()


def _search_nodes(name: str) -> list[dict[str, Any]]:
    payload = _fetch_phylopic_json(
        f"/nodes?filter_name={quote(name)}&page=0&embed_items=true"
    )
    return _extract_embedded_items(payload)


def _search_images(name: str) -> list[dict[str, Any]]:
    payload = _fetch_phylopic_json(
        f"/images?filter_name={quote(name)}&page=0&embed_items=true"
    )
    return _extract_embedded_items(payload)


def _fetch_node_primary_image(node: dict[str, Any]) -> dict[str, Any] | None:
    links = node.get("_links") or {}
    primary_href = (links.get("primaryImage") or {}).get("href")
    if isinstance(primary_href, str) and primary_href:
        payload = _fetch_phylopic_json(primary_href)
        if isinstance(payload, dict):
            return payload

    node_images_href = (links.get("images") or {}).get("href")
    if not isinstance(node_images_href, str) or not node_images_href:
        return None

    separator = "&" if "?" in node_images_href else "?"
    payload = _fetch_phylopic_json(
        f"{node_images_href}{separator}page=0&embed_items=true"
    )
    items = _extract_embedded_items(payload)
    return items[0] if items else None


def resolve_phylopic(name: str | None) -> PhyloPicResult:
    """Resolve a scientific/common name into cached PhyloPic URLs."""
    normalized_name = normalize_name(name or "")
    if not normalized_name:
        return PhyloPicResult()

    cached = _RESULT_CACHE.get(normalized_name)
    if cached is not None:
        return cached

    attempts = list(dict.fromkeys([normalized_name, normalized_name.split(" ")[0]]))
    result = PhyloPicResult()

    try:
        for attempt in attempts:
            if not attempt:
                continue

            nodes = _search_nodes(attempt)
            best_node = _pick_best_match(nodes, normalized_name)
            if best_node is not None:
                node_result = _image_to_result(_fetch_node_primary_image(best_node))
                if node_result.svg_url or node_result.thumb_url:
                    result = node_result
                    break

            images = _search_images(attempt)
            best_image = _pick_best_match(images, normalized_name)
            image_result = _image_to_result(best_image)
            if image_result.svg_url or image_result.thumb_url:
                result = image_result
                break
    except requests.RequestException as exc:
        logger.warning("PhyloPic lookup failed for %s: %s", normalized_name, exc)
    except RuntimeError as exc:
        logger.warning("PhyloPic lookup failed for %s: %s", normalized_name, exc)

    _RESULT_CACHE[normalized_name] = result
    return result
