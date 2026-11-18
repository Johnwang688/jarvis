"""Supply only short-lived ChatGPT access tokens to private app-servers.

Codex owns the refresh credential. Its managed peer reads and rotates it
under the same lock as the account metadata and title peers. Chat workers
use Codex's external-token protocol with an ephemeral credential store.
"""
import base64
import hashlib
import json
import threading
import time

from jarvis.v2.provider import BriefRefused
from . import codex_config
from .codex_rpc import RpcError

managed_lock = threading.Lock()


def _grant(rpc, refresh, deadline):
    status = rpc.request("getAuthStatus", {"includeToken": True, "refreshToken": refresh},
                         deadline=deadline)
    if not isinstance(status, dict) or status.get("authMethod") != "chatgpt":
        raise BriefRefused("Codex requires a managed ChatGPT login")
    token = status.get("authToken")
    try:
        _, payload, _ = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        account = claims["https://api.openai.com/auth"]
        identity = account["chatgpt_account_id"]
        plan = account.get("chatgpt_plan_type")
        if not isinstance(identity, str) or not identity or not isinstance(plan, (str, type(None))):
            raise ValueError()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise BriefRefused("Codex returned an invalid ChatGPT access token") from None
    return {"accessToken": token, "chatgptAccountId": identity, "chatgptPlanType": plan}


class ChatLogin:
    def __init__(self, binary, rpc_factory):
        self.binary = binary
        self.rpc_factory = rpc_factory
        self.account = None
        self.digest = None

    def _read(self):
        with managed_lock:
            deadline = time.monotonic() + 15
            argv, env, home = codex_config.prepare_login(self.binary)
            broker = self.rpc_factory(argv, cwd=home, env=env)
            try:
                broker.start().initialize(deadline=deadline)
                grant = _grant(broker, False, deadline)
                if self.account is not None:
                    if self.account != grant["chatgptAccountId"]:
                        raise RpcError("Codex ChatGPT account changed; reopen this chat")
                    if self.digest == self._digest(grant):
                        grant = _grant(broker, True, deadline)
                        if self.account != grant["chatgptAccountId"]:
                            raise RpcError("Codex refreshed a different ChatGPT account")
                self.account = grant["chatgptAccountId"]
                self.digest = self._digest(grant)
                return grant
            finally:
                broker.close()

    @staticmethod
    def _digest(grant):
        return hashlib.sha256(grant["accessToken"].encode()).digest()

    def attach(self, worker):
        grant = self._read()
        reply = worker.request("account/login/start", {"type": "chatgptAuthTokens", **grant})
        if not isinstance(reply, dict) or reply.get("type") != "chatgptAuthTokens":
            raise RpcError("Codex did not accept the chat login")
        worker.refresh_chat_login = self.refresh

    def refresh(self, params):
        if (not isinstance(params, dict) or params.get("reason") != "unauthorized"
                or set(params) - {"reason", "previousAccountId"}
                or params.get("previousAccountId") not in (None, self.account)):
            raise RpcError("Codex requested an invalid ChatGPT login refresh")
        return self._read()
