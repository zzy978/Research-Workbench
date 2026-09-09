"""Model endpoints and credentials must remain isolated across providers."""

import os
import runpy
from pathlib import Path

import dotenv
import pytest


@pytest.fixture
def read_settings(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: None)
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "DEEPSEEK_", "EMBEDDING_", "LLM_PROVIDER")):
            monkeypatch.delenv(key)

    def read(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        return runpy.run_path(str(Path(__file__).parents[2] / "src/deepresearch_agent/config/settings.py"))

    return read


def test_deepseek_and_embeddings_have_separate_credentials(read_settings):
    settings = read_settings(
        LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="deepseek-test",
        DEEPSEEK_MODEL="deepseek-v4-pro",
        EMBEDDING_API_KEY="embedding-test",
        EMBEDDING_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1",
        EMBEDDING_MODEL="text-embedding-v4", EMBEDDING_DIMENSIONS=1024,
    )
    llm = settings["OPENAI_LLM_CONFIG"]
    embeddings = settings["OPENAI_EMBEDDING_CONFIG"]
    assert llm["api_key"] == "deepseek-test"
    assert llm["base_url"] == "https://api.deepseek.com"
    assert llm["model"] == "deepseek-v4-pro"
    assert settings["OPENAI_API_KEY"] == "deepseek-test"  # health and background jobs
    assert embeddings["api_key"] == "embedding-test"
    assert embeddings["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert embeddings["model"] == "text-embedding-v4"
    assert embeddings["dimensions"] == 1024


def test_legacy_shared_endpoint_still_works(read_settings):
    settings = read_settings(
        OPENAI_API_KEY="legacy-test", OPENAI_BASE_URL="https://legacy.example/v1",
        OPENAI_LLM_MODEL="legacy-chat", OPENAI_EMBEDDINGS_MODEL="legacy-embedding",
        OPENAI_EMBEDDING_DIMENSIONS=768,
    )
    assert settings["OPENAI_LLM_CONFIG"]["api_key"] == "legacy-test"
    assert settings["OPENAI_EMBEDDING_CONFIG"]["api_key"] == "legacy-test"
    assert settings["OPENAI_EMBEDDING_CONFIG"]["base_url"] == "https://legacy.example/v1"
    assert settings["OPENAI_EMBEDDING_CONFIG"]["dimensions"] == 768


def test_independent_embeddings_never_inherit_chat_key(read_settings):
    settings = read_settings(
        LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="deepseek-test",
        OPENAI_API_KEY="legacy-test",
        EMBEDDING_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1",
        EMBEDDING_API_KEY="",
    )
    assert settings["OPENAI_EMBEDDING_CONFIG"]["api_key"] == ""


def test_deepseek_without_embedding_config_does_not_reuse_deepseek(read_settings):
    settings = read_settings(LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="deepseek-test")
    assert settings["OPENAI_EMBEDDING_CONFIG"]["api_key"] == ""
    assert settings["OPENAI_EMBEDDING_CONFIG"]["base_url"] == ""


def test_unknown_llm_provider_rejected(read_settings):
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        read_settings(LLM_PROVIDER="typo")
