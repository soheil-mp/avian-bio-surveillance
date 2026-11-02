"""Tests for Sovon extraction fallbacks."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import avian_biosurveillance.ingestion.sovon as sovon_module
from avian_biosurveillance.ingestion.sovon import (
    SPECIES,
    _extract_from_image,
    _parse_structured_monthly_response,
    extract_data,
)


def test_parse_structured_monthly_response_handles_stepwise_vlm_output() -> None:
    """Structured reasoning output is converted into the expected dict."""
    raw_text = """STEP 1 — METADATA:
- Species: Vink
- n_total: 11
- Year: 2015

STEP 2 — BARS:
- Month 1: 2
- Month 2: 2
- Month 3: 0
- Month 4: 0
- Month 5: 2
- Month 6: 4
- Month 7: 2
- Month 8: 0
- Month 9: 0
- Month 10: 1
- Month 11: 1
- Month 12: 0

STEP 3 — VERIFY:
Sum of values: 2 + 2 + 0 + 0 + 2 + 4 + 2 + 0 + 0 + 1 + 1 + 0 = 14
"""

    parsed = _parse_structured_monthly_response(raw_text)  # type: ignore[no-untyped-call]

    assert parsed == {
        "species": "Vink",
        "year": 2015,
        "n_total": 11,
        "months": {
            "1": 2,
            "2": 2,
            "3": 0,
            "4": 0,
            "5": 2,
            "6": 4,
            "7": 2,
            "8": 0,
            "9": 0,
            "10": 1,
            "11": 1,
            "12": 0,
        },
    }


def test_parse_structured_monthly_response_strips_species_quotes() -> None:
    """Quoted species names in structured output are normalised."""
    raw_text = """STEP 1 — METADATA:
- Species: "Houtduif"
- n_total: 190
- Year: 2000

STEP 2 — BARS:
- Month 1: 10
- Month 2: 10
- Month 3: 20
- Month 4: 23
- Month 5: 25
- Month 6: 55
- Month 7: 17
- Month 8: 16
- Month 9: 3
- Month 10: 6
- Month 11: 3
- Month 12: 2
"""

    parsed = _parse_structured_monthly_response(raw_text)  # type: ignore[no-untyped-call]

    assert parsed is not None
    assert parsed["species"] == "Houtduif"
    assert parsed["year"] == 2000
    assert parsed["months"]["6"] == 55


def test_extract_data_uses_cache_for_database_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Database-target extraction should reuse the JSON cache between runs."""
    species_folder = next(iter(SPECIES))
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    cache_dir = tmp_path / "cache"
    image_path = raw_dir / species_folder / "monthly_2024.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png")

    extract_calls: list[dict[str, object]] = []
    upsert_calls: list[list[dict[str, object]]] = []

    def record_extract(
        image_path: str,
        _prompt: str,
        **kwargs: object,
    ) -> dict[str, object]:
        extract_calls.append(
            {
                "image_path": image_path,
                "use_cache": kwargs.get("use_cache"),
                "raw_dir": kwargs.get("raw_dir"),
                "cache_dir": kwargs.get("cache_dir"),
            }
        )
        return {
            "species": "Test",
            "year": 2024,
            "n_total": 1,
            "months": {
                "1": 1,
                "2": 0,
                "3": 0,
                "4": 0,
                "5": 0,
                "6": 0,
                "7": 0,
                "8": 0,
                "9": 0,
                "10": 0,
                "11": 0,
                "12": 0,
            },
        }

    def record_upsert(rows: list[dict[str, object]]) -> int:
        upsert_calls.append(list(rows))
        return len(rows)

    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._load_vlm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._extract_from_image",
        record_extract,
    )

    import avian_biosurveillance.db.loaders as loaders

    monkeypatch.setattr(
        loaders,
        "upsert_sovon_mortality",
        record_upsert,
    )

    extract_data(
        target="database",
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        cache_dir=cache_dir,
    )

    assert extract_calls == [
        {
            "image_path": str(image_path),
            "use_cache": True,
            "raw_dir": str(raw_dir),
            "cache_dir": str(cache_dir),
        }
    ]
    assert len(upsert_calls) == 1
    assert len(upsert_calls[0]) == 12


