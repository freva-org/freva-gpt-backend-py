import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple

from .thread_storage import ThreadStorage, Thread, summarize_topic
from src.services.streaming.stream_variants import Conversation, SVUser, from_sv_to_json, cleanup_conversation

log = logging.getLogger(__name__)

THREADS_DIR = Path("./threads")


class DiskThreadStorage(ThreadStorage):
    """DEV / local implementation: store threads on disk."""    

    async def append_thread(
        self,
        thread_id: str,
        user_id: str,
        content: Conversation,
    ) -> None:
        THREADS_DIR.mkdir(parents=True, exist_ok=True)

        content = cleanup_conversation(content)
        if not content:
            return
        
        # convert to dicts
        to_write = []
        for v in content:
            try:
                v_dict = from_sv_to_json(v)
                to_write.append(json.dumps(v_dict, ensure_ascii=False))
            except Exception:
                # last-ditch legacy colon encoding (rare)
                v_dict = from_sv_to_json(v)
                var = v_dict.get("variant")
                c = v_dict.get("content")
                if isinstance(c, list):
                    line = f"{var}:{':'.join(map(str, c))}"
                else:
                    line = f"{var}:{c}"
                to_write.append(line)

        path = THREADS_DIR / f"{thread_id}.txt"
        with open(path, "a", encoding="utf-8") as f:
            for line in to_write:
                f.write(line + "\n")


    async def list_recent_threads(
        self,
        user_id: str,
        limit: int = 20,
    ) -> Tuple[List[Thread], int]:
        docs, n_threads = get_latest_files(THREADS_DIR, n=limit)
        threads: List[Thread] = []
        for d in docs:
            thread_id = d.stem
            content = await self.read_thread(thread_id)
            first_user_text = next((sv.text if isinstance(sv, SVUser) else getattr(sv, "content", None)
                                for sv in content
                                if isinstance(sv, SVUser)), None)
            topic = await summarize_topic(first_user_text or "Untitled")

            threads.append(
                Thread(
                    user_id=user_id,
                    thread_id=thread_id,
                    date=d.stat().st_ctime,
                    topic=topic,
                    content=content,
                    )
            )
        return threads, n_threads

    async def read_thread(
        self,
        thread_id: str,
    ) -> List[Dict]:
        # TODO check return
        path = THREADS_DIR / f"{thread_id}.txt"
        if not path.exists():
            raise FileNotFoundError("Thread not found")

        conv: List = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                obj = json.loads(line)
                conv.append(obj)
            except Exception:
                pass
        return conv


# ──────────────────── Helper functions ──────────────────────────────

def get_latest_files(directory: str, n: int):
    p = Path(directory)

    # Get only files, not directories
    try:
        files = [f for f in p.iterdir() if f.exists() and f.is_file()]
    except:
        files = []

    # Sort by creation time (ctime)
    files_sorted = sorted(files, key=lambda x: x.stat().st_ctime, reverse=True)

    # Return the n latest files
    return files_sorted[:n], len(files)