"""
Router that switches between disk and mongo backends (Rust: storage_router.rs).
"""

from enum import Enum
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.services.streaming.stream_variants import Conversation
from . import thread_storage, mongodb_storage


class AvailableStorages(str, Enum):
    DISK = "Disk"
    MONGODB = "MongoDB"


# Default parity with Rust: static STORAGE = MongoDB
STORAGE: AvailableStorages = AvailableStorages.MONGODB


async def append_thread(thread_id: str, user_id: str, content: Conversation, database: Optional[AsyncIOMotorDatabase] = None) -> None:
    """
    Dispatch append_thread to the correct storage backend.
    - Disk ignores user_id
    - Mongo requires user_id
    """
    if STORAGE == AvailableStorages.DISK or database is None:
        thread_storage.append_thread(thread_id, content)
    else:
        await mongodb_storage.append_thread(thread_id, user_id, content, database)


async def read_thread(thread_id: str, database: Optional[AsyncIOMotorDatabase] = None) -> Conversation:
    """
    Dispatch read_thread to the correct storage backend.
    - Disk: raises FileNotFoundError if missing
    - Mongo: returns .content field or raises FileNotFoundError
    """
    if STORAGE == AvailableStorages.DISK or database is None:
        return thread_storage.read_thread(thread_id)

    doc = await mongodb_storage.read_thread(thread_id, database)
    if not doc:
        raise FileNotFoundError("Thread not found")
    return doc