from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
import logging

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .thread_storage import cleanup_conversation
from src.services.streaming.litellm_client import acomplete, first_text
from src.services.streaming.stream_variants import (
    StreamVariant, SVUser,
    from_sv_to_json, from_json_to_sv, 
)
from src.core.available_chatbots import default_chatbot
from src.core.auth import get_mongodb_uri
from src.core.settings import get_settings

log = logging.getLogger(__name__)

# ==== Config from settings.py (singleton) ====
_settings = get_settings()
MONGODB_DATABASE_NAME = _settings.MONGODB_DATABASE_NAME
MONGODB_COLLECTION_NAME = _settings.MONGODB_COLLECTION_NAME

# ==== Model ====

@dataclass
class MongoDBThread:
    user_id: str
    thread_id: str
    date: str  # ISO 8601
    topic: str
    content: List[StreamVariant]

# ---------- helpers: (de)serialize ----------
def _serialize_sv_list(items: List[StreamVariant]) -> List[dict]:
    out: List[dict] = []
    for v in items:
        try:
            out.append(from_sv_to_json(v))
        except Exception as e:
            # last resort: Pydantic dump
            log.warning("serialize fallback for %r: %s", getattr(v, "variant", type(v)), e)
            out.append(v.model_dump())
    return out

def _deserialize_sv_list(items: List[dict]) -> List[StreamVariant]:
    out: List[StreamVariant] = []
    for obj in items or []:
        try:
            out.append(from_json_to_sv(obj))
        except Exception as e:
            log.warning("deserialize failure for %r: %s", obj, e)
            # skip malformed rows rather than crashing
    return out

# ==== Connection ====

async def get_database(vault_url: str) -> AsyncIOMotorDatabase:
    """
    Parity with Rust: fetch URI from vault via auth.get_mongodb_uri, connect with Motor.
    If connection fails, retry once without URI options (strip trailing ?query).
    """
    mongodb_uri = await get_mongodb_uri(vault_url)

    try:
        client = AsyncIOMotorClient(mongodb_uri)
        return client[MONGODB_DATABASE_NAME]
    except Exception:
        # Rust-style fallback: strip query options and retry once
        if "?" in mongodb_uri:
            stripped = mongodb_uri.rsplit("?", 1)[0]
            try:
                client = AsyncIOMotorClient(stripped)
                return client[MONGODB_DATABASE_NAME]
            except Exception:
                pass
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB")

# ==== Summarization for topic ====

def _fallback_topic(raw: str | None) -> str:
    if not raw:
        return "Untitled"
    # naive single-line truncation
    s = " ".join(raw.split())
    return (s[:80] + "…") if len(s) > 80 else s

async def summarize_topic(topic: str) -> str:
    """
    Try LiteLLM; on any failure, return a safe fallback so requests don't crash.
    """
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
    
# ==== CRUD ====

async def append_thread(
    thread_id: str,
    user_id: str,
    content: List[StreamVariant],
    database: AsyncIOMotorDatabase,
) -> None:
    content = cleanup_conversation(content)
    if not content:
        return

    coll = database[MONGODB_COLLECTION_NAME]

    existing = await coll.find_one({"thread_id": thread_id})
    if existing:
        existing_sv = _deserialize_sv_list(existing.get("content", []))
        merged_sv: List[StreamVariant] = existing_sv + content
        # topic: keep existing if present
        topic = existing.get("topic", "") or None
    else:
        merged_sv = content
        topic = None

    # compute topic if missing
    first_user_text = next((sv.text if isinstance(sv, SVUser) else getattr(sv, "content", None)
                            for sv in merged_sv
                            if isinstance(sv, SVUser)), None)
    if not topic:
        try:
            topic = await summarize_topic(first_user_text or "Untitled")
        except Exception as e:
            log.warning("append_thread: summarize_topic failed: %s", e)
            topic = _fallback_topic(first_user_text or "Untitled")

    doc = {
        "user_id": user_id,
        "thread_id": thread_id,
        "date": _iso_now(),
        "topic": topic,
        "content": _serialize_sv_list(merged_sv),  # <- store JSON-safe dicts
    }

    if existing:
        await coll.update_one({"thread_id": thread_id}, {"$set": doc}, upsert=True)
    else:
        await coll.insert_one(doc)


async def read_thread(thread_id: str, database: AsyncIOMotorDatabase) -> List[StreamVariant]:
    coll = database[MONGODB_COLLECTION_NAME]
    doc = await coll.find_one({"thread_id": thread_id})
    if not doc:
        raise FileNotFoundError("Thread not found")
    return doc.get("content", [])


async def read_threads(user_id: str, database: AsyncIOMotorDatabase) -> List[MongoDBThread]:
    coll = database[MONGODB_COLLECTION_NAME]
    cursor = coll.find({"user_id": user_id}).sort([("date", -1)]).limit(10)
    docs = await cursor.to_list(length=10)
    return [
        MongoDBThread(
            user_id=d["user_id"],
            thread_id=d["thread_id"],
            date=d["date"],
            topic=d.get("topic", ""),
            content=d.get("content", []),
        )
        for d in docs
    ]

# ==== utils ====

def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()