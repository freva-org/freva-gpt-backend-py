import re
from datetime import UTC, datetime, timezone
from typing import Dict, List, Optional

import pymongo
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.settings import get_settings
from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVServerHint,
    cleanup_conversation,
    from_json_to_sv,
    from_sv_to_json,
)

from .helpers import Thread, get_mongodb_uri
from .summarize_topic import summarize_topic

DEFAULT_LOGGER = configure_logging(__name__)

# ──────────────────── Config from settings.py ────────────────────────────

settings = get_settings()
MONGODB_DATABASE_NAME = settings.MONGODB_DATABASE_NAME
MONGODB_COLLECTION_NAME = settings.MONGODB_COLLECTION_NAME
MONGODB_COLLECTION_NAME_FEEDBACK = "userfeedback"


class ThreadStorage:
    """Store threads in MongoDB."""

    def __init__(self, client: AsyncMongoClient, db: AsyncDatabase) -> None:
        self.client = client
        self.db = db

    @classmethod
    async def create(cls) -> "ThreadStorage":
        mongo_uri = get_mongodb_uri()
        client: AsyncMongoClient = AsyncMongoClient(mongo_uri, connectTimeoutMS=30000)
        db: AsyncDatabase = client[MONGODB_DATABASE_NAME]

        storage = cls(client=client, db=db)

        coll = db[MONGODB_COLLECTION_NAME]
        await coll.create_index("thread_id", unique=True)
        await coll.create_index(
            [("user_id", pymongo.ASCENDING), ("date", pymongo.DESCENDING)]
        )

        return storage

    async def close(self) -> None:
        await self.client.close()

    async def thread_exists(self, thread_id: str) -> bool:
        coll = self.db[MONGODB_COLLECTION_NAME]
        exists = await coll.find_one({"thread_id": thread_id}, {"_id": 1}) is not None
        return exists

    async def save_thread(
        self,
        thread_id: str,
        user_id: str,
        content: list[StreamVariant],
        root_thread_id: str | None = None,
        parent_thread_id: str | None = None,
        fork_from_index: int | None = None,
        append_to_existing: bool | None = False,
    ) -> None:
        logger = configure_logging(__name__, thread_id=thread_id, user_id=user_id)
        content_cleaned: list[StreamVariant] = cleanup_conversation(content)
        if not content_cleaned:
            return

        coll = self.db[MONGODB_COLLECTION_NAME]

        existing = await coll.find_one({"thread_id": thread_id})
        merged_sv: list[StreamVariant] = content_cleaned
        topic = None
        if existing:
            if append_to_existing:
                existing_stream = existing.get("content", [])
                existing_sv: list[StreamVariant] = [
                    from_json_to_sv(v) for v in existing_stream
                ]
                merged_sv: list[StreamVariant] = existing_sv + content_cleaned  # type: ignore[no-redef]
            # topic: keep existing if present
            topic = existing.get("topic", "") or None
            if not root_thread_id:
                root_thread_id = existing.get("root_thread_id")
                parent_thread_id = existing.get("parent_thread_id")
                fork_from_index = existing.get("fork_from_index")
        else:
            if not root_thread_id:
                root_thread_id = thread_id
                parent_thread_id = thread_id
                fork_from_index = 0

        # compute topic if missing
        if not topic or topic == "No topic yet":
            topic = await summarize_topic(content)

        all_stream = [from_sv_to_json(v) for v in merged_sv] if merged_sv else []
        doc = {
            "user_id": user_id,
            "thread_id": thread_id,
            "date": datetime.now(UTC),
            "topic": topic,
            "content": all_stream,
            "root_thread_id": root_thread_id,
            "parent_thread_id": parent_thread_id,
            "fork_from_index": fork_from_index,
        }

        if existing:
            await coll.update_one({"thread_id": thread_id}, {"$set": doc}, upsert=True)
        else:
            await coll.insert_one(doc)
        logger.info(
            "Saved thread to MongoDB",
            extra={
                "thread_id": thread_id,
                "user_id": user_id,
                "append": append_to_existing,
            },
        )

    async def list_recent_threads(
        self,
        user_id: str,
        limit: int = 20,
        page: int = 0,
    ) -> tuple[list[Thread], int]:
        logger = configure_logging(__name__, user_id=user_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        n_threads = await coll.count_documents({"user_id": user_id})
        cursor = (
            coll.find({"user_id": user_id})
            .sort([("date", -1)])
            .skip(page * limit)
            .limit(limit)
        )
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
        logger.info(
            "Listed recent threads from MongoDB",
            extra={"user_id": user_id, "returned": len(threads), "limit": limit},
        )
        return threads, n_threads

    async def read_thread(
        self,
        thread_id: str,
    ) -> list[dict]:
        # TODO check the return
        logger = configure_logging(__name__, thread_id=thread_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        doc = await coll.find_one({"thread_id": thread_id})
        if not doc:
            logger.warning(
                "Thread not found in MongoDB", extra={"thread_id": thread_id}
            )
            raise FileNotFoundError("Thread not found")
        return doc.get("content", [])

    async def get_user_id_for_thread(self, thread_id: str) -> Optional[str]:
        logger = configure_logging(__name__, thread_id=thread_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        doc = await coll.find_one({"thread_id": thread_id})
        if not doc:
            logger.warning(
                "Thread not found in MongoDB when fetching user_id",
                extra={"thread_id": thread_id},
            )
            return None
        return doc.get("user_id")

    async def fork_thread(self, old_thread_id: str, new_thread_id: str, user_id: str):
        logger = configure_logging(__name__, thread_id=old_thread_id, user_id=user_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        doc = await coll.find_one({"thread_id": old_thread_id})
        if not doc:
            logger.warning(
                "Thread not found in MongoDB when forking",
                extra={"thread_id": old_thread_id},
            )
            raise FileNotFoundError("Thread not found")

        content = doc.get("content", [])
        content = [from_json_to_sv(v) for v in content]
        content = update_threadid_in_content(new_thread_id, content, logger=logger)
        content = [from_sv_to_json(v) for v in content]

        new_doc = {
            "user_id": user_id,
            "thread_id": new_thread_id,
            "date": datetime.now(timezone.utc),
            "topic": doc.get("topic", ""),
            "content": content,
        }
        await coll.insert_one(new_doc)
        logger.info(
            "Forked thread in MongoDB",
            extra={
                "old_thread_id": old_thread_id,
                "new_thread_id": new_thread_id,
                "user_id": user_id,
            },
        )

    async def get_root_id_for_thread(self, thread_id: str) -> Optional[str]:
        logger = configure_logging(__name__, thread_id=thread_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        doc = await coll.find_one({"thread_id": thread_id})
        if not doc:
            logger.warning(
                "Thread not found in MongoDB when fetching root_thread_id",
                extra={"thread_id": thread_id},
            )
            return None
        return doc.get("root_thread_id")

    async def update_thread_topic(self, thread_id: str, topic: str):
        logger = configure_logging(__name__, thread_id=thread_id)
        coll = self.db[MONGODB_COLLECTION_NAME]
        update_op = {"$set": {"topic": topic}}
        await coll.update_one({"thread_id": thread_id}, update_op)
        logger.info("Updated topic in MongoDB", extra={"thread_id": thread_id})

    async def delete_thread(
        self,
        thread_id: str,
    ):
        coll = self.db[MONGODB_COLLECTION_NAME]
        await coll.delete_one({"thread_id": thread_id})

    async def save_feedback(
        self,
        thread_id: str,
        user_id: str,
        content_json: List[Dict],
        index: int,
        feedback: str,
    ):
        coll_feedback = self.db[MONGODB_COLLECTION_NAME_FEEDBACK]
        feedback_filter = {"thread_id": thread_id, "entry_index": index}
        existing = await coll_feedback.find_one(feedback_filter)
        new_feedback: Dict = {
            "thread_id": thread_id,
            "user_id": user_id,
            "entry_index": index,
            "entry": content_json[index],
            "feedback": feedback,
        }
        if existing:
            # Check if there was already feedback on this entry, if so update the existing one
            await coll_feedback.update_one(
                feedback_filter, {"$set": new_feedback}, upsert=True
            )
        else:
            await coll_feedback.insert_one(new_feedback)

        # Save feedback in the thread history
        await self._save_feedback_to_thread(
            thread_id, user_id, content_json, index, feedback
        )

    async def delete_feedback(
        self,
        thread_id: str,
        user_id: str,
        content_json: List[Dict],
        index: int,
    ):
        coll = self.db[MONGODB_COLLECTION_NAME_FEEDBACK]
        feedback_filter = {
            "thread_id": thread_id,
            "user_id": user_id,
            "entry_index": index,
        }
        await coll.delete_one(feedback_filter)

        # Save feedback in the thread history
        await self._save_feedback_to_thread(
            thread_id, user_id, content_json, index, feedback="remove"
        )

    async def _save_feedback_to_thread(
        self,
        thread_id: str,
        user_id: str,
        content_json: List[Dict],
        index: int,
        feedback: str,
    ):
        if feedback == "remove":
            content_json[index].pop("feedback")
        else:
            content_json[index].update({"feedback": feedback})

        content_sv = [from_json_to_sv(v) for v in content_json]
        await self.save_thread(thread_id, user_id, content_sv)

    async def query_by_topic(
        self,
        user_id: str,
        topic: str,
        num_threads: int,
        page: int,
    ) -> tuple[int, list[Thread]]:
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
            .skip(page * num_threads)
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

    async def count_users(self) -> int:
        coll = self.db[MONGODB_COLLECTION_NAME]
        users = await coll.distinct("user_id")
        return len(users)

    async def count_conversations(self) -> int:
        coll = self.db[MONGODB_COLLECTION_NAME]
        return await coll.count_documents({})


def update_threadid_in_content(
    new_id: str, content: list[StreamVariant], logger
) -> list[StreamVariant]:
    if isinstance(content[0], SVServerHint):
        content[0] = SVServerHint(content={"thread_id": new_id})
        logger.info("Updated ServerHint with new thread-id.")
    else:
        if any(isinstance(c, SVServerHint) for c in content):
            logger.exception("ServerHint is in unexpected position in thread content!")
            raise ValueError("ServerHint is in unexpected position in thread content!")
        else:
            logger.info(
                "ServerHint is missing in the thread content. It is inserted with the new thread-id."
            )
            content = [SVServerHint(content={"thread_id": new_id})] + content
    return content
