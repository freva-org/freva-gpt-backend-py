from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

"""
Interactive multi-turn dev runner mirroring /chatbot/streamresponse behaviour.

- Reuses the same thread_id across turns (like a real conversation).
- Uses ONE global McpManager, initialized once.
- Persists to the same on-disk thread file that the orchestrator uses.
- Type '/new' to start a fresh conversation (new thread_id).
- Type '/exit' (or press Ctrl-D) to quit; the thread is already saved incrementally.

Notes:
- We rely on the stream orchestrator's existing persistence to append variants
  to the thread on disk; we create the user/thread directory before first turn.
- Printing: Assistant chunks are streamed as they arrive; non-Assistant variants
  are printed compactly when PRINT_DEBUG=True.
"""

import os
import asyncio
import logging
from typing import List, Dict, Any

from src.api.chatbot.streamresponse import _sse_data
from src.core.logging_setup import configure_logging
from motor.motor_asyncio import AsyncIOMotorClient
from src.services.streaming.stream_orchestrator import run_stream, get_conversation_history
from src.core.prompting import get_entire_prompt
from src.services.streaming.stream_variants import (
    from_sv_to_json,
    SVAssistant,
    SVCode,
    SVServerError,
    SVStreamEnd,
)

from src.services.streaming.active_conversations import (
    end_conversation, add_to_conversation,
    new_thread_id, initialize_conversation,
    save_conversation, remove_conversation
)


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MODEL = "gpt-4o"
USER_ID = "dev_user"

PRINT_DEBUG = True   # Print non-Assistant stream variants (ServerHint, etc.)
SHOW_STATS  = True    # Show per-turn simple stats

THREAD_ID = None  # It can be set to a prev thread_id to continue the conversation

# ──────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("dev_chat")
configure_logging()

# One global MCP manager reused for the whole session
mongodb_uri = os.getenv("MONGODB_URI_LOCAL", "")
freva_cfg_path = "/work/ch1187/clint/nextgems/freva/evaluation_system.conf"
mcp_headers = {
    "rag": {
        "mongodb-uri": mongodb_uri,
    },
    "code": {
        "freva-config-path": freva_cfg_path,
    },
}
database = AsyncIOMotorClient(mongodb_uri) if mongodb_uri else None
database = None


async def _start_thread(thread_id, read_history:bool):
    messages: List[Dict[str, Any]] = []
    if read_history:
        try:
            messages = await get_conversation_history(thread_id, database)
        except Exception as e:
            msg = f"Prompt/history assembly failed: {e}"
            log.exception(msg)
            err = SVServerError(message=msg)
            end = SVStreamEnd(message="Stream ended with an error.")
            print(err)
            print(end)
            await add_to_conversation(thread_id, [err, end])
            await end_conversation(thread_id)
    await initialize_conversation(thread_id, USER_ID, messages=messages, mcp_headers=mcp_headers)

async def _run_turn(
    *,
    model: str,
    thread_id: str,
    user_id: str,
    user_input: str,
    system_prompt: List[Dict[str, Any]],
) -> tuple[int, int]:
    """
    Runs a single turn through run_stream and prints Assistant output as it streams.

    Returns:
        (chunk_count, char_count) for Assistant text chunks.
    """
    chunk_count = 0
    char_count = 0

    # Stream Assistant output
    first_chunk = True
    try:
        async for variant in run_stream(
            model=model,
            thread_id=thread_id,     # ← fixed per conversation
            user_id=user_id,
            user_input=user_input,
            system_prompt=system_prompt,
        ):
            if isinstance(variant, SVAssistant):
                txt = getattr(variant, "text", "") or ""
                if first_chunk:
                    # Print a header once per assistant message
                    print("\nAssistant:", end=" ", flush=True)
                    first_chunk = False
                print(txt, end="", flush=True)
                chunk_count += 1
                char_count += len(txt)
            elif isinstance(variant, SVCode):
                txt = getattr(variant, "code", "") or ""
                if first_chunk:
                    # Print a header once per code variant
                    print("\nCode:", end=" ", flush=True)
                    first_chunk = False
                print(txt, end="", flush=True)
                chunk_count += 1
                char_count += len(txt)
            else:
                if PRINT_DEBUG:
                    print("\n[debug]", _sse_data(from_sv_to_json(variant)))

    except asyncio.CancelledError:
        print("\n[Cancelled]")
    except Exception as e:
        print(f"\n[Error] {e}")

    if not first_chunk:
        print()  # newline after assistant completes this turn
    return chunk_count, char_count


async def main() -> None:
    # Start with a fresh conversation
    if not THREAD_ID:
        thread_id = await new_thread_id()
        read_history = False
    else:
        thread_id = THREAD_ID
        read_history = True
    
    await _start_thread(thread_id, read_history)

    system_prompt = get_entire_prompt(USER_ID, thread_id, MODEL)

    print("Interactive dev chat")
    print("────────────────────")
    print("Commands: /new → new thread, /id → show thread id, /exit → quit")
    print(f"Model: {MODEL}")
    print(f"Thread: {thread_id}")
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting…")
            break

        if not user_input:
            # empty line → reprompt
            continue

        # Commands
        if user_input.lower() in ("/exit", "/quit"):
            await end_conversation(thread_id)
            await save_conversation(thread_id, database)
            break
        if user_input.lower() == "/id":
            print(f"Current thread_id: {thread_id}")
            continue
        if user_input.lower().startswith("/new"):
            # Optional prefix: "/new"
            thread_id = new_thread_id()
            _start_thread(thread_id, read_history=False)
            print(f"Started new conversation. Thread: {thread_id}")
            continue

        # Normal turn
        t_chunks, t_chars = await _run_turn(
            model=MODEL,
            thread_id=thread_id,
            user_id=USER_ID,
            user_input=user_input,
            system_prompt=system_prompt,
        )
        await save_conversation(thread_id, database)
        if SHOW_STATS:
            print(f"[turn stats] chunks={t_chunks} chars={t_chars}")

    # At this point the thread file has been incrementally written by the orchestrator.
    # We just print where it lives. (Same path used by recursively_create_dir_at_rw_dir)
    print("\nConversation ended.")
    print(f"Thread saved under the user/thread directory created for: user={USER_ID}, thread_id={thread_id}")


if __name__ == "__main__":
    asyncio.run(main())
