from typing import Dict, List, Literal
from pathlib import Path
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from pymongo import AsyncMongoClient

from freva_gpt.core.settings import get_settings
from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.streaming.stream_variants import StreamVariant, SVUser
from freva_gpt.services.streaming.litellm_client import acomplete, first_text

logger = configure_logging(__name__)

# ──────────────────── Config from settings.py ────────────────────────────

settings = get_settings()
MONGODB_DATABASE_NAME = settings.MONGODB_DATABASE_NAME
MONGODB_COLLECTION_NAME = settings.MONGODB_COLLECTION_NAME

CACHE_ROOT = Path("./cache")

# ──────────────────────────── Model ───────────────────────────────────

@dataclass
class Thread:
    user_id: str
    thread_id: str
    date: str  # ISO 8601
    topic: str
    content: List[StreamVariant]

# ──────────────────── Helper Functions ──────────────────────────────

def create_dir_at_cache(
    user_id: str, 
    thread_id: str
) -> None:
    """
    Create cache/{user_id}/{thread_id}. On failure (e.g., non-alphanumeric user_id),
    retry with a sanitized user_id (keep only [A-Za-z0-9]). Logs but never raises.
    """
    cache = CACHE_ROOT / thread_id
    try:
        cache.mkdir(parents=True, exist_ok=True)
        logger.debug("cache created or exists: %s", cache)
        return
    except Exception as e:
        logger.debug("Failed to create cache=%s, err=%s -- retrying with sanitized user_id", cache, e)


# ──────────────────── Summarization for topic ────────────────────

def _fallback_topic(raw: str | None) -> str:
    if not raw:
        return "Untitled"
    # naive single-line truncation
    s = " ".join(raw.split())
    return (s[:80] + "…") if len(s) > 80 else s


async def summarize_topic(content: List[Dict]) -> str:
    """
    Try LiteLLM; on any failure, return a safe fallback so requests don't crash.
    Only the first user text is taken into account.
    """
    if isinstance(content[0], Dict):
        topic = next(
            (item.get("content", "") for item in content if item.get("variant") == "user"),
            "Untitled"
        )
    else:
        topic = next(
            (sv.text for sv in content if isinstance(sv, SVUser)),
            "Untitled"
        )

    prompt = (
        "Summarize this chat topic in at most ~12 words, neutral tone.\n\n"
        f"Topic:\n{(topic or '')[:2000]}"
    )
    try:
        resp = await acomplete(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            max_tokens=50,
            temperature=0.2,
        )
        text = (first_text(resp) or "").strip()
        return text or _fallback_topic(topic)
    except Exception as e:
        logger.warning("summarize_topic: falling back due to error: %s", e)
        return _fallback_topic(topic)


# ──────────────────── Connection ──────────────────────────────

async def get_mongodb_uri(vault_url: str) -> str:
    # 1) GET vault_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(vault_url)
    except Exception:
        # 503 ServiceUnavailable
        raise HTTPException(status_code=503, detail="Error sending request to vault.")
    if not r.is_success:
        # 502 BadGateway
        raise HTTPException(status_code=502, detail="Failed to get MongoDB URL. Is Nginx running correctly?")

    # 2) Parse JSON and extract key
    try:
        data = r.json()
    except Exception:
        # 502 BadGateway
        raise HTTPException(status_code=502, detail="Vault response was malformed.")

    uri = data.get("mongodb.url") or data.get("mongo.url")
    if not uri:
        # 502 BadGateway
        raise HTTPException(status_code=502, detail="MongoDB URL not found in vault response.")
    return uri.strip()


async def get_database(
        vault_url: str
    ) -> AsyncMongoClient:
        """
        Parity with Rust: fetch URI from vault via auth.get_mongodb_uri, connect with Motor.
        If connection fails, retry once without URI options (strip trailing ?query).
        """
        mongodb_uri = await get_mongodb_uri(vault_url)

        try:
            client = AsyncMongoClient(mongodb_uri)
            return client[MONGODB_DATABASE_NAME]
        except Exception:
            # Rust-style fallback: strip query options and retry once
            if "?" in mongodb_uri:
                stripped = mongodb_uri.rsplit("?", 1)[0]
                try:
                    client = AsyncMongoClient(stripped)
                    return client[MONGODB_DATABASE_NAME]
                except Exception:
                    pass
            raise HTTPException(status_code=503, detail="Failed to connect to MongoDB")


# ──────────────────── Search threads ──────────────────────────────

Variant = Literal["User", "Assistant", "Code", "CodeOutput"]


PREFIX_MAP: Dict[str, Variant] = {
    # user variants
    "user": "User", "u": "User", "input": "User", "me": "User", "question": "User",
    "request": "User", "i": "User", "benutzer": "User", "eingabe": "User",
    # assistant variants
    "ai": "Assistant", "a": "Assistant", "assistant": "Assistant",
    "frevagpt": "Assistant", "freva-gpt": "Assistant", "freva_gpt": "Assistant",
    "answer": "Assistant", "ki": "Assistant", "assistent": "Assistant",
    "computer": "Assistant",
    # code input variants
    "code_input": "Code", "ci": "Code", "code": "Code", "codeinput": "Code",
    "python": "Code", "py": "Code",
    # code output variants
    "code_output": "CodeOutput", "co": "CodeOutput", "codeoutput": "CodeOutput",
    "output": "CodeOutput", "ausgabe": "CodeOutput", "ergebnis": "CodeOutput",
}

VARIANT_FIELD: Dict[Variant, str] = {
    "User": "user_text",
    "Assistant": "assistant_text",
    "Code": "code_input",
    "CodeOutput": "code_output",
}
