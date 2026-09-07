from __future__ import annotations

from pathlib import Path

import pytest

from model_router.config import ConfigError, RouterConfig, load_router_config

VALID = """
providers:
  - name: a
    kind: openai
    base_url: http://a/v1
    api_key_env: A_KEY
    pricing:
      m: {input_per_1m: 1, output_per_1m: 2}
routes:
  - alias: r
    candidates:
      - {provider: a, model: m}
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "router.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_loads_and_keys_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_router_config(write(tmp_path, VALID))
    assert config.route("r") is not None and config.route("nope") is None
    assert config.provider("a").api_key() is None
    monkeypatch.setenv("A_KEY", "secret")
    assert config.provider("a").api_key() == "secret"


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        (lambda t: t.replace("provider: a", "provider: zzz"), "unknown provider"),
        (lambda t: t.replace("model: m}", "model: other}"), "no price"),
        (
            lambda t: t + "  - alias: r\n    candidates:\n      - {provider: a, model: m}\n",
            "unique",
        ),
        (lambda t: t.replace("alias: r", "alias: a"), "shadow"),
        (lambda t: t.replace("kind: openai", "kind: carrier-pigeon"), "kind"),
        (
            lambda t: t.replace("max_retries", "max_retries") + "    max_retries: 99\n",
            "max_retries",
        ),
        (lambda t: "providers: []\nroutes: []\n", "providers"),
        (lambda t: "just a string", "mapping"),
        (lambda t: "providers: [\n", "YAML"),
    ],
)
def test_every_mistake_is_named(tmp_path: Path, mutation, fragment: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError) as info:
        load_router_config(write(tmp_path, mutation(VALID)))
    assert fragment.lower() in str(info.value).lower()


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_router_config(tmp_path / "absent.yaml")


def test_the_shipped_router_yaml_is_valid() -> None:
    config = load_router_config(Path(__file__).resolve().parent.parent / "router.yaml")
    assert config.route("demo") is not None
    assert {p.name for p in config.providers} >= {"fake", "openai", "anthropic"}


def test_model_validate_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError):
        RouterConfig.model_validate(
            {
                "providers": [
                    {
                        "name": "a",
                        "kind": "openai",
                        "base_url": "u",
                        "pricing": {"m": {"input_per_1m": 0, "output_per_1m": 0}},
                    }
                ],
                "routes": [
                    {
                        "alias": "r",
                        "strategy": "psychic",
                        "candidates": [{"provider": "a", "model": "m"}],
                    }
                ],
            }
        )
