import os
import requests

from fastmcp.server.auth.providers.jwt import JWTVerifier


# ── Auth (JWT via JWKS discovery) ─────────────────────────────────────────────
ISSUER   = os.getenv("OIDC_ISSUER",   "https://www.freva.dkrz.de/api/freva-nextgen/")
AUDIENCE = os.getenv("OIDC_AUDIENCE", "freva")
REQUIRED_SCOPES = [s.strip() for s in os.getenv("MCP_REQUIRED_SCOPES", "openid profile").split(",") if s.strip()]

disc = requests.get(ISSUER.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json()
JWKS_URI = disc["jwks_uri"]

jwt_verifier = JWTVerifier(
    jwks_uri=JWKS_URI,
    issuer=ISSUER,
    audience=AUDIENCE,
)
