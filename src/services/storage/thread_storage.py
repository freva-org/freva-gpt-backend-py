from __future__ import annotations
"""
Disk-based storage of conversations (Rust: thread_storage.rs).

Paths
-----
• Threads on disk: ./threads/{thread_id}.txt   (JSON Lines; one StreamVariant per line)
• RW dir for templating, artifacts, etc: ./rw_dir/{user_id}/{thread_id}

Formats
-------
• JSON lines — each line is a JSON object with a `variant` discriminator,
  compatible with StreamVariant classes (Pydantic v2 discriminated union).

Public API
----------
- append_thread(thread_id, content: Conversation) -> None
- read_thread(thread_id) -> Conversation
- extract_variants_from_string(raw: str) -> Conversation
- recursively_create_dir_at_rw_dir(user_id, thread_id) -> None
"""

import json
import logging
from pathlib import Path
from typing import List, Dict

from src.core.logging_setup import configure_logging
from src.services.streaming.stream_variants import (
    # Types
    Conversation,
    # Helpers
    cleanup_conversation,
    from_json_to_sv,
    from_sv_to_json,
)

logger = logging.getLogger(__name__)
configure_logging()

THREADS_DIR = Path("./threads")
RW_DIR_ROOT = Path("./rw_dir")


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
