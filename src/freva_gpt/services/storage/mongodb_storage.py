import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from freva_gpt.core.settings import get_settings
from freva_gpt.services.streaming.stream_variants import (
    StreamVariant,
    cleanup_conversation,
    from_json_to_sv,
    from_sv_to_json,
)

from .thread_storage import Thread, ThreadStorage, summarize_topic

log = logging.getLogger(__name__)

# ──────────────────── Config from settings.py ────────────────────────────

settings = get_settings()
MONGODB_DATABASE_NAME = settings.MONGO_DB_DATABASE_NAME
MONGODB_COLLECTION_NAME = settings.MONGO_DB_COLLECTION_NAME


class MongoThreadStorage(ThreadStorage):
    """PROD / shared implementation: store threads in MongoDB."""

    def __init__(self, vault_url: str) -> None:
        self.vault_url = vault_url
        self.db = None

    @classmethod
    async def create(cls, vault_url: str):
        self = cls(vault_url)
        self.db = await get_database(self.vault_url)
        return self

    async def save_thread(
        self,
        thread_id: str,
        user_id: str,
        content: List[StreamVariant],
        append_to_existing: Optional[bool] = False,
    ) -> None:
        content = cleanup_conversation(content)
        if not content:
            return

        coll = self.db[MONGODB_COLLECTION_NAME]

        existing = await coll.find_one({"thread_id": thread_id})
        if existing:
            if append_to_existing:
                existing_stream = existing.get("content", [])
                existing_sv = [from_json_to_sv(v) for v in existing_stream]
                merged_sv: List[StreamVariant] = existing_sv + content
            # topic: keep existing if present
            topic = existing.get("topic", "") or None
        else:
            merged_sv = content
            topic = None

        # compute topic if missing
        if not topic:
            topic = await summarize_topic(content or "Untitled")

        all_stream = [from_sv_to_json(v) for v in merged_sv] if merged_sv else []
        doc = {
            "user_id": user_id,
            "thread_id": thread_id,
            "date": datetime.now(timezone.utc),
            "topic": topic,
            "content": all_stream,
        }

        if existing:
            await coll.update_one(
                {"thread_id": thread_id}, {"$set": doc}, upsert=True
            )
        else:
            await coll.insert_one(doc)

    async def list_recent_threads(
        self,
        user_id: str,
        limit: int = 20,
    ) -> Tuple[List[Thread], int]:
        coll = self.db[MONGODB_COLLECTION_NAME]
        n_threads = await coll.count_documents({"user_id": user_id})
        cursor = coll.find({"user_id": user_id}).sort([("date", -1)]).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [
            Thread(
                user_id=d["user_id"],
                thread_id=d["thread_id"],
                date=d["date"],
                topic=d.get("topic", ""),
                content=d.get("content", []),
            )
            for d in docs
        ], n_threads

    async def read_thread(
        self,
        thread_id: str,
    ) -> List[Dict]:
        # TODO check the return
        coll = self.db[MONGODB_COLLECTION_NAME]
        doc = await coll.find_one({"thread_id": thread_id})
        if not doc:
            raise FileNotFoundError("Thread not found")
        return doc.get("content", [])

    async def update_thread_topic(self, thread_id: str, topic: str) -> bool:
        try:
            coll = self.db[MONGODB_COLLECTION_NAME]
            update_op = {"$set": {"topic": topic}}
            await coll.update_one({"thread_id": thread_id}, update_op)
            return True
        except:
            return False

    async def delete_thread(
        self,
        thread_id: str,
    ) -> bool:
        try:
            coll = self.db[MONGODB_COLLECTION_NAME]
            await coll.delete_one({"thread_id": thread_id})
            # TODO check the return
            return True
        except:
            return False


# ──────────────────── Connection ──────────────────────────────


async def get_mongodb_uri(vault_url: str) -> str:
    # 1) GET vault_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(vault_url)
    except Exception:
        # 503 ServiceUnavailable
        raise HTTPException(
            status_code=503, detail="Error sending request to vault."
        )
    if not r.is_success:
        # 502 BadGateway
        raise HTTPException(
            status_code=502,
            detail="Failed to get MongoDB URL. Is Nginx running correctly?",
        )

    # 2) Parse JSON and extract key
    try:
        data = r.json()
    except Exception:
        # 502 BadGateway
        raise HTTPException(
            status_code=502, detail="Vault response was malformed."
        )

    uri = data.get("mongodb.url") or data.get("mongo.url")
    if not uri:
        # 502 BadGateway
        raise HTTPException(
            status_code=502, detail="MongoDB URL not found in vault response."
        )
    return uri.strip()


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
        raise HTTPException(
            status_code=503, detail="Failed to connect to MongoDB"
        )
