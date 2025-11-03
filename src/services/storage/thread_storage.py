from __future__ import annotations
"""
Disk-based storage of conversations (Rust: thread_storage.rs).

Paths
-----
• Threads on disk: ./threads/{thread_id}.txt   (JSON Lines; one StreamVariant per line)
• RW dir for templating, artifacts, etc: ./rw_dir/{user_id}/{thread_id}

Formats
-------
• Primary: JSON lines — each line is a JSON object with a `variant` discriminator,
  compatible with StreamVariant classes (Pydantic v2 discriminated union).
• Legacy:  colon-separated fallback (mirrors old Rust extractor):
  - "<Variant>:<content>"                        # for most variants
  - "Code:<code>:<call_id>"
  - "CodeOutput:<output>:<call_id>"

Public API
----------
- append_thread(thread_id, content: Conversation) -> None
- read_thread(thread_id) -> Conversation
- extract_variants_from_string(raw: str) -> Conversation
- recursively_create_dir_at_rw_dir(user_id, thread_id) -> None
"""
# TODO drop legacy format support in future versions

import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

from pydantic import TypeAdapter

from src.core.logging_setup import configure_logging
from src.services.streaming.stream_variants import (
    # Types
    Conversation,
    StreamVariant,
    # Classes
    SVPrompt, SVUser, SVAssistant, SVCode, SVCodeOutput, SVImage,
    SVServerError, SVOpenAIError, SVCodeError, SVStreamEnd, SVServerHint,
    # Helpers
    cleanup_conversation,
    from_json_to_sv,
    from_sv_to_json,
    # Variant names
    PROMPT, USER, ASSISTANT, CODE, CODE_OUTPUT, IMAGE,
    SERVER_ERROR, OPENAI_ERROR, CODE_ERROR, STREAM_END, SERVER_HINT,
)

logger = logging.getLogger(__name__)
configure_logging()

THREADS_DIR = Path("./threads")
RW_DIR_ROOT = Path("./rw_dir")

# Pydantic adapters (fast & typed validation for union types)
_sv_adapter = TypeAdapter(StreamVariant)
_conv_adapter = TypeAdapter(List[StreamVariant])


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: JSON <-> StreamVariant
# ──────────────────────────────────────────────────────────────────────────────

def _variant_to_json_line(variant: StreamVariant) -> str:
    """Serialize a StreamVariant to a JSON line."""
    # model_dump gives a dict including the discriminator `variant`
    obj = variant.model_dump()
    return json.dumps(obj, ensure_ascii=False)


def _json_line_to_variant(line: str) -> Optional[StreamVariant]:
    """Parse a JSON line into a StreamVariant. Returns None on failure."""
    try:
        obj = json.loads(line)
    except Exception:
        return None
    try:
        return _sv_adapter.validate_python(obj)
    except Exception as e:
        logger.warning("Invalid JSON StreamVariant line skipped: %s; error=%s", line[:200], e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Legacy (colon-separated) parsing/encoding
# ──────────────────────────────────────────────────────────────────────────────

def _legacy_line_to_variant(line: str) -> Optional[StreamVariant]:
    """
    Parse legacy colon-separated encoding:
      - "<Variant>:<content>"
      - "Code:<code>:<call_id>"
      - "CodeOutput:<output>:<call_id>"
    """
    parts = line.split(":", 2)
    if not parts:
        return None
    var = parts[0]

    # Simple one-field variants
    if var in (PROMPT, USER, ASSISTANT, IMAGE, SERVER_ERROR, OPENAI_ERROR, CODE_ERROR, STREAM_END, SERVER_HINT):
        content = parts[1] if len(parts) > 1 else ""
        if var == PROMPT:
            return SVPrompt(payload=content)
        if var == USER:
            return SVUser(text=content)
        if var == ASSISTANT:
            return SVAssistant(text=content)
        if var == IMAGE:
            # No MIME in legacy path, assume png
            return SVImage(b64=content, mime="image/png")
        if var == SERVER_ERROR:
            return SVServerError(message=content)
        if var == OPENAI_ERROR:
            return SVOpenAIError(message=content)
        if var == CODE_ERROR:
            # Legacy can't encode call_id reliably; store message only
            return SVCodeError(message=content, call_id=None)
        if var == STREAM_END:
            return SVStreamEnd(message=content)
        if var == SERVER_HINT:
            # Could be JSON-ish content; keep as string
            return SVServerHint(data=content)

    # Two-field code shapes
    if var in (CODE, CODE_OUTPUT) and len(parts) >= 3:
        field1, field2 = parts[1], parts[2]
        if var == CODE:
            return SVCode(code=field1, call_id=field2)
        if var == CODE_OUTPUT:
            return SVCodeOutput(output=field1, call_id=field2)

    logger.warning("Unrecognized legacy line skipped: %r", line[:200])
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def append_thread(thread_id: str, content: Conversation) -> None:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    content = cleanup_conversation(content)
    if not content:
        return
    
    # convert to dicts
    to_write = []
    for v in content:
        try:
            v_dict = from_sv_to_json(v)
            to_write.append(json.dumps(v_dict, ensure_ascii=False))
        except Exception:
            # last-ditch legacy colon encoding (rare)
            v_dict = from_sv_to_json(v)
            var = v_dict.get("variant")
            c = v_dict.get("content")
            if isinstance(c, list):
                line = f"{var}:{':'.join(map(str, c))}"
            else:
                line = f"{var}:{c}"
            to_write.append(line)

    path = THREADS_DIR / f"{thread_id}.txt"
    with open(path, "a", encoding="utf-8") as f:
        for line in to_write:
            f.write(line + "\n")

def read_thread(thread_id: str) -> List[Dict]:
    path = THREADS_DIR / f"{thread_id}.txt"
    if not path.exists():
        raise FileNotFoundError("Thread not found")

    conv: List = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
            conv.append(obj)
        except Exception:
            pass
    return conv


def extract_variants_from_string(raw: str) -> Conversation:
    """
    Parse a JSONL string (one JSON object per line) into a list[StreamVariant].
    - Ignores blank lines and lines starting with '//' or '#'
    - Tries JSON per line; on failure, tries legacy colon-separated parsing
    - Preserves order
    """
    variants: List[StreamVariant] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip("\n")
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        v = _json_line_to_variant(line)
        if v is None:
            v = _legacy_line_to_variant(line)

        if v is not None:
            variants.append(v)
        else:
            logger.warning("Skipping unreadable examples.jsonl line: %r", line[:200])

    return variants


def recursively_create_dir_at_rw_dir(user_id: str, thread_id: str) -> None:
    """
    Create rw_dir/{user_id}/{thread_id}. On failure (e.g., non-alphanumeric user_id),
    retry with a sanitized user_id (keep only [A-Za-z0-9]). Logs but never raises.
    """
    rw_dir = RW_DIR_ROOT / user_id / thread_id
    try:
        rw_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("rw_dir created or exists: %s", rw_dir)
        return
    except Exception as e:
        logger.debug("Failed to create rw_dir=%s, err=%s -- retrying with sanitized user_id", rw_dir, e)

    sanitized_user = "".join(c for c in user_id if c.isalnum()) or "user"
    sanitized = RW_DIR_ROOT / sanitized_user / thread_id
    try:
        sanitized.mkdir(parents=True, exist_ok=True)
        logger.debug("Sanitized rw_dir created or exists: %s", sanitized)
    except Exception as e:
        logger.error("Failed to create sanitized rw_dir=%s, err=%s", sanitized, e)
