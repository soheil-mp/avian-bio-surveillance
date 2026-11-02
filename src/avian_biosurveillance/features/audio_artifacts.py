"""Image artifact rendering for processed audio clips."""

from __future__ import annotations

import io
from collections.abc import Iterable
from itertools import pairwise

import librosa
import librosa.display
import matplotlib
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


matplotlib.use("Agg")
import matplotlib.pyplot as plt


EDGE_COLOR = "#5EE0B1"
FILL_COLOR = "#347A61"
BACKGROUND_COLOR = "#05110E"
BAR_COLOR = "#5ACDA0"
GLOW_COLOR = "#1A4A3C"
ARTIFACT_WIDTH_PX = 1460
ARTIFACT_HEIGHT_PX = 360
ARTIFACT_DPI = 180


def render_spectrogram_png(
    audio: np.ndarray,
    sample_rate: int,
    *,
    width_px: int = ARTIFACT_WIDTH_PX,
    height_px: int = ARTIFACT_HEIGHT_PX,
    dpi: int = ARTIFACT_DPI,
    output_sample_rate: int = 22_050,
    n_fft: int = 2_048,
    hop_length: int = 512,
    n_mels: int = 128,
    fmin: int = 0,
    fmax: int | None = None,
) -> bytes:
    """Render a rectangular mel-spectrogram PNG from mono audio samples."""
    mono = _mono_float_audio(audio)
    if sample_rate != output_sample_rate:
        mono = librosa.resample(
            mono,
            orig_sr=sample_rate,
            target_sr=output_sample_rate,
        )
        sample_rate = output_sample_rate

    mel_power = librosa.feature.melspectrogram(
        y=mono,
        sr=sample_rate,
        n_fft=n_fft,
        hop_length=hop_length,
        n_mels=n_mels,
        fmin=fmin,
        fmax=fmax,
        power=2.0,
    )
    mel_db = librosa.power_to_db(mel_power, ref=np.max)

    fig = plt.figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    axes = plt.axes([0, 0, 1, 1])
    librosa.display.specshow(
        mel_db,
        sr=sample_rate,
        hop_length=hop_length,
        cmap="magma",
    )
    axes.set_axis_off()
    plt.margins(0, 0)
    plt.axis("off")

    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format="png",
        facecolor=BACKGROUND_COLOR,
        pad_inches=0,
        transparent=False,
    )
    plt.close(fig)
    return buffer.getvalue()


