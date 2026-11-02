from __future__ import annotations

from unittest.mock import patch

import pytest

from avian_biosurveillance.utils.phylopic import (
    PhyloPicResult,
    reset_phylopic_caches,
    resolve_phylopic,
)


@pytest.fixture(autouse=True)
def clear_phylopic_cache_fixture() -> None:
    reset_phylopic_caches()


def test_resolve_phylopic_prefers_node_primary_image() -> None:
    with (
        patch(
            "avian_biosurveillance.utils.phylopic._search_nodes",
            return_value=[
                {
                    "names": [[{"text": "Erithacus rubecula"}]],
                    "_links": {
                        "primaryImage": {"href": "/images/primary-robin"},
                    },
                }
            ],
        ),
        patch(
            "avian_biosurveillance.utils.phylopic._fetch_node_primary_image",
            return_value={
                "uuid": "robin-uuid",
                "attribution": "Robin artist",
                "_links": {
                    "vectorFile": {"href": "https://example.com/robin.svg"},
                    "thumbnailFiles": [{"href": "https://example.com/robin-thumb.png"}],
                },
            },
        ),
        patch(
            "avian_biosurveillance.utils.phylopic._search_images",
            return_value=[],
        ),
    ):
        result = resolve_phylopic("Erithacus rubecula")

    assert result == PhyloPicResult(
        svg_url="https://example.com/robin.svg",
        thumb_url="https://example.com/robin-thumb.png",
        attribution="Robin artist",
        uuid="robin-uuid",
    )


def test_resolve_phylopic_falls_back_to_image_search() -> None:
    with (
        patch(
            "avian_biosurveillance.utils.phylopic._search_nodes",
            return_value=[],
        ),
        patch(
            "avian_biosurveillance.utils.phylopic._search_images",
            return_value=[
                {
                    "uuid": "blackbird-uuid",
                    "attribution": "Blackbird artist",
                    "_links": {
                        "vectorFile": {"href": "https://example.com/blackbird.svg"},
                        "thumbnailFiles": [
                            {"href": "https://example.com/blackbird-thumb.png"}
                        ],
                        "specificNode": {"title": "Turdus merula"},
                    },
                }
            ],
        ),
    ):
        result = resolve_phylopic("Turdus merula")

    assert result == PhyloPicResult(
        svg_url="https://example.com/blackbird.svg",
        thumb_url="https://example.com/blackbird-thumb.png",
        attribution="Blackbird artist",
        uuid="blackbird-uuid",
    )


def test_resolve_phylopic_returns_empty_on_lookup_failure() -> None:
    with patch(
        "avian_biosurveillance.utils.phylopic._search_nodes",
        side_effect=RuntimeError("boom"),
    ):
        result = resolve_phylopic("Erithacus rubecula")

    assert result == PhyloPicResult()
