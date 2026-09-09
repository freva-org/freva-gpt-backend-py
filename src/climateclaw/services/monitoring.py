from prometheus_client import Counter, Gauge

BOT_REQUESTS = Counter(
    "climateclaw_bot_requests_total",
    "Total number of requests sent to ClimateClaw",
    ["model"],
)

LLM_REQUESTS = Counter(
    "climateclaw_llm_requests_total",
    "Total number of requests sent to LLM providers",
    ["model"],
)

USERS_TOTAL = Gauge(
    "climateclaw_users_total",
    "Total number of unique ClimateClaw users",
)

CONVERSATIONS_TOTAL = Gauge(
    "climateclaw_conversations_total",
    "Total number of ClimateClaw conversations",
)