def test_extract_data_upserts_each_species_incrementally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Database-target extraction should persist each species as it finishes."""
    species_folders = list(SPECIES)[:2]
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    cache_dir = tmp_path / "cache"

    for index, species_folder in enumerate(species_folders, start=1):
        image_path = raw_dir / species_folder / f"monthly_{2023 + index}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"png")

    def record_extract(
        _image_path: str,
        _prompt: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "species": "Test",
            "year": 2024,
            "n_total": 1,
            "months": {
                "1": 1,
                "2": 0,
                "3": 0,
                "4": 0,
                "5": 0,
                "6": 0,
                "7": 0,
                "8": 0,
                "9": 0,
                "10": 0,
                "11": 0,
                "12": 0,
            },
        }

    upsert_calls: list[list[dict[str, object]]] = []

    def record_upsert(rows: list[dict[str, object]]) -> int:
        upsert_calls.append(list(rows))
        return len(rows)

    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._load_vlm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._extract_from_image",
        record_extract,
    )

    import avian_biosurveillance.db.loaders as loaders

    monkeypatch.setattr(
        loaders,
        "upsert_sovon_mortality",
        record_upsert,
    )

    extract_data(
        target="database",
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        cache_dir=cache_dir,
    )

    assert len(upsert_calls) == 2
    assert [len(call) for call in upsert_calls] == [12, 12]


def test_extract_data_upserts_each_completed_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Database-target extraction should flush rows after each completed image."""
    species_folder = next(iter(SPECIES))
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    cache_dir = tmp_path / "cache"

    for year in (2023, 2024):
        image_path = raw_dir / species_folder / f"monthly_{year}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"png")

    def record_extract(
        _image_path: str,
        _prompt: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "species": "Test",
            "year": 2024,
            "n_total": 1,
            "months": {
                "1": 1,
                "2": 0,
                "3": 0,
                "4": 0,
                "5": 0,
                "6": 0,
                "7": 0,
                "8": 0,
                "9": 0,
                "10": 0,
                "11": 0,
                "12": 0,
            },
        }

    upsert_calls: list[list[dict[str, object]]] = []

    def record_upsert(rows: list[dict[str, object]]) -> int:
        upsert_calls.append(list(rows))
        return len(rows)

    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._load_vlm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._extract_from_image",
        record_extract,
    )

    import avian_biosurveillance.db.loaders as loaders

    monkeypatch.setattr(
        loaders,
        "upsert_sovon_mortality",
        record_upsert,
    )

    extract_data(
        target="database",
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        cache_dir=cache_dir,
    )

    assert len(upsert_calls) == 2
    assert [len(call) for call in upsert_calls] == [12, 12]


def test_extract_from_image_caps_generation_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sovon extraction should use the configured generation budget."""

    class FakeNoGrad:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeTensor:
        shape = (1, 1)

    class FakeInputs(dict[str, object]):
        def __init__(self) -> None:
            super().__init__(input_ids=FakeTensor())

        def to(self, _device: object) -> FakeInputs:
            return self

    class FakeProcessor:
        def __init__(self) -> None:
            self.chat_template = "{{ add_generation_prompt }}"
            self.chat_template_kwargs: dict[str, object] | None = None
            self.tokenizer = SimpleNamespace(eos_token_id=7)

        def apply_chat_template(self, *_args: object, **_kwargs: object) -> str:
            self.chat_template_kwargs = _kwargs
            return "prompt"

        def __call__(self, *_args: object, **_kwargs: object) -> FakeInputs:
            return FakeInputs()

        def decode(self, *_args: object, **_kwargs: object) -> str:
            return (
                '{"species":"Test","year":2024,"n_total":1,'
                '"months":{"1":1,"2":0,"3":0,"4":0,"5":0,"6":0,'
                '"7":0,"8":0,"9":0,"10":0,"11":0,"12":0}}'
            )

    class FakeModel:
        def __init__(self) -> None:
            self.device = "cpu"
            self.kwargs: dict[str, object] | None = None

        def generate(self, **kwargs: object) -> list[list[int]]:
            self.kwargs = kwargs
            return [[0, 1]]

    class FakeImage:
        def convert(self, _mode: str) -> FakeImage:
            return self

    fake_model = FakeModel()
    fake_processor = FakeProcessor()

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(no_grad=FakeNoGrad))
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._load_vlm",
        lambda *_args, **_kwargs: (fake_model, fake_processor),
    )
    monkeypatch.setattr(
        "PIL.Image.open",
        lambda *_args, **_kwargs: FakeImage(),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon.VLM_MAX_NEW_TOKENS",
        321,
    )

    parsed = _extract_from_image(
        str(tmp_path / "monthly_2024.png"),
        "prompt",
        use_cache=False,
    )

    assert parsed is not None
    assert fake_model.kwargs is not None
    assert fake_model.kwargs["max_new_tokens"] == 321
    assert fake_processor.chat_template_kwargs is not None
    assert "enable_thinking" not in fake_processor.chat_template_kwargs


def test_extract_from_image_disables_thinking_for_reasoning_templates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reasoning templates should still receive enable_thinking=False."""

    class FakeNoGrad:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeTensor:
        shape = (1, 1)

    class FakeInputs(dict[str, object]):
        def __init__(self) -> None:
            super().__init__(input_ids=FakeTensor())

        def to(self, _device: object) -> FakeInputs:
            return self

    class FakeProcessor:
        def __init__(self) -> None:
            self.chat_template = "{{ enable_thinking }} {{ add_generation_prompt }}"
            self.chat_template_kwargs: dict[str, object] | None = None
            self.tokenizer = SimpleNamespace(eos_token_id=7)

        def apply_chat_template(self, *_args: object, **_kwargs: object) -> str:
            self.chat_template_kwargs = _kwargs
            return "prompt"

        def __call__(self, *_args: object, **_kwargs: object) -> FakeInputs:
            return FakeInputs()

        def decode(self, *_args: object, **_kwargs: object) -> str:
            return (
                '{"species":"Test","year":2024,"n_total":1,'
                '"months":{"1":1,"2":0,"3":0,"4":0,"5":0,"6":0,'
                '"7":0,"8":0,"9":0,"10":0,"11":0,"12":0}}'
            )

    class FakeModel:
        device = "cpu"

        def generate(self, **_kwargs: object) -> list[list[int]]:
            return [[0, 1]]

    class FakeImage:
        def convert(self, _mode: str) -> FakeImage:
            return self

    fake_processor = FakeProcessor()

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(no_grad=FakeNoGrad))
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._load_vlm",
        lambda *_args, **_kwargs: (FakeModel(), fake_processor),
    )
    monkeypatch.setattr(
        "PIL.Image.open",
        lambda *_args, **_kwargs: FakeImage(),
    )

    parsed = _extract_from_image(
        str(tmp_path / "monthly_2024.png"),
        "prompt",
        use_cache=False,
    )

    assert parsed is not None
    assert fake_processor.chat_template_kwargs is not None
    assert fake_processor.chat_template_kwargs["enable_thinking"] is False


