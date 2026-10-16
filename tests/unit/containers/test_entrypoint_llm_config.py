"""Tests for standalone container model configuration compatibility."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def entrypoint_module():
    path = Path(__file__).parents[3] / "containers" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("forge_container_entrypoint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clear_model_env(monkeypatch):
    for name in (
        "LLM_BACKEND",
        "LLM_MODEL",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_standalone_container_requires_backend(entrypoint_module):
    with pytest.raises(ValueError, match="LLM_BACKEND is required"):
        entrypoint_module.resolve_llm_backend()


def test_credentials_do_not_infer_backend(entrypoint_module, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    with pytest.raises(ValueError, match="LLM_BACKEND is required"):
        entrypoint_module.resolve_llm_backend()


def test_standalone_container_requires_model(entrypoint_module, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "google-genai")
    with pytest.raises(ValueError, match="LLM_MODEL is required"):
        entrypoint_module.resolve_llm_model()


def test_explicit_backend_and_model_take_precedence(entrypoint_module, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex-ai")
    monkeypatch.setenv("LLM_MODEL", "gemini-custom")

    assert entrypoint_module.resolve_llm_backend() == "vertex-ai"
    assert entrypoint_module.resolve_llm_model() == "gemini-custom"


def test_openai_compatible_requires_base_url(entrypoint_module, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai-compatible")
    monkeypatch.setenv("LLM_MODEL", "custom-model")

    with pytest.raises(RuntimeError, match="OPENAI_BASE_URL is required"):
        entrypoint_module._create_llm_model()
