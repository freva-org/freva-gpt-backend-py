# src/core/available_chatbots.py
from __future__ import annotations

"""
Model catalog loader for LiteLLM config (YAML-based).

Behavior:
- Parse YAML with yaml.safe_load
- Collect every value found under any "model_name" key (string-like), anywhere in the document
- Preserve list order as encountered in YAML
- Ignore comments naturally (YAML parser drops them)
- Fatal (SystemExit) if the resulting list is empty

- model_is_reasoning: names starting with 'o3', 'o4', or 'gpt-5'
- model_is_gpt_5:    names starting with 'gpt-5'
- model_supports_images: names starting with 'gpt-4o', 'gpt-5', or 'gpt-4.1'
- model_ends_on_no_choice: names starting with 'qwen2_5'
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

import yaml

from freva_gpt.core.logging_setup import configure_logging

logger = configure_logging(__name__)

ENV_CONFIG_PATH = "LITELLM_CONFIG"
DEFAULT_CONFIG_BASENAME = "litellm_config.yaml"


def _fatal_no_models(msg: str) -> None:
    logger.error(msg)
    raise SystemExit(1)


def _as_str_or_none(value: Any) -> Optional[str]:
    """Coerce simple scalars to string; ignore complex types."""
    if isinstance(value, str):
        return value.strip()
    # If someone wrote model_name: 123, accept but coerce to str (and warn).
    if isinstance(value, (int, float)):
        s = str(value).strip()
        if s:
            logger.warning(
                "Coercing non-string model_name=%r to string '%s'.", value, s
            )
            return s
    return None


def _collect_model_names(node: Any, sink: List[str]) -> None:
    """
    Recursively traverse loaded YAML and collect values under 'model_name' keys
    (mimicking Rust's 'scan anywhere' behavior, but structured).
    """
    if isinstance(node, dict):
        # If we see a model_name at this level, collect it.
        if "model_name" in node:
            name = _as_str_or_none(node.get("model_name"))
            if name:
                sink.append(name)
            else:
                logger.warning(
                    "Ignoring non-string/empty model_name: %r",
                    node.get("model_name"),
                )

        # Recurse into all values to catch nested occurrences.
        for v in node.values():
            _collect_model_names(v, sink)

    elif isinstance(node, list):
        for item in node:
            _collect_model_names(item, sink)
    # Other scalar types are ignored.


def _discover_config_path() -> Path:
    """
    Resolve the LiteLLM YAML path with the following priority:
    1) LITELLM_CONFIG env var (absolute or relative)
    2) {CWD}/litellm_config.yaml
    3) Walk parents from this file and the CWD to find the first litellm_config.yaml
    """
    # 1) ENV override
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            return p
        logger.warning("LITELLM_CONFIG is set but not a file: %s", p)

    # 2) CWD
    cwd_candidate = Path.cwd() / DEFAULT_CONFIG_BASENAME
    if cwd_candidate.is_file():
        return cwd_candidate.resolve()

    # 3) Walk up from this file
    here = Path(__file__).resolve()
    for parent in [*here.parents, *Path.cwd().resolve().parents]:
        candidate = parent / DEFAULT_CONFIG_BASENAME
        if candidate.is_file():
            return candidate

    # Fallback to CWD for error messaging
    return cwd_candidate


def _load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _fatal_no_models(f"LiteLLM config not found at: {path}")
    except Exception as e:
        _fatal_no_models(f"Failed to read LiteLLM config at {path}: {e}")

    try:
        return yaml.safe_load(text)
    except Exception as e:
        _fatal_no_models(f"Failed to parse YAML at {path}: {e}")


@lru_cache(maxsize=1)
def available_chatbots() -> List[str]:
    """
    Returns an ordered list of model names discovered under 'model_name' keys
    in litellm_config.yaml. Fatal if empty.
    """
    path = _discover_config_path()
    data = _load_yaml(path)
    names: List[str] = []
    _collect_model_names(data, names)

    # Preserve order; do not deduplicate (matches the spirit of the Rust scan).
    filtered = [n for n in names if n]  # already warned on invalids

    if not filtered:
        _fatal_no_models(
            f"No available chatbots found in LiteLLM file at {path}."
        )
    logger.info("Available chatbots (%d): %s", len(filtered), filtered)
    return filtered


def default_chatbot() -> str:
    """
    Default chatbot is the first entry.
    """
    models = available_chatbots()
    return models[0]


# ──────────────────────────────────────────────────────────────────────────────
# Helper predicates
# ──────────────────────────────────────────────────────────────────────────────


def model_is_reasoning(model: str) -> bool:
    """
    True for names starting with 'o3', 'o4', or 'gpt-5'.
    """
    return model.startswith(("o3", "o4", "gpt-5"))


def model_is_gpt_5(model: str) -> bool:
    """
    True for names starting with 'gpt-5'.
    """
    return model.startswith("gpt-5")


def model_is_ollama(model: str) -> bool:
    """
    True for names starting with 'mistral', 'ministral', 'qwen', 'llama' or 'deepseek'.
    """
    ollama_list = (
        "mistral",
        "ministral",
        "qwen",
        "llama",
        "deepseek",
    )
    return model.startswith(ollama_list)


def model_supports_images(model: str) -> bool:
    """
    True for names starting with 'gpt-4o', 'gpt-5', or 'gpt-4.1'.
    """
    return model.startswith(("gpt-4o", "gpt-5", "gpt-4.1"))


def model_ends_on_no_choice(model: str) -> bool:
    """
    True for names starting with 'qwen2_5' (quirk in some Qwen APIs).
    """
    return model.startswith("qwen2_5")


def refresh_cache() -> None:
    """
    Clear memoized results (useful in tests or after config changes).
    """
    available_chatbots.cache_clear()


__all__ = [
    "available_chatbots",
    "default_chatbot",
    "model_is_reasoning",
    "model_is_gpt_5",
    "model_is_ollama",
    "model_supports_images",
    "model_ends_on_no_choice",
    "refresh_cache",
]
