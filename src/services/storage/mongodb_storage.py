"""
MongoDB storage for conversations (Rust: mongodb_storage.rs).
Env: MONGODB_DATABASE_NAME, MONGODB_COLLECTION_NAME
"""

import os
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from src.services.streaming.stream_variants import Conversation


async def get_database(vault_url: str) -> AsyncIOMotorDatabase:
    """
    Rust: pub async fn get_database(vault_url) -> Result<Database, HttpResponse>.
    For now we ignore vault URL parsing complexity, just connect via motor.
    """
    db_name = os.getenv("MONGODB_DATABASE_NAME")
    if not db_name:
        raise RuntimeError("MONGODB_DATABASE_NAME not set")
    uri = vault_url or "mongodb://localhost:27017"
    client = AsyncIOMotorClient(uri)
    return client[db_name]


async def append_thread(thread_id: str, user_id: str, content: Conversation, database: AsyncIOMotorDatabase) -> None:
    """
    Rust: pub async fn append_thread(thread_id, user_id, content, database).
    Upsert thread by id; append content.
    """
    coll_name = os.getenv("MONGODB_COLLECTION_NAME", "threads")
    coll = database[coll_name]
    if not content:
        return
    await coll.update_one(
        {"thread_id": thread_id, "user_id": user_id},
        {"$push": {"content": {"$each": content}}, "$set": {"date": {"$currentDate": {"$type": "date"}}}},
        upsert=True,
    )


async def read_thread(thread_id: str, database: AsyncIOMotorDatabase) -> Optional[dict]:
    """
    Rust: pub async fn read_thread(thread_id, database) -> Option<MongoDBThread>.
    Returns document with .content
    """
    coll_name = os.getenv("MONGODB_COLLECTION_NAME", "threads")
    coll = database[coll_name]
    doc = await coll.find_one({"thread_id": thread_id})
    return doc


async def read_threads(user_id: str, database: AsyncIOMotorDatabase) -> List[dict]:
    """
    Rust: pub async fn read_threads(user_id, database) -> Vec<MongoDBThread>.
    Returns latest 10 threads for a user.
    """
    coll_name = os.getenv("MONGODB_COLLECTION_NAME", "threads")
    coll = database[coll_name]
    cursor = coll.find({"user_id": user_id}).sort("date", -1).limit(10)
    return [doc async for doc in cursor]
