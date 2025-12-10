import os
import requests

from fastmcp.server.auth.providers.jwt import JWTVerifier


# ── Auth (JWT via JWKS discovery) ─────────────────────────────────────────────
REDIRECT_URL = os.getenv("OIDC_ISSUER",   "https://www.freva.dkrz.de/api/freva-nextgen/")
# REDIRECT_URL = os.getenv("OIDC_ISSUER",   "https://freva-keycloak.cloud.dkrz.de/realms/Freva/") # Uncomment in case <freva.dkrz.de> is down
AUDIENCE = os.getenv("OIDC_AUDIENCE", "freva")
REQUIRED_SCOPES = [s.strip() for s in os.getenv("MCP_REQUIRED_SCOPES", "openid profile").split(",") if s.strip()]

disc = requests.get(REDIRECT_URL.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json()
JWKS_URI = disc["jwks_uri"]
TOKEN_ISSUER = disc["issuer"]

jwt_verifier = JWTVerifier(
    jwks_uri=JWKS_URI,
    issuer=TOKEN_ISSUER,
    audience=AUDIENCE,
)
