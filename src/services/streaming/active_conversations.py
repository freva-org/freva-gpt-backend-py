
import string
import random
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Iterable, Any
from datetime import datetime, timezone, timedelta
import logging

import asyncio
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import Request

from src.core.logging_setup import configure_logging
from src.services.streaming.stream_variants import StreamVariant, from_sv_to_json
from src.services.mcp.mcp_manager import McpManager, build_mcp_manager
from src.services.storage.router import append_thread
from src.services.streaming.helpers import get_mcp_headers_from_req

log = logging.getLogger(__name__)
configure_logging()

# Idle timeout for cleanup
MAX_IDLE = timedelta(minutes=3)


class ConversationState(str, Enum):
    STREAMING = "streaming"
    STOPPING  = "stopping"
    ENDED     = "ended"


@dataclass
class ActiveConversation:
    thread_id: str
    user_id: str
    state: ConversationState
    mcp_manager: McpManager
    messages: List[StreamVariant] = field(default_factory=list)
    last_activity: datetime = field(default_factory=datetime.utcnow)


Registry: Dict[str, ActiveConversation] = {}
RegistryLock = asyncio.Lock()


def _generate_id(length: int = 32) -> str:
    """Generate a random thread id candidate."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


async def new_thread_id() -> str:
    """
    Create a new unique thread_id that does not collide with existing entries
    in the in-memory registry.
    """
    async with RegistryLock:
        while True:
            candidate = _generate_id()
            if candidate not in Registry:
                return candidate

async def check_thread_exists(thread_id: str) -> bool:
    """
    Check if a thread_id exists in the registry.
    """
    async with RegistryLock:
        return thread_id in Registry.keys()
    

async def initialize_conversation(
    thread_id: str, 
    user_id: str,
    messages: Optional[List[Dict[str, Any]]] = [],
    request: Optional[Request] = None,
    mcp_headers: Optional[Dict[str, Any]] = None,
) -> ActiveConversation:
    now = datetime.now(timezone.utc)      
    if not await check_thread_exists(thread_id):
        log.debug("Initializing the conversation and saving it to Registry...")

        if request:
            mcp_headers = await get_mcp_headers_from_req(request, thread_id)
            mcp_mgr = build_mcp_manager(headers=mcp_headers)
        elif mcp_headers:
            mcp_mgr = build_mcp_manager(headers=mcp_headers)
        else:
            log.warning("The conversation is initialized without MCPManager! Please note that the MCP servers cannot be connected!")

        conv = ActiveConversation(
            thread_id=thread_id,
            user_id=user_id,
            state=ConversationState.STREAMING,
            mcp_manager= mcp_mgr,
            messages=messages,
            last_activity=now,
        )
        # TODO: send tool calls to MCP server if there are variants present in messages, i.e. Code
        Registry[thread_id] = conv
        


async def add_to_conversation(
    thread_id: str,
    messages: List[StreamVariant],
) -> ActiveConversation: 
    """
    Check if an ActiveConversation exists for thread_id and append new variants.
    Updates last_activity and returns the updated conversation object.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        if conv is None: 
            raise ValueError("Conversation does not exist. Please initialize first!")
        conv.messages.extend(messages)
        conv.last_activity = datetime.now(timezone.utc)
        return conv


async def get_conversation_state(thread_id: str) -> Optional[ConversationState]: 
    """
    Return the state of the conversation, or None if it is unknown.
    Does NOT create a conversation if missing.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        return conv.state if conv is not None else None
    
    
async def get_conv_mcpmanager(thread_id: str) -> Optional[McpManager]: 
    """
    Return the MCPManager of the conversation, or None if it does not exist
    Does NOT create a conversation if missing.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        return conv.mcp_manager if conv is not None else None
    
async def get_conv_messages(thread_id: str) -> Optional[List[StreamVariant]]: 
    """
    Return the messages of the conversation, or None if it does not exist
    Does NOT create a conversation if missing.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        return conv.messages if conv is not None else None
    

async def request_stop(thread_id: str) -> bool:
    """
    Signal that a conversation should stop streaming.
    Returns True if the conversation was found and updated.
    (The streaming loop should periodically check the state and exit when STOPPING.)
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        if conv is None:
            return False
        conv.state = ConversationState.STOPPING
        conv.last_activity = datetime.now(timezone.utc)
        return True
    
async def end_conversation(
    thread_id: str
) -> Optional[ActiveConversation]: 
    """
    Mark a conversation as ENDED but keep it in the registry.
    Usually followed by save_conversation and remove_conversation.
    Returns the conversation if it existed.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        if conv is None:
            return None
        conv.state = ConversationState.ENDED
        # TODO interrupt MCP tool call
        # TODO interrupt LiteLLM call
        conv.last_activity = datetime.now(timezone.utc)
        return conv

async def save_conversation(
    thread_id: str, 
    database:  Optional[AsyncIOMotorDatabase] = None,
) -> bool: 
    """
    Save a conversation to available storage through storage.router.
    Returns True if a conversation was found and saved, False if it didn't exist.
    """
    conv: Optional[ActiveConversation]

    async with RegistryLock:
        conv = Registry.get(thread_id)
        if not conv:
            return False
        else:
            await append_thread(conv.thread_id, conv.user_id, conv.messages, database)
            return True

async def remove_conversation(
    thread_id: str
) -> bool: 
    """
    Remove a conversation from the registry.
    Returns True if a conversation was removed, False if it didn't exist.
    NOTE: we do NOT hold the registry_lock while awaiting I/O.
    """
    # Remove under lock
    async with RegistryLock:
        conv = Registry.pop(thread_id, None)

    if conv is None:
        return False
    return True

async def cleanup_idle(
    database:  Optional[AsyncIOMotorDatabase] = None,
) -> list[str]:  # thread_ids evicted
    """
    Remove conversations that have been idle longer than MAX_IDLE.
    Each removed conversation is persisted via `save_conversation`.
    Returns a list of evicted thread_ids.
    """
    now = datetime.now(timezone.utc)
    to_evict: List[ActiveConversation] = []
    evicted_ids: List[str] = []

    # Decide which ones to evict under lock and remove them.
    async with RegistryLock:
        for thread_id, conv in list(Registry.items()):
            if now - conv.last_activity > MAX_IDLE:
                evicted_ids.append(thread_id)
                conv.mcp_manager.close()
                to_evict.append(Registry.pop(thread_id))

    # Persist outside the lock to avoid blocking other requests.
    if database:
        for conv in to_evict:
            await save_conversation(conv, database)

    return evicted_ids
