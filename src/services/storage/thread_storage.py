from __future__ import annotations
from abc import ABC, abstractmethod

import logging
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path

from src.services.streaming.stream_variants import StreamVariant, SVUser
from src.services.streaming.litellm_client import acomplete, first_text
from src.core.available_chatbots import default_chatbot


log = logging.getLogger(__name__)

RW_DIR_ROOT = Path("./rw_dir")

# ──────────────────────────── Model ───────────────────────────────────

@dataclass
class Thread:
    user_id: str
    thread_id: str
    date: str  # ISO 8601
    topic: str
    content: List[StreamVariant]

# ────────────────────────── Base Class ─────────────────────────────────

class ThreadStorage(ABC):
        
    @abstractmethod
    async def save_thread(
        self,
        thread_id: str,
        user_id: str,
        content: List[StreamVariant],
        append_to_existing: Optional[bool]
    ) -> None:
        ...

    @abstractmethod
    async def list_recent_threads(
        self,
        user_id: str,
        limit: int = 20,
    ) -> Tuple[List[Thread], int]:
        ...

    @abstractmethod
    async def read_thread(
        self,
        thread_id: str,
    ) -> List[Dict]:
        ...

    @abstractmethod
    async def update_thread_topic(
        self,
        thread_id: str,
        topic: str
    ) -> bool:
        ...

    @abstractmethod
    async def delete_thread(
        self,
        thread_id: str,
    ) -> bool:
        ...

    @abstractmethod
    async def save_feedback(
        self,
        thread_id: str,
        user_id: str,
        index: int,
        feedback: str,
    ) -> bool:
        ...


# ──────────────────── Helper Functions ──────────────────────────────

def create_dir_at_rw_dir(
    user_id: str, 
    thread_id: str
) -> None:
    """
    Create rw_dir/{user_id}/{thread_id}. On failure (e.g., non-alphanumeric user_id),
    retry with a sanitized user_id (keep only [A-Za-z0-9]). Logs but never raises.
    """
    rw_dir = RW_DIR_ROOT / user_id / thread_id
    try:
        rw_dir.mkdir(parents=True, exist_ok=True)
        log.debug("rw_dir created or exists: %s", rw_dir)
        return
    except Exception as e:
        log.debug("Failed to create rw_dir=%s, err=%s -- retrying with sanitized user_id", rw_dir, e)

    sanitized_user = "".join(c for c in user_id if c.isalnum()) or "user"
    sanitized = RW_DIR_ROOT / sanitized_user / thread_id
    try:
        sanitized.mkdir(parents=True, exist_ok=True)
        log.debug("Sanitized rw_dir created or exists: %s", sanitized)
    except Exception as e:
        log.error("Failed to create sanitized rw_dir=%s, err=%s", sanitized, e)

# ==== Summarization for topic ====

# TODO: update topic

def _fallback_topic(raw: str | None) -> str:
    if not raw:
        return "Untitled"
    # naive single-line truncation
    s = " ".join(raw.split())
    return (s[:80] + "…") if len(s) > 80 else s


async def summarize_topic(content: List[Dict]) -> str:
    """
    Try LiteLLM; on any failure, return a safe fallback so requests don't crash.
    Only the first user text is taken into account.
    """
    if isinstance(content[0], Dict):
        topic = next(
            (item.get("content", "") for item in content if item.get("variant") == "user"),
            "Untitled"
        )
    else:
        topic = next(
            (sv.text for sv in content if isinstance(sv, SVUser)),
            "Untitled"
        )

    prompt = (
        "Summarize this chat topic in at most ~12 words, neutral tone.\n\n"
        f"Topic:\n{(topic or '')[:2000]}"
    )
    try:
        resp = await acomplete(
            messages=[{"role": "user", "content": prompt}],
            model=default_chatbot(),
            max_tokens=50,
            temperature=0.2,
        )
        text = (first_text(resp) or "").strip()
        return text or _fallback_topic(topic)
    except Exception as e:
        log.warning("summarize_topic: falling back due to error: %s", e)
        return _fallback_topic(topic)