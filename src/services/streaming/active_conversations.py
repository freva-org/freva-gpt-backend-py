
import string
import random
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Iterable, Tuple
from datetime import datetime
import asyncio

from src.services.streaming.stream_variants import StreamVariant
from src.services.mcp.mcp_manager import McpManager


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
    conversation: List[StreamVariant] = field(default_factory=list)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    freva_config_path: Optional[str] = None


Registry = Dict[str, ActiveConversation]
RegistryLock = asyncio.Lock


def new_conversation_id(length: int = 32) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def add_to_conversation(
    registry: Registry,
    thread_id: str,
    variants: Iterable[StreamVariant],
    user_id: str,
    freva_config_path: Optional[str] = None,
) -> ActiveConversation: ...

def get_conversation_state(registry: Registry, thread_id: str) -> Optional[ConversationState]: ...

def set_conversation_state(
    registry: Registry, thread_id: str, state: ConversationState
) -> bool: ...

def end_conversation(
    registry: Registry, thread_id: str
) -> Optional[ActiveConversation]: ...

def compact_variants(
    variants: Iterable[StreamVariant]
) -> list[StreamVariant]: ...

def save_and_remove_conversation(
    registry: Registry, thread_id: str, store: "Storage"
) -> bool: ...

def cleanup_idle(
    registry: Registry, idle_after: float,  # seconds
    now: Optional[datetime] = None,
    store: Optional["Storage"] = None,
) -> list[str]:  # thread_ids evicted
    ...
