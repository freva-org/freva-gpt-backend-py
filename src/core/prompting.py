from __future__ import annotations

"""
Prompt loading & assembly (non-streaming), single API for all models.

What this module does
---------------------
• Pick a prompt-set directory based on the model (GPT-5 falls back to baseline for now)
• Load 3 prompt assets: starting_prompt.txt, examples.jsonl, summary_prompt.txt
• Build OpenAI Chat messages in this order:
    1) System(starting_prompt)   [name="prompt"]
    2) Example conversation messages (from examples.jsonl via StreamVariants)
    3) System(summary_prompt)    [name="prompt"]

Differences from Rust (documented for future parity)
----------------------------------------------------
1) We expose toggles via help_convert_sv_ccrm:
   - include_images=False (Rust also dropped images in prompting; same effective behavior)
   - include_meta=True (Rust generally drops ServerHint/Errors/StreamEnd; we INCLUDE them)
     → Change this to False later to match Rust exactly.
2) GPT-5: placeholder — we do NOT use GPT-5-specific prompt files yet; we log a warning
   and fall back to the baseline prompt set.

Dependencies
------------
• src/core/stream_variants.parse_examples_jsonl, help_convert_sv_ccrm
• src/services/storage/thread_storage.recursively_create_dir_at_rw_dir
• src/core/available_chatbots.model_is_gpt_5
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from src.core.available_chatbots import model_is_gpt_5
from src.services.storage.thread_storage import recursively_create_dir_at_rw_dir
from src.core.stream_variants import parse_examples_jsonl, help_convert_sv_ccrm

logger = logging.getLogger(__name__)

# Filenames we expect in a prompt set
STARTING_TXT = "starting_prompt.txt"
SUMMARY_TXT = "summary_prompt.txt"
EXAMPLES_JL = "examples.jsonl"

# Where we’ll look for prompts
BASELINE_DIRS = [
    Path("src/prompt_library/baseline"),
]

GPT5_DIRS = [
    Path("src/prompt_library/gpt_5"),
]


def _resolve_baseline_dir() -> Path:
    for d in BASELINE_DIRS:
        if all((d / name).is_file() for name in (STARTING_TXT, SUMMARY_TXT, EXAMPLES_JL)):
            return d
    tried = [str(d.resolve()) for d in BASELINE_DIRS]
    raise FileNotFoundError(f"Baseline prompt set not found. Tried: {tried}")


def _resolve_gpt5_dir_or_placeholder() -> Path:
    # Placeholder policy: until GPT-5 is implemented, fall back to baseline.
    logger.warning("GPT-5 prompting is a placeholder; falling back to BASELINE prompt set.")
    return _resolve_baseline_dir()


def _pick_prompt_dir(model: str) -> Path:
    if model_is_gpt_5(model):
        return _resolve_gpt5_dir_or_placeholder()
    return _resolve_baseline_dir()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=256)
def _load_prompts(model: str) -> Dict[str, str]:
    """
    Load raw prompt assets for the given model (with GPT-5 placeholder fallback).

    Returns:
        {
          "starting": str,          # file contents
          "summary": str,           # file contents
          "examples_path": str      # absolute path to examples.jsonl
        }
    """
    prompt_dir = _pick_prompt_dir(model)
    starting = _read_text(prompt_dir / STARTING_TXT)
    summary = _read_text(prompt_dir / SUMMARY_TXT)
    examples_path = str((prompt_dir / EXAMPLES_JL).resolve())
    logger.debug("Loaded prompts for model='%s' from %s", model, prompt_dir)
    return {"starting": starting, "summary": summary, "examples_path": examples_path}


def _as_system_message(text: str) -> Dict[str, Any]:
    return {"role": "system", "content": text}


def _load_examples_as_messages(examples_path: str | Path) -> list[dict]:
    """
    Read examples.jsonl from disk, convert wire → class → OpenAI chat messages.
    Safe if missing/empty (returns []).
    """
    svs = parse_examples_jsonl(examples_path)  # wire → classes
    # Rust passes include_images = false for examples
    return help_convert_sv_ccrm(
        svs,
        include_images=False,
        include_meta=True,  # parity note: Rust typically drops meta; we keep for now
    )


def get_entire_prompt(user_id: str, thread_id: str, model: str) -> List[Dict[str, Any]]:
    """
    Build the full, ordered message list for a completion request (non-streaming).
    Order: [ System(starting), *examples, System(summary) ]
    """
    # Best-effort parity with Rust’s directory prep
    try:
        recursively_create_dir_at_rw_dir(user_id, thread_id)
    except Exception:
        logger.debug("Could not ensure RW dir for user/thread; proceeding.", exc_info=True)

    assets = _load_prompts(model)
    messages: List[Dict[str, Any]] = []
    messages.append(_as_system_message(assets["starting"]))
    messages.extend(_load_examples_as_messages(assets["examples_path"]))
    messages.append(_as_system_message(assets["summary"]))

    # Optional: mark placeholder when model is GPT-5 (useful for debugging)
    if model_is_gpt_5(model):
        logger.info("GPT-5 placeholder active: baseline prompts used for model='%s'.", model)

    return messages


def get_entire_prompt_json(user_id: str, thread_id: str, model: str) -> str:
    """Return the entire prompt as JSON string (useful for debugging & tests)."""
    return json.dumps(get_entire_prompt(user_id, thread_id, model), ensure_ascii=False)
