import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import os

from .thread_storage import ThreadStorage, Thread, summarize_topic
from src.services.streaming.stream_variants import Conversation, SVUser, from_sv_to_json, cleanup_conversation

log = logging.getLogger(__name__)

THREADS_DIR = Path("./threads")


class DiskThreadStorage(ThreadStorage):
    """DEV / local implementation: store threads on disk."""    
    def __init__(self):
        THREADS_DIR.mkdir(parents=True, exist_ok=True)

    async def save_thread(
        self,
        thread_id: str,
        user_id: str,
        content: Conversation,
        root_thread_id: Optional[str] = None,
        parent_thread_id: Optional[str] = None,
        fork_from_index: Optional[int] = None,
        append_to_existing: Optional[bool] = False,
    ) -> None:

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
        if append_to_existing:
            with open(path, "a", encoding="utf-8") as f:
                for line in to_write:
                    f.write(line + "\n")
        else:
            with open(path, "w", encoding="utf-8") as f:
                for line in to_write:
                    f.write(line + "\n")

        topic = await self._get_topic(thread_id, content)
        meta_json = {
            "topic": topic,
            "root_thread_id": thread_id or root_thread_id,
            "parent_thread_id": thread_id or parent_thread_id,
            "fork_from_index": 0 or fork_from_index,
        }
        self._save_meta(thread_id, meta=meta_json)


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
            
            topic = await self._get_topic(thread_id, content)
            
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
    

    async def update_thread_topic(
        self,
        thread_id: str,
        topic: str
    ) -> bool:
        try:
            # Meta already exists. Read meta
            meta_json = self._read_meta(thread_id)
            # Update topic
            meta_json.update({"topic": topic})
            # Save updated meta
            self._save_meta(thread_id, meta_json)
            return True
        except ValueError:
            # self._read_meta returned ValueError. Meta doesn't exist. 
            # Create meta
            meta_json = {
                "topic": topic,
                "root_thread_id": thread_id,
                "parent_thread_id": thread_id,
                "fork_from_index": 0,
            }
            # Save meta
            self._save_meta(thread_id, meta_json)
            return True
        except:
            return False
        
    
    async def delete_thread(
        self,
        thread_id: str,
    ) -> bool:
        thread_path = THREADS_DIR / f"{thread_id}.txt"
        topic_path = THREADS_DIR / f"{thread_id}.meta.json"
        try:
            if os.path.exists(thread_path):
                os.remove(thread_path)
            if os.path.exists(topic_path):
                os.remove(topic_path)
            return True 
        except:
            return False
    

    async def _get_topic(self, thread_id: str, content: List[Dict]) -> None:
        """ 
        If meta file exists, reads topic and returns else summarizes the topic, 
        saves and returns it
        """
        try:
            d = self._read_meta(thread_id)
            return d.get("topic")
        except:
            topic = await summarize_topic(content)
            return topic
        

    def _save_meta(self, thread_id: str, meta: Dict) -> None:
        topic_path = THREADS_DIR / f"{thread_id}.meta.json"
        with open(topic_path, "w", encoding="utf-8") as f:
                json.dump(meta, f)


    def _read_meta(self, thread_id: str) -> None:
        topic_path = THREADS_DIR / f"{thread_id}.meta.json"
        if topic_path.exists():
            with open(topic_path) as f:
                d = json.load(f)
            return d.get("topic")
        else:
            raise ValueError(f"Meta file not found: {thread_id}")


# ──────────────────── Helper functions ──────────────────────────────

def get_latest_files(directory: str, n: int):
    p = Path(directory)

    # Get only files, not directories
    try:
        files = [f for f in p.iterdir() if f.is_file() and f.suffix == ".txt"]
    except:
        files = []

    # Sort by creation time (ctime)
    files_sorted = sorted(files, key=lambda x: x.stat().st_ctime, reverse=True)

    # Return the n latest files
    return files_sorted[:n], len(files)