def test_load_vlm_uses_cuda_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default VLM loading should place the model on CUDA when available."""

    class FakeModel:
        def __init__(self) -> None:
            self.device = "cpu"
            self.eval_called = False

        def to(self, device: str) -> FakeModel:
            self.device = device
            return self

        def eval(self) -> None:
            self.eval_called = True

    class FakeProcessorAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> object:
            return object()

    class FakeModelAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> FakeModel:
            return fake_model

    fake_model = FakeModel()
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForImageTextToText=FakeModelAuto,
        AutoProcessor=FakeProcessorAuto,
    )

    sovon_module._vlm_cache.clear()
    monkeypatch.setattr(sovon_module, "VLM_DEVICE", "auto")
    monkeypatch.setattr(sovon_module, "_accelerate_available", lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model, _processor = sovon_module._load_vlm("fake-model")

    assert model is fake_model
    assert fake_model.device == "cuda"
    assert fake_model.eval_called is True
    sovon_module._vlm_cache.clear()


def test_load_vlm_disables_cudnn_sdp_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid cuDNN symbol failures while keeping CUDA inference enabled."""

    class FakeModel:
        def to(self, _device: str) -> FakeModel:
            return self

        def eval(self) -> None:
            return None

    class FakeProcessorAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> object:
            return object()

    class FakeModelAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> FakeModel:
            return FakeModel()

    cudnn_sdp_values: list[bool] = []
    fake_cudnn_backend = SimpleNamespace(enabled=True)
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True),
        backends=SimpleNamespace(
            cudnn=fake_cudnn_backend,
            cuda=SimpleNamespace(
                enable_cudnn_sdp=lambda enabled: cudnn_sdp_values.append(enabled),
            ),
            mps=SimpleNamespace(is_available=lambda: False),
        ),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForImageTextToText=FakeModelAuto,
        AutoProcessor=FakeProcessorAuto,
    )

    sovon_module._vlm_cache.clear()
    monkeypatch.setattr(sovon_module, "VLM_DEVICE", "auto")
    monkeypatch.setattr(sovon_module, "VLM_DISABLE_CUDNN", True)
    monkeypatch.setattr(sovon_module, "VLM_DISABLE_CUDNN_SDP", True)
    monkeypatch.setattr(sovon_module, "_accelerate_available", lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    sovon_module._load_vlm("fake-cudnn-backend-model")

    assert fake_cudnn_backend.enabled is False
    assert cudnn_sdp_values == [False]
    sovon_module._vlm_cache.clear()


def test_load_vlm_uses_accelerate_device_map_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small-GPU loading should use device_map when accelerate is installed."""

    class FakeModel:
        def __init__(self) -> None:
            self.eval_called = False
            self.to_called = False

        def to(self, _device: str) -> FakeModel:
            self.to_called = True
            return self

        def eval(self) -> None:
            self.eval_called = True

    class FakeProcessorAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> object:
            return object()

    class FakeModelAuto:
        @staticmethod
        def from_pretrained(*_args: object, **kwargs: object) -> FakeModel:
            from_pretrained_kwargs.update(kwargs)
            return fake_model

    fake_model = FakeModel()
    from_pretrained_kwargs: dict[str, object] = {}
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            mem_get_info=lambda: (6 * 1024**3, 6 * 1024**3),
        ),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForImageTextToText=FakeModelAuto,
        AutoProcessor=FakeProcessorAuto,
    )

    sovon_module._vlm_cache.clear()
    monkeypatch.setattr(sovon_module, "VLM_DEVICE", "auto")
    monkeypatch.setattr(sovon_module, "VLM_DEVICE_MAP", "auto")
    monkeypatch.setattr(sovon_module, "_accelerate_available", lambda: True)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model, _processor = sovon_module._load_vlm("fake-device-map-model")

    assert model is fake_model
    assert from_pretrained_kwargs["device_map"] == "auto"
    assert from_pretrained_kwargs["max_memory"] == {
        0: 6 * 1024**3 - sovon_module.VLM_CUDA_MEMORY_RESERVE_MB * 1024 * 1024,
        "cpu": "48GiB",
    }
    assert fake_model.to_called is False
    assert fake_model.eval_called is True
    sovon_module._vlm_cache.clear()


def test_load_vlm_falls_back_to_cpu_on_cuda_oom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto device selection should recover when the full model does not fit."""

    class FakeCudaOom(RuntimeError):
        pass

    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name
            self.device = "cpu"
            self.eval_called = False

        def to(self, device: str) -> FakeModel:
            if device == "cuda":
                raise FakeCudaOom("CUDA out of memory")
            self.device = device
            return self

        def eval(self) -> None:
            self.eval_called = True

    class FakeProcessorAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> object:
            return object()

    class FakeModelAuto:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> FakeModel:
            model = FakeModel(f"load-{len(loaded_models) + 1}")
            loaded_models.append(model)
            return model

    empty_cache_calls: list[bool] = []
    loaded_models: list[FakeModel] = []
    fake_torch = SimpleNamespace(
        OutOfMemoryError=FakeCudaOom,
        cuda=SimpleNamespace(
            is_available=lambda: True,
            empty_cache=lambda: empty_cache_calls.append(True),
        ),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForImageTextToText=FakeModelAuto,
        AutoProcessor=FakeProcessorAuto,
    )

    sovon_module._vlm_cache.clear()
    monkeypatch.setattr(sovon_module, "VLM_DEVICE", "auto")
    monkeypatch.setattr(sovon_module, "_accelerate_available", lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model, _processor = sovon_module._load_vlm("fake-oom-model")

    assert model is loaded_models[1]
    assert model.device == "cpu"
    assert model.eval_called is True
    assert len(loaded_models) == 2
    assert len(empty_cache_calls) == 1
    sovon_module._vlm_cache.clear()


def test_extract_data_skips_fallback_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A disabled fallback model should not trigger a second extraction pass."""

    species_folder = next(iter(SPECIES))
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    cache_dir = tmp_path / "cache"
    image_path = raw_dir / species_folder / "monthly_2024.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png")

    calls: list[str | None] = []

    def record_extract(
        _image_path: str,
        _prompt: str,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(kwargs.get("model_id") if "model_id" in kwargs else None)
        return {
            "species": "Test",
            "year": 2024,
            "n_total": 9,
            "months": {
                "1": 1,
                "2": 0,
                "3": 0,
                "4": 0,
                "5": 0,
                "6": 0,
                "7": 0,
                "8": 0,
                "9": 0,
                "10": 0,
                "11": 0,
                "12": 0,
            },
        }

    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._load_vlm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon._extract_from_image",
        record_extract,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.ingestion.sovon.VLM_FALLBACK_MODEL_ID",
        "",
    )

    extract_data(
        target="local",
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        cache_dir=cache_dir,
    )

    assert calls == [None]


def test_default_sovon_model_uses_vision_checkpoint() -> None:
    """The default Sovon model must be a multimodal checkpoint."""

    assert sovon_module.VLM_MODEL_ID == "Qwen/Qwen2.5-VL-3B-Instruct"


def test_default_sovon_generation_budget_is_bounded() -> None:
    """The default Sovon generation budget should stay modest on CPU workers."""

    assert sovon_module.VLM_MAX_NEW_TOKENS == 256
