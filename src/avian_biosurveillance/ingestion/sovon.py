"""
Sovon Dead Bird Mortality Pipeline
=====================================
Two-phase pipeline:
  Phase 1 – Download bar chart images from portal.sovon.nl
  Phase 2 – Extract numeric data from charts using a local vision LLM
                        via HuggingFace Transformers (default: Qwen/Qwen2.5-VL-3B-Instruct)

Target species:
  Loaded from config/target_species.json (single source of truth).

Input:   portal.sovon.nl chart images
Output:  data/raw/sovon/{species}/*.png          (downloaded charts)
         data/raw/sovon/{species}_monthly.csv     (per-species tables)
         data/raw/sovon/all_monthly.csv           (combined)

Usage:
  python sovon.py                   # run both phases
  python sovon.py --download-only   # phase 1 only
  python sovon.py --extract-only    # phase 2 only (charts must exist)
"""

import gc
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from importlib import util as importlib_util
from pathlib import Path

import requests

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.config.species import registry as _sp_registry


load_runtime_env()

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

SOVON_BASE_URL = "https://portal.sovon.nl"
DOWNLOAD_DELAY = 0.5  # seconds between download requests

# Vision model configuration (HuggingFace Transformers)
VLM_MODEL_ID = os.environ.get("VLM_MODEL_ID", "Qwen/Qwen2.5-VL-3B-Instruct")
VLM_FALLBACK_MODEL_ID = os.environ.get("VLM_FALLBACK_MODEL_ID", "")
VLM_DEVICE = os.environ.get("VLM_DEVICE", "auto").strip().lower()
VLM_DEVICE_MAP = os.environ.get("VLM_DEVICE_MAP", "auto").strip().lower()
VLM_CUDA_MEMORY_RESERVE_MB = int(os.environ.get("VLM_CUDA_MEMORY_RESERVE_MB", "1536"))
VLM_DISABLE_CUDNN = os.environ.get("VLM_DISABLE_CUDNN", "1").strip().lower()
VLM_DISABLE_CUDNN = VLM_DISABLE_CUDNN not in {"0", "false", "no", "off"}
VLM_DISABLE_CUDNN_SDP = os.environ.get("VLM_DISABLE_CUDNN_SDP", "1").strip().lower()
VLM_DISABLE_CUDNN_SDP = VLM_DISABLE_CUDNN_SDP not in {"0", "false", "no", "off"}
VLM_MAX_RETRIES = 3
VLM_MAX_NEW_TOKENS = int(os.environ.get("VLM_MAX_NEW_TOKENS", "256"))


# Derived look-ups built from the shared species registry.
# SPECIES maps a filesystem-safe folder name (underscores) to dict with
# "dutch" and "id" — preserving the interface used by download_charts()
# and extract_data().
SPECIES = {
    s.english.replace(" ", "_"): {"dutch": s.dutch, "id": s.sovon_id}
    for s in _sp_registry
}

FOLDER_TO_ENGLISH = {k: k.replace("_", " ") for k in SPECIES}

NOW = datetime.now()
YEAR_START = 2000
YEAR_END = NOW.year

# Paths relative to project root (script lives in src/avian_biosurveillance/ingestion/)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(
    os.path.join(_SCRIPT_DIR, os.pardir, os.pardir, os.pardir)
)
RAW_DIR = os.path.join(_PROJECT_ROOT, "data", "raw", "sovon")
PROCESSED_DIR = os.path.join(_PROJECT_ROOT, "data", "raw", "sovon")
CACHE_DIR = os.path.join(PROCESSED_DIR, ".cache")

# ══════════════════════════════════════════════════════════════
# PHASE 1: DOWNLOAD CHARTS
# ══════════════════════════════════════════════════════════════


