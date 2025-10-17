from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

"""
Dev runner without a frontend.

Edit the CONFIG section and run:
  python -m src.tools.dev_script

What it does:
- Builds full prompt (system + examples + system)
- Calls LiteLLM (non-streaming) with a non-streaming client
- Persists to ./threads/{thread_id}.txt like the server
- Prints latency + a short summary
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.core.available_chatbots import default_chatbot, model_supports_images
from src.core.prompting import get_entire_prompt, get_entire_prompt_json
from src.services.streaming.stream_variants import (
    SVAssistant, SVPrompt, SVServerError, SVServerHint, SVStreamEnd, SVUser,
    help_convert_sv_ccrm,
)
from src.services.models.litellm_client import acomplete, first_text
from src.services.storage.thread_storage import (
    append_thread, read_thread, recursively_create_dir_at_rw_dir,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
USER_ID = "dev_user"
MODEL = "qwen2.5:3b"   # <- use the YAML model_name
INPUT_TEXT = "hello there 👋. What can you do?"
RUNS = 1
CONCURRENCY = 1
THREAD_MODE = "new-each"  # "reuse"
THREAD_ID = ""
SHOW_FIRST_ANSWER = True
PARAMS = {
    "temperature": 0.4,
    # no api_base here — we always route via the proxy set by LITELLM_BASE
}

# ──────────────────────────────────────────────────────────────────────────────


def _new_thread_id(length: int = 32) -> str:
    import random, string
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


@dataclass
class RunResult:
    ok: bool
    latency_ms: float
    assistant_text: Optional[str]
    usage: Dict[str, Any]


async def chat_once(
    *,
    user_id: str,
    input_text: str,
    model: Optional[str] = None,
    thread_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    create_new_if_missing: bool = True,
) -> Tuple[str, RunResult]:
    """
    End-to-end single turn, mirroring /streamresponse (Phase 3 shim) without HTTP.
    Returns (thread_id, RunResult).
    """
    model = model or default_chatbot()
    params = params or {}

    # Decide thread (new vs reuse)
    create_new = not thread_id or not thread_id.strip()
    if create_new and not create_new_if_missing:
        raise ValueError("thread_id is required when create_new_if_missing=False")
    thread_id = thread_id or _new_thread_id()

    # Best-effort RW dir (parity with Rust)
    try:
        recursively_create_dir_at_rw_dir(user_id, thread_id)
    except Exception:
        pass

    # Build messages (prompt or history)
    try:
        if create_new:
            base = get_entire_prompt(user_id, thread_id, model)
            # Persist the prompt context we used (Rust writes Prompt on new threads)
            prompt_json = get_entire_prompt_json(user_id, thread_id, model)
            append_thread(thread_id, [SVPrompt(payload=prompt_json)], ensure_end=False)
            messages = list(base)
        else:
            prior_conv = read_thread(thread_id)
            messages = help_convert_sv_ccrm(
                prior_conv,
                include_images=model_supports_images(model),
                include_meta=False,  # don’t feed meta back into the model
            )
    except Exception as e:
        append_thread(thread_id, [SVServerError(message=str(e)), SVStreamEnd(message="Error")], ensure_end=False)
        return thread_id, RunResult(False, 0.0, None, {})

    # Add user input + persist hint/user
    messages.append({"role": "user", "content": input_text})
    append_thread(thread_id, [SVServerHint(data={"thread_id": thread_id}), SVUser(text=input_text)], ensure_end=False)

    # Call the model
    t0 = time.perf_counter()
    try:
        resp = await acomplete(model=model, messages=messages, **params)
        dt = (time.perf_counter() - t0) * 1000.0
        text = first_text(resp)
        usage = resp.get("usage") or {}
        append_thread(thread_id, [SVAssistant(text=text or ""), SVStreamEnd(message="Done")], ensure_end=False)
        return thread_id, RunResult(True, dt, text, usage)
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000.0
        append_thread(thread_id, [SVServerError(message=str(e)), SVStreamEnd(message="Error")], ensure_end=False)
        return thread_id, RunResult(False, dt, None, {})


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


async def main():
    model = MODEL or default_chatbot()

    # choose thread id
    reuse_tid = THREAD_ID or (_new_thread_id() if THREAD_MODE == "reuse" else "")

    results: List[RunResult] = []
    first_ok_text: Optional[str] = None

    i = 0
    while i < RUNS:
        # make a batch of tasks up to CONCURRENCY
        batch = []
        for _ in range(min(CONCURRENCY, RUNS - i)):
            tid_seed = (reuse_tid if THREAD_MODE == "reuse" else "")
            batch.append(chat_once(
                user_id=USER_ID, input_text=INPUT_TEXT, model=model, thread_id=tid_seed, params=PARAMS
            ))
            i += 1
        done = await asyncio.gather(*batch)
        for tid, res in done:
            results.append(res)
            if SHOW_FIRST_ANSWER and first_ok_text is None and res.ok and res.assistant_text:
                first_ok_text = res.assistant_text

    # summary
    oks = [r for r in results if r.ok]
    fails = [r for r in results if not r.ok]
    lat = sorted([r.latency_ms for r in oks])
    print("\n=== Summary ===")
    print(f"runs: {len(results)}  ok: {len(oks)}  fail: {len(fails)}")
    if lat:
        print(f"latency_ms: avg={sum(lat)/len(lat):.1f}  p50={_pct(lat,50):.1f}  p90={_pct(lat,90):.1f}  p99={_pct(lat,99):.1f}")

    if SHOW_FIRST_ANSWER and first_ok_text:
        print("\n=== First assistant_text ===\n")
        print(first_ok_text)


if __name__ == "__main__":
    asyncio.run(main())
