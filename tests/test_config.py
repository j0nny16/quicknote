"""Config loading: YAML shape, env overrides and the secret indirection."""

from __future__ import annotations

import pytest

from quicknotes.config import ConfigError, load_config

MINIMAL = """
data_dir: /tmp/qn
threshold_words: 10
source:
  type: telegram
  allowed_user_ids: [1]
sinks:
  - type: anytype
    space_id: space-from-yaml
"""


def write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_minimal_config_with_defaults(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL))

    assert cfg.threshold_words == 10
    assert cfg.enricher.type == "anthropic"
    assert cfg.enricher.model == "claude-haiku-4-5"
    assert cfg.transcriber.base_url == "http://whisper:8000/v1"
    assert cfg.sinks[0].type_key == "quicknote"
    assert cfg.db_path.name == "quicknotes.db"


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_sinks_must_not_be_empty(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "sinks: []\n"))


def test_env_overrides_user_ids_space_and_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222")
    monkeypatch.setenv("ANYTYPE_SPACE_ID", "space-from-env")
    monkeypatch.setenv("QUICKNOTES_DATA_DIR", "/data-from-env")

    cfg = load_config(write(tmp_path, MINIMAL))

    assert cfg.source.allowed_user_ids == [111, 222]
    assert cfg.sinks[0].space_id == "space-from-env"
    assert str(cfg.data_dir) == "/data-from-env"


def test_secrets_are_read_from_the_named_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-123")
    monkeypatch.setenv("ANYTYPE_API_KEY", "key-456")
    cfg = load_config(write(tmp_path, MINIMAL))

    assert cfg.source.token() == "tok-123"
    assert cfg.sinks[0].api_key() == "key-456"


def test_unset_secret_raises_a_helpful_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = load_config(write(tmp_path, MINIMAL))

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        cfg.source.token()


def test_optional_secret_is_allowed_to_be_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = load_config(write(tmp_path, MINIMAL + "transcriber:\n  api_key_env: GROQ_API_KEY\n"))

    assert cfg.transcriber.api_key() is None


def test_tag_generation_is_off_by_default(tmp_path):
    """Tags cost tokens on every note; opting in should be explicit."""
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.enricher.generate_tags is False


def test_tag_generation_can_be_enabled(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL + "enricher:\n  generate_tags: true\n"))
    assert cfg.enricher.generate_tags is True