def _get_http_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/png,image/*;q=0.8",
        }
    )
    return s


def _download_if_missing(session, url, filepath):
    """Download file only if it doesn't already exist. Returns status string."""
    if os.path.exists(filepath) and os.path.getsize(filepath) > 500:
        return "SKIP"
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        if len(resp.content) < 500:
            return "EMPTY"
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return "OK"
    except requests.exceptions.ConnectionError:
        return "CONN_ERR"
    except requests.exceptions.HTTPError as e:
        return f"HTTP {e.response.status_code}"
    except requests.exceptions.Timeout:
        return "TIMEOUT"
    except Exception as e:
        return f"ERR: {type(e).__name__}"


def download_charts(raw_dir=None):
    """Phase 1: Download monthly chart images from Sovon."""
    raw_dir = os.fspath(raw_dir) if raw_dir is not None else RAW_DIR

    n_species = len(SPECIES)
    n_years = YEAR_END - YEAR_START + 1
    logger.info(
        "Sovon chart download: %d species, %d-%d (%d years each)",
        n_species,
        YEAR_START,
        YEAR_END,
        n_years,
    )

    session = _get_http_session()
    os.makedirs(raw_dir, exist_ok=True)

    total_downloads = 0
    total_skipped = 0
    total_errors = 0
    consecutive_conn_errors = 0
    MAX_CONN_ERRORS = 5

    for bird_name, info in SPECIES.items():
        sid = info["id"]
        bird_dir = os.path.join(raw_dir, bird_name)
        os.makedirs(bird_dir, exist_ok=True)
        consecutive_conn_errors = 0

        sp_new = 0
        sp_cached = 0
        sp_err = 0

        for year in range(YEAR_START, YEAR_END + 1):
            url = f"{SOVON_BASE_URL}/dood/result/grafiek_get/{sid}/{year}/0/0/0/maand"
            fpath = os.path.join(bird_dir, f"monthly_{year}.png")
            status = _download_if_missing(session, url, fpath)

            if status == "SKIP":
                sp_cached += 1
                total_skipped += 1
                consecutive_conn_errors = 0
            elif status == "OK":
                sp_new += 1
                total_downloads += 1
                consecutive_conn_errors = 0
                time.sleep(DOWNLOAD_DELAY)
            elif status == "CONN_ERR":
                sp_err += 1
                total_errors += 1
                consecutive_conn_errors += 1
                if consecutive_conn_errors >= MAX_CONN_ERRORS:
                    logger.warning(
                        "  %s: %d consecutive connection errors — skipping rest",
                        bird_name,
                        MAX_CONN_ERRORS,
                    )
                    break
                time.sleep(DOWNLOAD_DELAY)
            else:
                sp_err += 1
                total_errors += 1
                consecutive_conn_errors = 0
                time.sleep(DOWNLOAD_DELAY)

        # Single-line per-species summary
        parts = []
        if sp_new:
            parts.append(f"{sp_new} new")
        if sp_cached:
            parts.append(f"{sp_cached} cached")
        if sp_err:
            parts.append(f"{sp_err} err")
        logger.info(
            "  %-25s %s",
            bird_name,
            ", ".join(parts) or "nothing",
        )

    logger.info(
        "Sovon download complete — %d new, %d cached, %d errors",
        total_downloads,
        total_skipped,
        total_errors,
    )

    return total_errors == 0 or total_downloads > 0 or total_skipped > 0


# ══════════════════════════════════════════════════════════════
# PHASE 2: EXTRACT DATA WITH VISION LLM (HUGGINGFACE)
# ══════════════════════════════════════════════════════════════

MONTHLY_PROMPT = """\
Extract data from this bar chart of monthly dead bird counts.

READING INSTRUCTIONS:
- The x-axis has positions 1-12 (months). Bars are centered on their month number.
- Each bar has its exact count printed directly above it — use that number, do not estimate from the y-axis.
- If a month position has NO bar at all (just empty space), its value is 0.

Think step by step, then output JSON.

STEP 1 — METADATA: Read the text below the chart.
- Species: the Dutch name before "(n= ...)"
- n_total: the number after "n="
- Year: the number after "Jaar:"

STEP 2 — BARS: Scan left to right, month 1 through 12, ONE pass only.
For each month position, write:
  Month X: [value on bar] OR Month X: 0 (no bar)

STEP 3 — VERIFY: Add up all 12 month values. The sum must equal n_total.
If it does not match, re-read only the months you are unsure about.

STEP 4 — OUTPUT: After your reasoning, output ONLY this JSON block:
```json
{
  "species": "...",
  "year": 2005,
  "n_total": 5,
  "months": {
    "1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0,
    "7": 0, "8": 0, "9": 0, "10": 0, "11": 0, "12": 0
  }
}
```
"""


def _get_cache_path(image_path, raw_dir=RAW_DIR, cache_dir=CACHE_DIR):
    """Return filesystem path for the JSON cache entry of an image."""
    rel = os.path.relpath(image_path, raw_dir)
    return os.path.join(cache_dir, rel.replace(os.sep, "_").replace(".png", ".json"))


def _load_cache(image_path, raw_dir=RAW_DIR, cache_dir=CACHE_DIR):
    """Load cached extraction result, or return None."""
    cache_path = _get_cache_path(image_path, raw_dir=raw_dir, cache_dir=cache_dir)
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _save_cache(image_path, data, raw_dir=RAW_DIR, cache_dir=CACHE_DIR):
    """Persist extraction result to the JSON cache."""
    cache_path = _get_cache_path(image_path, raw_dir=raw_dir, cache_dir=cache_dir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(data, f, indent=2)


def _parse_structured_monthly_response(raw_text):
    """Parse step-by-step VLM output when no JSON block is emitted.

    Some model responses follow the prompt faithfully with explicit
    ``Species:``, ``n_total:``, ``Year:``, and ``Month X:`` lines but never
    emit the final JSON block. This parser recovers those structured values.
    """
    if not raw_text:
        return None

    text = raw_text.replace("\r\n", "\n")
    species_match = re.search(
        r'^[\-\*\s]*Species:\s*"?([^"\n]+?)"?\s*$',
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    n_total_match = re.search(
        r"^[\-\*\s]*n_total:\s*(\d+)\s*$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    year_match = re.search(
        r"^[\-\*\s]*Year:\s*(\d{4})\s*$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    month_matches = re.findall(
        r"^[\-\*\s]*Month\s+(\d{1,2})\s*:\s*(-?\d+)\s*$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    if not (species_match and n_total_match and year_match and month_matches):
        return None

    months = {}
    for month_str, count_str in month_matches:
        month = int(month_str)
        if 1 <= month <= 12:
            months[str(month)] = int(count_str)

    if len(months) != 12:
        return None

    return {
        "species": species_match.group(1).strip(),
        "year": int(year_match.group(1)),
        "n_total": int(n_total_match.group(1)),
        "months": {str(i): months[str(i)] for i in range(1, 13)},
    }


# -- Model cache (supports multiple models for fallback) --------------------
_vlm_cache = {}  # {model_id: (model, processor)}


def _resolve_vlm_device(torch_module):
    """Select the VLM inference device from VLM_DEVICE and available hardware."""
    requested = VLM_DEVICE or "auto"
    if requested == "auto":
        if torch_module.cuda.is_available():
            return "cuda"
        mps_backend = getattr(getattr(torch_module, "backends", None), "mps", None)
        if mps_backend is not None and mps_backend.is_available():
            return "mps"
        return "cpu"

    if requested == "cuda" and not torch_module.cuda.is_available():
        raise RuntimeError("VLM_DEVICE=cuda was requested, but CUDA is not available")

    if requested == "mps":
        mps_backend = getattr(getattr(torch_module, "backends", None), "mps", None)
        if mps_backend is None or not mps_backend.is_available():
            raise RuntimeError("VLM_DEVICE=mps was requested, but MPS is not available")

    if requested not in {"cpu", "cuda", "mps"}:
        raise ValueError(
            f"Unsupported VLM_DEVICE={VLM_DEVICE!r}; use auto, cpu, cuda, or mps"
        )

    return requested


def _configure_vlm_torch_backends(torch_module):
    """Apply backend workarounds needed by local VLM inference."""
    cudnn_backend = getattr(getattr(torch_module, "backends", None), "cudnn", None)
    if VLM_DISABLE_CUDNN and cudnn_backend is not None:
        cudnn_backend.enabled = False

    cuda_backend = getattr(getattr(torch_module, "backends", None), "cuda", None)
    disable_cudnn_sdp = getattr(cuda_backend, "enable_cudnn_sdp", None)
    if VLM_DISABLE_CUDNN_SDP and disable_cudnn_sdp is not None:
        disable_cudnn_sdp(False)


def _accelerate_available():
    """Return whether Transformers can use accelerate-backed device maps."""
    return importlib_util.find_spec("accelerate") is not None


def _is_cuda_oom(torch_module, exc):
    """Return whether an exception is a CUDA OOM raised by PyTorch."""
    oom_error = getattr(torch_module, "OutOfMemoryError", None)
    if oom_error is not None and isinstance(exc, oom_error):
        return True
    return "CUDA out of memory" in str(exc)


def _cuda_max_memory(torch_module):
    """Build a conservative max_memory map for accelerate dispatch."""
    try:
        free_bytes, _total_bytes = torch_module.cuda.mem_get_info()
    except Exception:
        return None

    reserve_bytes = max(VLM_CUDA_MEMORY_RESERVE_MB, 0) * 1024 * 1024
    cuda_budget = max(free_bytes - reserve_bytes, 1024 * 1024 * 1024)
    return {
        0: int(cuda_budget),
        "cpu": os.environ.get("VLM_CPU_MEMORY", "48GiB"),
    }


def _resolve_vlm_device_map():
    """Return the configured Transformers device map, or None for manual .to()."""
    if VLM_DEVICE_MAP in {"", "none", "off", "false"}:
        return None
    if VLM_DEVICE_MAP in {"auto", "balanced", "balanced_low_0", "sequential"}:
        return VLM_DEVICE_MAP
    raise ValueError(
        "Unsupported VLM_DEVICE_MAP="
        f"{VLM_DEVICE_MAP!r}; use auto, balanced, balanced_low_0, sequential, or none"
    )


def _chat_template_supports_kwarg(processor, kwarg):
    """Return whether a processor chat template references a Jinja kwarg."""
    template = getattr(processor, "chat_template", None)
    if isinstance(template, dict):
        template = template.get("default") or next(iter(template.values()), "")
    if not template:
        template = getattr(getattr(processor, "tokenizer", None), "chat_template", "")
    return bool(template and kwarg in template)


def _load_vlm(model_id=None):
    """Lazy-load a vision language model and processor.

    Results are cached so each model is loaded at most once per run.
    Downloads model weights on the first call; subsequent calls are instant.
    Uses CUDA/MPS automatically when available unless VLM_DEVICE overrides it.

    Args:
        model_id: HuggingFace model identifier.  Defaults to *VLM_MODEL_ID*.

    Returns:
        Tuple of ``(model, processor)``.
    """
    model_id = model_id or VLM_MODEL_ID
    if model_id in _vlm_cache:
        return _vlm_cache[model_id]

    try:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except ImportError as exc:
        msg = (
            f"Missing dependency: {exc}. "
            "Run: pip install 'transformers>=4.45' torch torchvision Pillow"
        )
        raise ImportError(msg) from exc

    _configure_vlm_torch_backends(torch)
    device = _resolve_vlm_device(torch)
    device_map = _resolve_vlm_device_map() if device == "cuda" else None
    use_device_map = bool(device_map and _accelerate_available())
    if device_map and not use_device_map:
        logger.info(
            "VLM_DEVICE_MAP=%s requested, but accelerate is not installed; "
            "trying full-model %s placement",
            device_map,
            device,
        )

    logger.info("Loading vision model: %s (device=%s) ...", model_id, device)
    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    load_kwargs = {
        "trust_remote_code": True,
        "torch_dtype": "auto",
    }
    if use_device_map:
        load_kwargs["device_map"] = device_map
        max_memory = _cuda_max_memory(torch)
        if max_memory is not None:
            load_kwargs["max_memory"] = max_memory

    try:
        model = AutoModelForImageTextToText.from_pretrained(model_id, **load_kwargs)
    except Exception as exc:
        if device != "cuda" or not _is_cuda_oom(torch, exc) or VLM_DEVICE != "auto":
            raise
        logger.warning(
            "CUDA OOM while loading %s; falling back to CPU. "
            "Install accelerate or increase VLM_CUDA_MEMORY_RESERVE_MB to keep "
            "less of the model on GPU.",
            model_id,
        )
        gc.collect()
        torch.cuda.empty_cache()
        device = "cpu"
        use_device_map = False
        model = AutoModelForImageTextToText.from_pretrained(
            model_id,
            trust_remote_code=True,
            torch_dtype="auto",
        )

    if not use_device_map:
        try:
            moved_model = model.to(device)
        except Exception as exc:
            if device != "cuda" or not _is_cuda_oom(torch, exc) or VLM_DEVICE != "auto":
                raise
            logger.warning(
                "CUDA OOM while moving %s to GPU; falling back to CPU. "
                "Install accelerate for automatic GPU/CPU layer dispatch on "
                "small-VRAM cards.",
                model_id,
            )
            del model
            gc.collect()
            torch.cuda.empty_cache()
            device = "cpu"
            model = AutoModelForImageTextToText.from_pretrained(
                model_id,
                trust_remote_code=True,
                torch_dtype="auto",
            )
            moved_model = model.to(device)
        if moved_model is not None:
            model = moved_model

    model.eval()
    _vlm_cache[model_id] = (model, processor)
    logger.info("VLM loaded: %s (device=%s)", model_id, device)
    return model, processor


def _extract_from_image(
    image_path,
    prompt,
    *,
    model_id=None,
    use_cache=True,
    raw_dir=RAW_DIR,
    cache_dir=CACHE_DIR,
):
    """Extract JSON data from a chart image using a vision language model.

    Args:
        image_path: Path to the chart PNG image.
        prompt: Text prompt for the model.
        model_id: HuggingFace model identifier (defaults to *VLM_MODEL_ID*).
        use_cache: Whether to read/write the JSON cache.
    """
    if use_cache:
        cached = _load_cache(image_path, raw_dir=raw_dir, cache_dir=cache_dir)
        if cached is not None:
            return cached

    model, processor = _load_vlm(model_id)

    import torch
    from PIL import Image

    fname = os.path.basename(image_path)
    image = Image.open(image_path).convert("RGB")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }
    ]

    for attempt in range(1, VLM_MAX_RETRIES + 1):
        raw_text = ""
        try:
            chat_template_kwargs = {
                "tokenize": False,
                "add_generation_prompt": True,
            }
            if _chat_template_supports_kwarg(processor, "enable_thinking"):
                # Disable thinking mode if the template supports it (Qwen3+).
                chat_template_kwargs["enable_thinking"] = False
            text_input = processor.apply_chat_template(
                messages,
                **chat_template_kwargs,
            )

            inputs = processor(
                text=[text_input], images=[image], return_tensors="pt"
            ).to(model.device)

            with torch.no_grad():
                output_ids = model.generate(
                    **inputs,
                    max_new_tokens=VLM_MAX_NEW_TOKENS,
                    do_sample=False,
                    temperature=None,
                    pad_token_id=processor.tokenizer.eos_token_id,
                )

            generated = output_ids[0][inputs["input_ids"].shape[1] :]
            raw_text = processor.decode(generated, skip_special_tokens=True).strip()

            # Empty model output — no point retrying identically
            if not raw_text:
                logger.warning(
                    "Empty model output for %s (attempt %d/%d)",
                    fname,
                    attempt,
                    VLM_MAX_RETRIES,
                )
                if attempt < VLM_MAX_RETRIES:
                    continue
                return None

            # Strip closed thinking blocks (Qwen3.5 reasoning mode)
            text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()

            # If nothing remains (JSON inside thinking block or
            # truncated before closing tag), strip bare tags.
            if not text:
                text = re.sub(r"</?think>", "", raw_text).strip()

            # Extract JSON from the response
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
            else:
                brace_match = re.search(
                    r'\{[^{}]*"months"[^{}]*\{[^}]*\}[^}]*\}', text, re.DOTALL
                )
                if brace_match:
                    text = brace_match.group(0)
                elif not text.lstrip().startswith("{"):
                    # Last resort: find any JSON object in the text
                    any_json = re.search(r"\{[^}]+\}", text)
                    if any_json:
                        text = any_json.group(0)

            data = json.loads(text)
            if use_cache:
                _save_cache(image_path, data, raw_dir=raw_dir, cache_dir=cache_dir)
            return data

        except json.JSONDecodeError as e:
            parsed = _parse_structured_monthly_response(raw_text)
            if parsed is not None:
                if use_cache:
                    _save_cache(
                        image_path,
                        parsed,
                        raw_dir=raw_dir,
                        cache_dir=cache_dir,
                    )
                return parsed

            logger.warning(
                "JSON parse error for %s: %s (raw %d chars)",
                fname,
                e,
                len(raw_text),
            )
            if attempt < VLM_MAX_RETRIES:
                logger.info("  Retrying (%d/%d)...", attempt, VLM_MAX_RETRIES)
                continue
            return None

        except Exception as e:
            if attempt < VLM_MAX_RETRIES:
                logger.warning(
                    "Error for %s: %s — retrying (%d/%d)",
                    fname,
                    e,
                    attempt,
                    VLM_MAX_RETRIES,
                )
                continue
            logger.error("Persistent error for %s: %s", fname, e)
            return None

    return None


def extract_data(
    target: str = "local",
    raw_dir=None,
    processed_dir=None,
    cache_dir=None,
):
    """Phase 2: Extract bar chart data to CSV using a local vision language model.

    Args:
        target: Storage target — ``"local"`` (default) or ``"database"``.
    """
    persist_to_db = target == "database"
    save_local = target == "local"
    use_cache = True
    raw_dir = os.fspath(raw_dir) if raw_dir is not None else RAW_DIR
    processed_dir = (
        os.fspath(processed_dir) if processed_dir is not None else PROCESSED_DIR
    )
    cache_dir = os.fspath(cache_dir) if cache_dir is not None else CACHE_DIR
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("pandas not installed. Run: pip install pandas") from exc

    def _month_sum(d):
        """Sum the monthly values in an extraction result."""
        return sum(
            int(v)
            for v in d.get("months", {}).values()
            if v is not None and str(v).lstrip("-").isdigit()
        )

    # Pre-load the primary vision model (downloads on first use)
    _load_vlm(VLM_MODEL_ID)

    logger.info(
        "Sovon extraction starting (model=%s, max_new_tokens=%d)",
        VLM_MODEL_ID,
        VLM_MAX_NEW_TOKENS,
    )

    if save_local:
        os.makedirs(processed_dir, exist_ok=True)

    all_monthly_rows = []
    api_calls = 0
    db_rows_upserted = 0
    processed_images = 0
    upsert_sovon_mortality_fn = None

    if persist_to_db:
        try:
            from avian_biosurveillance.db.loaders import upsert_sovon_mortality

            upsert_sovon_mortality_fn = upsert_sovon_mortality
        except Exception as exc:
            logger.error("DB upsert setup for sovon data failed: %s", exc)

    raw_path = Path(raw_dir)
    if not raw_path.exists():
        logger.warning("No chart images found in %s", raw_dir)
        return

    species_dirs = sorted(
        d for d in raw_path.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    if not species_dirs:
        logger.warning("No chart images found in %s", raw_dir)
        return

    # Count work
    total_images = sum(len(list(d.glob("monthly_*.png"))) for d in species_dirs)
    cached_count = (
        sum(
            1
            for d in species_dirs
            for img in d.glob("monthly_*.png")
            if _load_cache(
                str(img),
                raw_dir=raw_dir,
                cache_dir=cache_dir,
            )
            is not None
        )
        if use_cache
        else 0
    )
    remaining = total_images - cached_count

    logger.info(
        "Extraction scope: %d images (%d cached, %d to process)",
        total_images,
        cached_count,
        remaining,
    )

    for sp_dir in species_dirs:
        bird_folder = sp_dir.name
        bird_english = FOLDER_TO_ENGLISH.get(bird_folder, bird_folder)
        species_info = SPECIES.get(bird_folder)

        # ── Monthly charts ──
        monthly_files = sorted(sp_dir.glob("monthly_*.png"))
        species_monthly = []

        sp_ok = 0
        sp_cache = 0
        sp_fail = 0
        for img_path in monthly_files:
            m = re.search(r"monthly_(\d{4})", img_path.name)
            if not m:
                continue
            file_year = int(m.group(1))
            image_rows = []
            is_cached = (
                use_cache
                and _load_cache(
                    str(img_path),
                    raw_dir=raw_dir,
                    cache_dir=cache_dir,
                )
                is not None
            )

            if not is_cached:
                api_calls += 1
            data = _extract_from_image(
                str(img_path),
                MONTHLY_PROMPT,
                use_cache=use_cache,
                raw_dir=raw_dir,
                cache_dir=cache_dir,
            )

            if data and "months" in data:
                if is_cached:
                    sp_cache += 1
                else:
                    sp_ok += 1

                # -- Ground-truth species names from config (never trust LLM)
                sp_dutch = (
                    species_info["dutch"] if species_info else data.get("species", "")
                )
                sp_eng = bird_english

                # -- Year: trust filename over LLM
                year = file_year

                # -- Validate: sum of months vs reported n_total
                raw_n = data.get("n_total")
                n_total = int(raw_n) if raw_n is not None else None
                month_sum = _month_sum(data)

                # On mismatch, try the fallback (larger) model
                if n_total is not None and month_sum != n_total:
                    use_fallback = (
                        VLM_FALLBACK_MODEL_ID and VLM_FALLBACK_MODEL_ID != VLM_MODEL_ID
                    )
                    if use_fallback:
                        api_calls += 1
                        fb = _extract_from_image(
                            str(img_path),
                            MONTHLY_PROMPT,
                            model_id=VLM_FALLBACK_MODEL_ID,
                            use_cache=False,
                            raw_dir=raw_dir,
                            cache_dir=cache_dir,
                        )
                        if fb and "months" in fb:
                            fb_sum = _month_sum(fb)
                            fb_n = (
                                int(fb["n_total"])
                                if fb.get("n_total") is not None
                                else None
                            )
                            if (fb_n is not None and fb_sum == fb_n) or (
                                fb_n is not None
                                and abs(fb_sum - fb_n) < abs(month_sum - n_total)
                            ):
                                data = fb
                                n_total, month_sum = fb_n, fb_sum
                                if use_cache:
                                    _save_cache(
                                        str(img_path),
                                        data,
                                        raw_dir=raw_dir,
                                        cache_dir=cache_dir,
                                    )

                for month_str, count in data["months"].items():
                    try:
                        month_int = int(month_str)
                        count_int = int(count)
                    except (ValueError, TypeError):
                        continue
                    row = {
                        "species_english": sp_eng,
                        "species_dutch": sp_dutch,
                        "sovon_species_id": (
                            species_info["id"] if species_info else None
                        ),
                        "year": year,
                        "month": month_int,
                        "dead_bird_count": count_int,
                        "n_total": n_total,
                        "source_image": str(img_path),
                    }
                    image_rows.append(row)
                    species_monthly.append(row)
                    all_monthly_rows.append(row)

                if image_rows and upsert_sovon_mortality_fn is not None:
                    try:
                        n = upsert_sovon_mortality_fn(image_rows)
                        db_rows_upserted += n
                    except Exception as exc:
                        logger.error(
                            "DB upsert for sovon data failed for %s/%s: %s",
                            bird_english,
                            file_year,
                            exc,
                        )
            else:
                sp_fail += 1

            processed_images += 1
            if (
                processed_images == 1
                or processed_images % 10 == 0
                or processed_images == total_images
            ):
                logger.info(
                    "Sovon extraction progress: %d/%d images complete",
                    processed_images,
                    total_images,
                )

        # Single-line per-species summary
        parts = []
        if sp_ok:
            parts.append(f"{sp_ok} extracted")
        if sp_cache:
            parts.append(f"{sp_cache} cached")
        if sp_fail:
            parts.append(f"{sp_fail} failed")
        logger.info(
            "  %-25s %s",
            bird_english,
            ", ".join(parts) or "nothing",
        )

        if species_monthly and save_local:
            df = (
                pd.DataFrame(species_monthly)
                .sort_values(["year", "month"])
                .reset_index(drop=True)
            )
            path = os.path.join(processed_dir, f"{bird_folder}_monthly.csv")
            df.to_csv(path, index=False)

    # ── Combined CSV ──
    if all_monthly_rows and save_local:
        df = (
            pd.DataFrame(all_monthly_rows)
            .sort_values(["species_english", "year", "month"])
            .reset_index(drop=True)
        )
        path = os.path.join(processed_dir, "all_monthly.csv")
        df.to_csv(path, index=False)
        logger.info("Saved combined CSV: %s (%d rows)", path, len(df))

    logger.info(
        "Sovon extraction complete — %d rows, %d API calls",
        len(all_monthly_rows),
        api_calls,
    )

    if db_rows_upserted:
        logger.info("DB: upserted %d sovon mortality rows total", db_rows_upserted)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════


def main():
    download_only = "--download-only" in sys.argv
    extract_only = "--extract-only" in sys.argv
    target_flag = "local"
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1] in ("local", "database"):
            target_flag = sys.argv[idx + 1]

    if download_only:
        download_charts()
    elif extract_only:
        extract_data(target=target_flag)
    else:
        # Run both phases
        has_charts = download_charts()
        if has_charts:
            extract_data(target=target_flag)
        else:
            logger.info("No charts available — skipping extraction.")


if __name__ == "__main__":
    main()
