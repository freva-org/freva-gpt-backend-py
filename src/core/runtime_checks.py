import logging
from src.core.settings import Settings

logger = logging.getLogger(__name__)

#TODO: validate parity with Rust checks

def run_startup_checks(settings: Settings) -> None:
    # AUTH_KEY is required by Rust
    if not settings.AUTH_KEY:
        # In Rust this would be a hard error; raise to fail fast at startup.
        raise RuntimeError("Missing AUTH_KEY environment variable (required).")

    # LITE_LLM_ADDRESS sanity note
    if not (settings.LITE_LLM_ADDRESS.startswith("http://") or settings.LITE_LLM_ADDRESS.startswith("https://")):
        logger.warning("LITE_LLM_ADDRESS does not look like a URL: %s", settings.LITE_LLM_ADDRESS)

    # TODO: optional ping to LiteLLM for liveliness (warning only)
    logger.info(
        "Startup checks passed. Port=%s GuestsAllowed=%s LiteLLM=%s Version=%s",
        settings.BACKEND_PORT,
        settings.ALLOW_GUESTS,
        settings.LITE_LLM_ADDRESS,
        settings.VERSION,
    )