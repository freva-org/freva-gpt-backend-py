import string
import random
import json
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timezone, timedelta
import asyncio

from src.core.logging_setup import configure_logging
from src.services.streaming.stream_variants import StreamVariant, SVCode
from src.services.service_factory import (
    Authenticator,
    ThreadStorage,
    McpManager,
    get_mcp_manager,
)
from src.services.streaming.tool_calls import run_tool_via_mcp

DEFAULT_LOGGER = configure_logging(__name__)


class ConversationState(str, Enum):
    STREAMING = "streaming"
    STOPPING = "stopping"
    ENDED = "ended"


@dataclass
class ActiveConversation:
    thread_id: str
    user_id: str
    state: ConversationState
    mcp_manager: Optional[McpManager]
    tool_tasks: set[asyncio.Task] = field(default_factory=set)
    messages: List[StreamVariant] = field(default_factory=list)
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
    messages: List[StreamVariant],
    auth: Authenticator,
    logger=None,
):
    """
    Initialize and register a new conversation in the registry with the given thread_id and user_id.
    If a conversation with the same thread_id already exists, it will be updated to STREAMING state
    and the last_activity timestamp will be refreshed, but the existing conversation will stay unchanged.
    """
    log = logger or configure_logging(__name__, thread_id=thread_id, user_id=user_id)
    now = datetime.now(timezone.utc)
    # if auth:
    mcp_mgr = await get_mcp_manager(authenticator=auth, thread_id=thread_id)
    # else:
    #     log.warning(f"The conversation {thread_id} initialized without MCPManager! "
    #                 "Please note that the MCP servers cannot be connected!")

    # Precreate the conversation object to reduce time spent under lock
    maybe_new_conv = ActiveConversation(
        thread_id=thread_id,
        user_id=user_id,
        state=ConversationState.STREAMING,
        mcp_manager=mcp_mgr,
        messages=messages,
        last_activity=now,
    )

    async with RegistryLock:
        conv = Registry.get(thread_id)
        if conv:
            # We posess the lock and know the conversation exists.
            # However, if at this point, it is already streaming, we hit a race condition where
            # between the check at the start of the streamresponse endpoint and now, another request has initialized the same conversation and started streaming.
            # To avoid conflicts, we will abort here immediately without updating the conversation, and the streamresponse endpoint will raise a 409.
            if conv.state == ConversationState.STREAMING:
                raise ValueError(
                    f"Conversation with thread_id: {thread_id} already exists. This should not happen due to the check at the start of the streaming endpoint, so it indicates"
                    "a race condition. Aborting to avoid conflicts; the streaming endpoint should raise a 409 Conflict response to the client."
                )

            log.debug("Conversation was found in the Registry. Starting streaming...")

            conv.state = ConversationState.STREAMING
            conv.last_activity = datetime.now(timezone.utc)
            return  # Don't continue with initialization if conversation already exists; we just update the state and timestamp.

        # In order to not have any race conditions, we keep the lock until we've written to the registry

        # register conversation
        Registry[thread_id] = maybe_new_conv

    log.debug("Initialized the conversation and saved to Registry. ")

    # send tool calls to MCP server if there are Code variants present in messages
    if mcp_mgr is not None and any(isinstance(v, SVCode) for v in messages):
        loop = asyncio.get_running_loop()
        task = loop.create_task(_replay_code_history(thread_id))
        await register_tool_task(thread_id, task)
        task.add_done_callback(
            # to be unregistered when done
            lambda t: asyncio.create_task(unregister_tool_task(thread_id, t))
        )


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


async def end_and_save_conversation(
    thread_id: str,
    Storage: ThreadStorage,
) -> bool:
    """
    Mark a conversation as ENDED but keep it in the registry and save to available
    storage through storage.router. Usually followed by remove_conversation.
    Returns True if a conversation was found and saved, False if it didn't exist.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        if conv is None:
            return False
        # End conversation
        conv.state = ConversationState.ENDED
        conv.last_activity = datetime.now(timezone.utc)
    # Save conversation
    await Storage.save_thread(
            conv.thread_id, conv.user_id, conv.messages, append_to_existing=False
        )
    return True


async def remove_conversation(thread_id: str) -> bool:
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


async def _replay_code_history(thread_id: str) -> None:
    """
    Replays all SVCode blocks for a conversation into the code-interpreter MCP server,
    to reconstruct the kernel state when a conversation has been reloaded from storage.

    This is best-effort: failures are logged and we continue or stop depending on the error.
    """
    async with RegistryLock:
        conv = Registry.get(thread_id)
        if conv is None:
            return
        mcp = conv.mcp_manager
        messages = list(conv.messages)

    # Extract all code blocks in chronological order
    code_blocks: list[str] = [
        v.code
        for v in messages
        if isinstance(v, SVCode) and isinstance(v.code, str) and v.code.strip()
    ]

    log = configure_logging(__name__, thread_id=thread_id)

    if not code_blocks:
        log = DEFAULT_LOGGER
        log.debug(
            f"No code blocks found in history for thread {thread_id}; nothing to replay."
        )
        return

    log.info(
        f"Replaying {len(code_blocks)} code blocks to code_interpreter for thread {thread_id}"
    )

    for code in code_blocks:
        try:
            # Run the MCP call asynchronously, reusing helper from stream_orchestrator
            await run_tool_via_mcp(
                mcp=mcp,
                tool_name="code_interpreter",
                arguments_json=json.dumps({"code": code}),
                logger=log,
            )

        except Exception as e:
            log.exception(
                "Failed while replaying code block for thread %s: %s",
                thread_id,
                e,
            )
            # break on first failure; might replace with `continue`
            break


async def register_tool_task(thread_id: str, task: asyncio.Task) -> None:
    """
    Register a long-running tool task with a conversation so it can be cancelled
    via /stop.
    """
    async with RegistryLock:
        Registry.get(thread_id).tool_tasks.add(task)


async def unregister_tool_task(thread_id: str, task: asyncio.Task) -> None:
    """
    Remove a task from the registry once it finishes.
    """
    async with RegistryLock:
        tasks = Registry.get(thread_id).tool_tasks or ()
        if not tasks:
            return
        tasks.discard(task)


async def cancel_tool_tasks(thread_id: str) -> None:
    """
    Cancel all known tool tasks for this conversation.
    """
    async with RegistryLock:
        tasks = Registry.get(thread_id).tool_tasks or ()
    for t in tasks:
        t.cancel()


async def cleanup_idle(
    max_idle: timedelta,
) -> list[str]:  # thread_ids evicted
    """
    Remove conversations that have been idle longer than MAX_IDLE.
    Returns a list of evicted thread_ids.
    """
    now = datetime.now(timezone.utc)
    evicted_ids: List[str] = []
    managers_to_close = []

    # Decide which ones to evict under lock and remove them.
    async with RegistryLock:
        for thread_id, conv in list(Registry.items()):
            if now - conv.last_activity > max_idle:
                evicted_ids.append(thread_id)
                if conv.mcp_manager is not None:
                    managers_to_close.append(conv.mcp_manager)
                Registry.pop(thread_id)

    for mgr in managers_to_close:
        try:
            await mgr.close()
        except Exception:
            DEFAULT_LOGGER.exception("Failed to close MCP manager during idle cleanup.")

    return evicted_ids
