import logging

from freva_gpt import __version__
from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.settings import Settings

logger = configure_logging(__name__)

# TODO: code-interpreter tests
# TODO: ping to LiteLLM for liveliness (warning only)


def run_startup_checks(settings: Settings) -> None:

    # LITE_LLM_ADDRESS sanity note
    if not (
        settings.LITE_LLM_ADDRESS.startswith("http://")
        or settings.LITE_LLM_ADDRESS.startswith("https://")
    ):
        logger.warning(
            "LITE_LLM_ADDRESS does not look like a URL: %s",
            settings.LITE_LLM_ADDRESS,
        )

    logger.info(
        "Startup checks passed. Port=%s GuestsAllowed=%s LiteLLM=%s Version=%s",
        settings.BACKEND_PORT,
        settings.ALLOW_GUESTS,
        settings.LITE_LLM_ADDRESS,
        __version__,
    )
