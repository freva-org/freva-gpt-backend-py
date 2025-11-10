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
import json
import logging
import random
import string
from typing import Optional

from src.api.chatbot.streamresponse import _sse_data
from src.core.logging_setup import configure_logging
from src.services.streaming.stream_orchestrator import run_stream, new_conversation_id
from src.services.storage.thread_storage import recursively_create_dir_at_rw_dir, append_thread
from src.services.mcp.mcp_manager import build_mcp_manager
from src.core.prompting import get_entire_prompt, get_entire_prompt_json
from src.services.streaming.stream_variants import (
    from_sv_to_json,
    SVAssistant,
    SVPrompt,
    SVCode,
    SVCodeOutput,
    SVCodeError,
    SVImage,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVUser,
    StreamVariant,
)


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MODEL = "gpt-4o"
USER_ID = "dev_user"

PRINT_DEBUG = True   # Print non-Assistant stream variants (ServerHint, etc.)
SHOW_STATS  = True    # Show per-turn simple stats

# ──────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("dev_chat")
configure_logging()

# One global MCP manager reused for the whole session
mongodb_uri = os.getenv("MONGODB_URI_LOCAL")
freva_cfg_path = "/work/ch1187/clint/nextgems/freva/evaluation_system.conf"
MCP = build_mcp_manager()
headers = {
    "rag": {
        "mongodb-uri": mongodb_uri,
    },
    "code": {
        "freva-config-path": freva_cfg_path,
    },
}
MCP.initialize(headers)


def _new_thread_id(prefix: Optional[str] = None) -> str:
    if prefix:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"{prefix}-{suffix}"
    return new_conversation_id()

def _start_new_thread(thread_id):
    hint = SVServerHint(data={"thread_id": thread_id})
    append_thread(thread_id, [hint])

async def _run_turn(
    *,
    model: str,
    thread_id: str,
    user_id: str,
    user_input: str,
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
            mcp=MCP,
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
    thread_id = _new_thread_id()
    _start_new_thread(thread_id)

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
            break
        if user_input.lower() == "/id":
            print(f"Current thread_id: {thread_id}")
            continue
        if user_input.lower().startswith("/new"):
            # Optional prefix: "/new mytopic"
            parts = user_input.split(maxsplit=1)
            prefix = parts[1] if len(parts) > 1 else None
            thread_id = _new_thread_id(prefix)
            _start_new_thread(thread_id)
            print(f"Started new conversation. Thread: {thread_id}")
            continue

        # Normal turn
        t_chunks, t_chars = await _run_turn(
            model=MODEL,
            thread_id=thread_id,
            user_id=USER_ID,
            user_input=user_input,
        )
        if SHOW_STATS:
            print(f"[turn stats] chunks={t_chunks} chars={t_chars}")

    # At this point the thread file has been incrementally written by the orchestrator.
    # We just print where it lives. (Same path used by recursively_create_dir_at_rw_dir)
    print("\nConversation ended.")
    print(f"Thread saved under the user/thread directory created for: user={USER_ID}, thread_id={thread_id}")


if __name__ == "__main__":
    asyncio.run(main())
