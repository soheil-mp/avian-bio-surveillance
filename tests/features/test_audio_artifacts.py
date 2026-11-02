"""Tests for audio artifact rendering helpers."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from avian_biosurveillance.features.audio_artifacts import (
    ARTIFACT_HEIGHT_PX,
    ARTIFACT_WIDTH_PX,
    render_bar_waveform_png,
    render_smooth_waveform_png,
    render_spectrogram_png,
)


def _sine_clip(sample_rate: int = 8_000) -> np.ndarray:
    time_axis = np.linspace(0, 0.25, sample_rate // 4, endpoint=False)
    return (0.4 * np.sin(2 * np.pi * 1_000 * time_axis)).astype(np.float32)


def _png_size(png: bytes) -> tuple[int, int]:
    assert png.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(png)) as image:
        return image.size


def test_render_spectrogram_png() -> None:
    png = render_spectrogram_png(_sine_clip(), 8_000)
    assert _png_size(png) == (ARTIFACT_WIDTH_PX, ARTIFACT_HEIGHT_PX)


def test_render_bar_waveform_png() -> None:
    png = render_bar_waveform_png(_sine_clip())
    assert _png_size(png) == (ARTIFACT_WIDTH_PX, ARTIFACT_HEIGHT_PX)


def test_render_smooth_waveform_png() -> None:
    png = render_smooth_waveform_png(_sine_clip())
    assert _png_size(png) == (ARTIFACT_WIDTH_PX, ARTIFACT_HEIGHT_PX)


def test_rendered_artifacts_share_rectangular_size() -> None:
    clip = _sine_clip()
    sizes = {
        _png_size(render_spectrogram_png(clip, 8_000)),
        _png_size(render_bar_waveform_png(clip)),
        _png_size(render_smooth_waveform_png(clip)),
    }
    assert sizes == {(ARTIFACT_WIDTH_PX, ARTIFACT_HEIGHT_PX)}
    assert ARTIFACT_WIDTH_PX > ARTIFACT_HEIGHT_PX