def render_bar_waveform_png(
    audio: np.ndarray,
    *,
    width_px: int = ARTIFACT_WIDTH_PX,
    height_px: int = ARTIFACT_HEIGHT_PX,
    bar_step_px: int = 4,
    bar_width_px: int = 2,
    show_right_scale_numbers: bool = True,
) -> bytes:
    """Render the neon vertical-bar waveform used by the dashboard."""
    mono = _normalize_peak(_mono_float_audio(audio))
    center_y = height_px // 2
    margin_left = 5
    margin_right = 24
    margin_top = 10
    margin_bottom = 10
    usable_height = height_px - margin_top - margin_bottom
    max_bar_half_height = int(usable_height * 0.47)

    bar_count = max(1, (width_px - margin_left - margin_right) // bar_step_px)
    frame_size = max(1, int(np.ceil(len(mono) / bar_count)))
    padded_size = bar_count * frame_size
    padded = np.pad(mono, (0, max(0, padded_size - len(mono))))[:padded_size]
    frames = padded.reshape(bar_count, frame_size)
    amplitudes = np.max(np.abs(frames), axis=1)
    amplitudes = np.power(amplitudes, 0.72)
    amplitudes = np.clip(amplitudes, 0.03, 1.0)

    image = Image.new("RGBA", (width_px, height_px), BACKGROUND_COLOR)
    base = ImageDraw.Draw(image)
    base.line([(0, 1), (width_px, 1)], fill=_hex_to_rgba(GLOW_COLOR, 80), width=1)
    base.line(
        [(0, height_px - 2), (width_px, height_px - 2)],
        fill=_hex_to_rgba(GLOW_COLOR, 55),
        width=1,
    )

    glow = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    for index, amplitude in enumerate(amplitudes):
        position_x = margin_left + index * bar_step_px
        half_height = int(float(amplitude) * max_bar_half_height)
        glow_draw.line(
            [
                (position_x, center_y - half_height),
                (position_x, center_y + half_height),
            ],
            fill=_hex_to_rgba(GLOW_COLOR, 150),
            width=4,
        )

    image = Image.alpha_composite(
        image, glow.filter(ImageFilter.GaussianBlur(radius=1.6))
    )
    draw = ImageDraw.Draw(image)
    for index, amplitude in enumerate(amplitudes):
        position_x = margin_left + index * bar_step_px
        half_height = int(float(amplitude) * max_bar_half_height)
        draw.line(
            [
                (position_x, center_y - half_height),
                (position_x, center_y + half_height),
            ],
            fill=_hex_to_rgba(BAR_COLOR, 245),
            width=bar_width_px,
        )

    if show_right_scale_numbers:
        try:
            font = ImageFont.truetype("DejaVuSansMono.ttf", 9)
        except OSError:
            font = ImageFont.load_default()
        text_color = _hex_to_rgba(BAR_COLOR, 95)
        draw.text((width_px - 22, 6), "1.0", fill=text_color, font=font)
        draw.text((width_px - 28, height_px - 18), "-1.0", fill=text_color, font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_smooth_waveform_png(
    audio: np.ndarray,
    *,
    width_px: int = ARTIFACT_WIDTH_PX,
    height_px: int = ARTIFACT_HEIGHT_PX,
    dpi: int = ARTIFACT_DPI,
    envelope_points: int = 2_200,
) -> bytes:
    """Render a smoothed filled waveform inspired by the sample script."""
    mono = _normalize_peak(_mono_float_audio(audio))
    amplitudes = _amplitude_envelope(mono, envelope_points)
    amplitudes = _smooth(amplitudes, sigma=18.0)
    max_amplitude = float(np.max(amplitudes)) if amplitudes.size else 0.0
    if max_amplitude > 1e-10:
        amplitudes = amplitudes / max_amplitude * 0.75

    time_axis = np.linspace(0, 1, len(amplitudes))
    upper = amplitudes
    lower = -amplitudes

    fig, axes = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    fig.patch.set_facecolor(BACKGROUND_COLOR)
    axes.set_facecolor(BACKGROUND_COLOR)
    axes.fill_between(time_axis, lower, upper, color=FILL_COLOR, alpha=0.90)

    for line_width, alpha in ((20, 0.03), (14, 0.06), (9, 0.10), (5, 0.18)):
        axes.plot(
            time_axis,
            upper,
            color=EDGE_COLOR,
            lw=line_width,
            alpha=alpha,
            solid_capstyle="round",
        )
        axes.plot(
            time_axis,
            lower,
            color=EDGE_COLOR,
            lw=line_width,
            alpha=alpha,
            solid_capstyle="round",
        )

    axes.plot(time_axis, upper, color=EDGE_COLOR, lw=1.4, alpha=0.95)
    axes.plot(time_axis, lower, color=EDGE_COLOR, lw=1.4, alpha=0.95)
    axes.set_xlim(0, 1)
    axes.set_ylim(-1.25, 1.25)
    axes.axis("off")
    plt.subplots_adjust(0, 0, 1, 1)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=BACKGROUND_COLOR)
    plt.close(fig)
    return buffer.getvalue()


def _mono_float_audio(audio: np.ndarray) -> np.ndarray:
    array = np.asarray(audio, dtype=np.float32)
    if array.size == 0:
        raise ValueError("audio must not be empty")
    if array.ndim > 1:
        array = np.mean(array, axis=1, dtype=np.float32)
    return np.asarray(array, dtype=np.float32)


def _normalize_peak(audio: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(audio)))
    if peak > 1e-10:
        return audio / peak
    return audio


def _amplitude_envelope(audio: np.ndarray, envelope_points: int) -> np.ndarray:
    frame_count = min(max(64, envelope_points), max(1, len(audio)))
    boundaries = np.linspace(0, len(audio), frame_count + 1, dtype=int)
    values = [
        float(np.max(np.abs(audio[start:end]))) if end > start else 0.0
        for start, end in pairwise(boundaries)
    ]
    return np.asarray(values, dtype=np.float32)


def _smooth(values: np.ndarray, *, sigma: float) -> np.ndarray:
    try:
        from scipy.ndimage import gaussian_filter1d
    except ImportError:
        return _moving_average(values, window=37)
    return gaussian_filter1d(values, sigma=sigma)


def _moving_average(values: np.ndarray, *, window: int) -> np.ndarray:
    if values.size == 0:
        return values
    window = max(1, min(window, values.size))
    weights = np.ones(window, dtype=np.float32) / float(window)
    return np.convolve(values, weights, mode="same")


def _hex_to_rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    clean = hex_color.lstrip("#")
    channels: Iterable[int] = (int(clean[index : index + 2], 16) for index in (0, 2, 4))
    return (*channels, alpha)
