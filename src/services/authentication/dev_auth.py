from .authenticator import Authenticator


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
            self.freva_config_path = request.headers.get("freva-config-path", "")
        else:
            self.username = "janedoe"
            self.vault_url = "http://dev-vault"
            self.rest_url = "http://dev-rest"
            self.access_token = "Access-token"
            self.freva_config_path = ""

        return self
