from src.core.logging_setup import configure_logging
from .authenticator import Authenticator

log = configure_logging(__name__)


class DevAuthenticator(Authenticator):
    async def run(self) -> "DevAuthenticator":
        """
        DEV mode:
        - no real token validation
        - username, vault_url, rest_url come from headers or defaults
        """
        request = self.request

        if request:
            self.username = request.headers.get("x-dev-user", "janedoe")
            self.vault_url = request.headers.get("x-dev-vault-url", "http://dev-vault")
            self.rest_url = request.headers.get("x-dev-rest-url", "http://dev-rest")
            self.access_token = request.headers.get("Authorization", "Access-token")
        else:
            self.username = "janedoe"
            self.vault_url = "http://dev-vault"
            self.rest_url = "http://dev-rest"
            self.access_token = "Access-token"

        log.info(
            "DEV auth applied",
            extra={"user_id": self.username, "vault_url": self.vault_url, "rest_url": self.rest_url},
        )
        return self
