import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from pymongo import AsyncMongoClient

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.settings import get_settings
from freva_gpt.services.streaming.stream_variants import (
    StreamVariant,
    cleanup_conversation,
    from_json_to_sv,
    from_sv_to_json,
)

from .helpers import Thread, Variant, get_database, summarize_topic

logger = configure_logging(__name__)

# ──────────────────── Config from settings.py ────────────────────────────

settings = get_settings()
MONGODB_DATABASE_NAME = settings.MONGODB_DATABASE_NAME
MONGODB_COLLECTION_NAME = settings.MONGODB_COLLECTION_NAME


class ThreadStorage:
    """Store threads in MongoDB."""

    def __init__(self, vault_url: str | None) -> None:
        self.vault_url = vault_url
        self.db = None

    @classmethod
    async def create(cls, vault_url: str | None):
        self = cls(vault_url)
        if settings.DEV:
            self.db = AsyncMongoClient(settings.MONGODB_URI_DEV)[
                MONGODB_DATABASE_NAME
            ]
        else:
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

        all_stream = (
            [from_sv_to_json(v) for v in merged_sv] if merged_sv else []
        )
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
        cursor = (
            coll.find({"user_id": user_id}).sort([("date", -1)]).limit(limit)
        )
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
        except Exception:
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
        except Exception:
            return False

    async def query_by_topic(
        self,
        user_id: str,
        topic: str,
        num_threads: int,
    ) -> tuple[int, List[Thread]]:
        """
        Search in the topic field.
        """
        coll = self.db[MONGODB_COLLECTION_NAME]
        filt = {
            "user_id": user_id,
            "topic": {"$regex": re.escape(topic), "$options": "i"},
        }

        total = await coll.count_documents(filt)
        cursor = coll.find(filt).sort("updated_at", -1).limit(num_threads)
        docs = await cursor.to_list(length=num_threads)
        threads = [
            Thread(
                user_id=d["user_id"],
                thread_id=d["thread_id"],
                date=d["date"],
                topic=d.get("topic", ""),
                content=d.get("content", []),
            )
            for d in docs
        ]
        return total, threads

    async def query_by_variant(
        self,
        user_id: str,
        variant: Variant,
        content: str,
        num_threads: int,
    ) -> tuple[int, List[Thread]]:
        """
        Search in a specific variant field (user/assistant/code/code_output).
        """
        coll = self.db[MONGODB_COLLECTION_NAME]

        filt = {
            "user_id": user_id,
            "content": {
                "$elemMatch": {
                    "variant": variant,
                    "content": {"$regex": re.escape(content), "$options": "i"},
                }
            },
        }

        total = await coll.count_documents(filt)
        cursor = coll.find(filt).sort("updated_at", -1).limit(num_threads)
        docs = await cursor.to_list(length=num_threads)
        threads = [
            Thread(
                user_id=d["user_id"],
                thread_id=d["thread_id"],
                date=d["date"],
                topic=d.get("topic", ""),
                content=d.get("content", []),
            )
            for d in docs
        ]
        return total, threads
