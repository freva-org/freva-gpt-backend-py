from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime, timezone
import re

from pymongo import AsyncMongoClient

from .helpers import Thread, get_database, summarize_topic, Variant, VARIANT_FIELD
from src.core.settings import get_settings
from src.services.streaming.stream_variants import StreamVariant, cleanup_conversation, from_sv_to_json, from_json_to_sv
from src.core.logging_setup import configure_logging

DEFAULT_LOGGER = configure_logging(__name__)

# ──────────────────── Config from settings.py ────────────────────────────

settings = get_settings()
MONGODB_DATABASE_NAME = settings.MONGODB_DATABASE_NAME
MONGODB_COLLECTION_NAME = settings.MONGODB_COLLECTION_NAME


class ThreadStorage():
    """PROD / shared implementation: store threads in MongoDB."""
    def __init__(self, vault_url: str) -> None:
        self.vault_url = vault_url
        self.db = None


    @classmethod
    async def create(cls, vault_url: str):
        self = cls(vault_url)
        if settings.DEV:
            self.db = AsyncMongoClient(settings.MONGODB_URI_DEV)[MONGODB_DATABASE_NAME]
        else:
            self.db = await get_database(self.vault_url)
        return self


    async def save_thread(
        self,
        thread_id: str,
        user_id: str,
        content: List[StreamVariant],
        root_thread_id: Optional[str] = None,
        parent_thread_id: Optional[str] = None,
        fork_from_index: Optional[int] = None,
        append_to_existing: Optional[bool] = False,
    ) -> None:
        logger = configure_logging(__name__, thread_id=thread_id, user_id=user_id)
        content = cleanup_conversation(content)
        if not content:
            return

        coll = self.db[MONGODB_COLLECTION_NAME]

        existing = await coll.find_one({"thread_id": thread_id})
        merged_sv: List[StreamVariant] = content
        topic = None
        if existing:
            if append_to_existing:
                existing_stream = existing.get("content", [])
                existing_sv = [from_json_to_sv(v) for v in existing_stream]
                merged_sv: List[StreamVariant] = existing_sv + content
            # topic: keep existing if present
            topic = existing.get("topic", "") or None

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
            "root_thread_id": thread_id or root_thread_id,
            "parent_thread_id": thread_id or parent_thread_id,
            "fork_from_index": 0 or fork_from_index,
        }

        if existing:
            await coll.update_one({"thread_id": thread_id}, {"$set": doc}, upsert=True)
        else:
            await coll.insert_one(doc)
        logger.info("Saved thread to MongoDB", extra={"thread_id": thread_id, "user_id": user_id, "append": append_to_existing})


    async def list_recent_threads(
        self,
        user_id: str,
        limit: int = 20,
    ) -> Tuple[List[Thread], int]:
        logger = configure_logging(__name__, user_id=user_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        n_threads = await coll.count_documents({"user_id": user_id})
        cursor = coll.find({"user_id": user_id}).sort([("date", -1)]).limit(limit)
        docs = await cursor.to_list(length=limit)
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
        logger.info("Listed recent threads from MongoDB", extra={"user_id": user_id, "returned": len(threads), "limit": limit})
        return threads, n_threads


    async def read_thread(
        self,
        thread_id: str,
    ) -> List[Dict]:
        #TODO check the return
        logger = configure_logging(__name__, thread_id=thread_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        doc = await coll.find_one({"thread_id": thread_id})
        if not doc:
            logger.warning("Thread not found in MongoDB", extra={"thread_id": thread_id})
            raise FileNotFoundError("Thread not found")
        return doc.get("content", [])
    

    async def update_thread_topic(
        self,
        thread_id: str,
        topic: str
    ) -> bool:
        try:
            logger = configure_logging(__name__, thread_id=thread_id)
            coll = self.db[MONGODB_COLLECTION_NAME]
            update_op = { '$set' :  { 'topic' : topic } }
            await coll.update_one({"thread_id": thread_id}, update_op)
            logger.info("Updated topic in MongoDB", extra={"thread_id": thread_id})
            return True
        except:
            logger = configure_logging(__name__, thread_id=thread_id)
            logger.exception("Failed to update topic in MongoDB", extra={"thread_id": thread_id})
            return False
        

    async def delete_thread(
        self,
        thread_id: str,
    ) -> bool:
        try:
            logger = configure_logging(__name__, thread_id=thread_id)
            coll = self.db[MONGODB_COLLECTION_NAME]
            await coll.delete_one({"thread_id": thread_id})
            #TODO check the return
            logger.info("Deleted thread in MongoDB", extra={"thread_id": thread_id})
            return True
        except:
            logger = configure_logging(__name__, thread_id=thread_id)
            logger.exception("Failed to delete thread in MongoDB", extra={"thread_id": thread_id})
            return False

    async def query_by_topic(
        self,
        user_id: str,
        topic: str,
        num_threads: int,
    ) -> Dict[str, Any]:
        """
        Search in the topic field.
        """
        coll = self.db[MONGODB_COLLECTION_NAME]
        filt = {
            "user_id": user_id,
            "topic": {"$regex": re.escape(topic), "$options": "i"},
        }

        total = await coll.count_documents(filt)
        cursor = (
            coll.find(filt)
            .sort("updated_at", -1)
            .limit(num_threads)
        )
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
    ) -> Dict[str, Any]:
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
        cursor = (
            coll.find(filt)
            .sort("updated_at", -1)
            .limit(num_threads)
        )
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
