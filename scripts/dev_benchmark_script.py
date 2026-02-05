from __future__ import annotations

"""
Headless dev/benchmark runner mirroring /chatbot/streamresponse behaviour.

- Config below (no argparse).
- Disk-only persistence (never Mongo).
- RUNS/CONCURRENCY for benchmarks; set CONCURRENCY=1 for clean mode.
- Uses ONE global McpManager; orchestrator ties MCP session to thread_id.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.prompting import get_entire_prompt
from freva_gpt.services.service_factory import (
    get_authenticator,
    get_thread_storage,
)
from freva_gpt.services.streaming.active_conversations import (
    new_thread_id,
    end_and_save_conversation,
)
from freva_gpt.services.streaming.stream_orchestrator import (
    prepare_for_stream,
    run_stream,
)
from freva_gpt.services.streaming.stream_variants import from_sv_to_json

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MODEL = "gpt-4o"
USER_ID = "dev_user"
PROMPT = "Make an annual mean temperature global map plot for the year 2023"

RUNS = 1
CONCURRENCY = 1  # ← set to 1 for clean mode
WARMUP_RUNS = 0

NEW_THREAD_PER_RUN = True
THREAD_ID_BASE: Optional[str] = None

PRINT_STREAM = False
PRINT_PER_RUN_SUMMARY = True
PRINT_FINAL_SUMMARY = True

# ──────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("dev_script")
configure_logging()


@dataclass
class RunResult:
    idx: int
    thread_id: str
    duration_s: float
    chunks: int
    chars: int
    status: str


async def _run_once(idx: int, sem: asyncio.Semaphore) -> RunResult:
    async with sem:
        thread_id = new_thread_id()

        Storage = await get_thread_storage(
            user_name=USER_ID, thread_id=thread_id
        )
        Auth = get_authenticator()

        await prepare_for_stream(thread_id, USER_ID, Auth)

        system_prompt = get_entire_prompt(USER_ID, thread_id, MODEL)

        t0 = time.perf_counter()
        chunk_count = 0
        char_count = 0
        status = "Done"

        try:
            async for variant in run_stream(
                model=MODEL,
                thread_id=(None if NEW_THREAD_PER_RUN else thread_id),
                user_input=PROMPT,
                system_prompt=system_prompt,  # ← reuse single McpManager
            ):
                if getattr(variant, "variant", None) == "Assistant":
                    txt = getattr(variant, "text", "") or ""
                    chunk_count += 1
                    char_count += len(txt)

                if (
                    PRINT_STREAM
                    and getattr(variant, "variant", None) != "Assistant"
                ):
                    print(
                        json.dumps(
                            from_sv_to_json(variant), ensure_ascii=False
                        )
                    )

            await end_and_save_conversation(thread_id, Storage)

        except asyncio.CancelledError:
            status = "Cancelled"
        except Exception as e:
            status = f"Error:{e}"

        duration = time.perf_counter() - t0

        if PRINT_PER_RUN_SUMMARY:
            print(
                f"[run {idx:03d}] thread={thread_id} status={status} "
                f"chunks={chunk_count} chars={char_count} time={duration:.3f}s"
            )

        return RunResult(
            idx, thread_id, duration, chunk_count, char_count, status
        )


async def _warmup() -> None:
    if WARMUP_RUNS <= 0:
        return
    sem = asyncio.Semaphore(1)
    tasks = [
        asyncio.create_task(_run_once(-i - 1, sem)) for i in range(WARMUP_RUNS)
    ]
    await asyncio.gather(*tasks)


async def main() -> None:
    await _warmup()

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(_run_once(i, sem)) for i in range(RUNS)]
    results = await asyncio.gather(*tasks)

    if PRINT_FINAL_SUMMARY and results:
        ok = [r for r in results if r.status == "Done"]
        errs = [r for r in results if r.status != "Done"]
        avg = sum(r.duration_s for r in results) / len(results)
        p50 = sorted(r.duration_s for r in results)[len(results) // 2]
        fastest = min(results, key=lambda r: r.duration_s)
        slowest = max(results, key=lambda r: r.duration_s)
        total_chunks = sum(r.chunks for r in results)
        total_chars = sum(r.chars for r in results)

        print("\n=== Summary ===")
        print(
            f"model={MODEL} runs={RUNS} concurrency={CONCURRENCY} warmups={WARMUP_RUNS}"
        )
        print(f"success={len(ok)} errors={len(errs)}")
        print(
            f"avg_time={avg:.3f}s p50_time={p50:.3f}s fastest={fastest.duration_s:.3f}s slowest={slowest.duration_s:.3f}s"
        )
        print(f"total_chunks={total_chunks} total_chars={total_chars}")
        if errs:
            print("errors:")
            for r in errs[:10]:
                print(f"  run={r.idx} thread={r.thread_id} status={r.status}")


if __name__ == "__main__":
    asyncio.run(main())
