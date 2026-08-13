"""SignalWire telephony adapter -- the live outbound-calling path for the
dialer (services/dialer.py, routers/dialer.py). Distinct from
enrichment/deepgram_nova.py, which only transcribes recordings after the
fact and never touches a live call.

SignalWire's REST API is a documented drop-in for Twilio's "Compatibility
API": same resource shape, same auth scheme, same LaML/cXML call-control
markup, reachable at your Space's own hostname instead of api.twilio.com.
Verified against SignalWire's own docs/search results before writing this
(not guessed from memory alone):
  - Base path:  https://<space_url>/api/laml/2010-04-01/Accounts/<project_id>/...
  - Create a call:  POST .../Calls.json  (form-encoded body: To, From, Url, ...)
  - Auth:  HTTP Basic, username=project_id, password=api_token
  - StatusCallback: an outbound-call resource accepts a StatusCallback URL
    that receives call-progress webhooks (queued/ringing/in-progress/
    completed/busy/no-answer/failed/canceled), same field names as Twilio.
  - Call recording is requested via the `Record`/`RecordingStatusCallback`
    params on the call resource (or the <Dial record="..."> attribute when
    bridging, see routers/dialer.py's SWML/LaML response).

NOT independently verified against SignalWire's own docs (their docs site
was unreachable from this environment) and therefore treated as "port from
Twilio's identically-shaped API, flagged rather than presented as
confirmed": the exact webhook signature-validation scheme
(`verify_webhook_signature` below assumes Twilio's X-Twilio-Signature
HMAC-SHA1-over-URL+sorted-params construction, since SignalWire's docs
describe the Compatibility API as accepting "the same webhooks" as
Twilio's). Confirm this against SignalWire's current docs before relying
on it to gate anything security-sensitive beyond a basic sanity check.
"""

import base64
import hashlib
import hmac
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("brainboard.signalwire")


class SignalWireAdapter:
    def __init__(self, project_id: str = "", api_token: str = "", space_url: str = ""):
        self.project_id = project_id
        self.api_token = api_token
        self.space_url = space_url.strip().removeprefix("https://").removeprefix("http://").rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.project_id and self.api_token and self.space_url)

    @property
    def _base_url(self) -> str:
        return f"https://{self.space_url}/api/laml/2010-04-01/Accounts/{self.project_id}"

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self._base_url, auth=(self.project_id, self.api_token), timeout=30)

    def place_call(
        self,
        *,
        to_number: str,
        from_number: str,
        laml_url: str,
        status_callback_url: str | None = None,
    ) -> dict:
        """Create an outbound call. `laml_url` is the endpoint SignalWire
        fetches call-control LaML/cXML from once the call connects (see
        routers/dialer.py's /dialer/laml/outbound handler) -- it is NOT the
        destination being dialed."""
        if not self.enabled:
            raise RuntimeError("SignalWire is not configured (project_id/api_token/space_url)")

        data = {"To": to_number, "From": from_number, "Url": laml_url}
        if status_callback_url:
            data["StatusCallback"] = status_callback_url
            data["StatusCallbackEvent"] = "initiated ringing answered completed"
            data["StatusCallbackMethod"] = "POST"

        with self._client() as client:
            response = client.post("/Calls.json", data=data)
            response.raise_for_status()
            return response.json()

    def verify_credentials(self) -> dict:
        """Fetches the Account resource -- confirms project_id/api_token/
        space_url are actually valid together, with no cost and no need
        for a public callback URL (unlike place_call, this never gets
        called back into)."""
        if not self.enabled:
            raise RuntimeError("SignalWire is not configured (project_id/api_token/space_url)")
        with self._client() as client:
            response = client.get(f"{self._base_url}.json")
            response.raise_for_status()
            return response.json()

    def get_call(self, call_sid: str) -> dict:
        if not self.enabled:
            raise RuntimeError("SignalWire is not configured (project_id/api_token/space_url)")
        with self._client() as client:
            response = client.get(f"/Calls/{call_sid}.json")
            response.raise_for_status()
            return response.json()

    def verify_webhook_signature(self, signing_key: str, url: str, params: dict, signature: str) -> bool:
        """Best-effort validation using Twilio's public X-Twilio-Signature
        construction (see module docstring -- unconfirmed against
        SignalWire's own docs). Returns True (skips validation) when no
        signing key is configured, so local/dev testing against a tunnel
        isn't blocked by default."""
        if not signing_key:
            return True
        payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        expected = base64.b64encode(hmac.new(signing_key.encode(), payload.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(expected, signature or "")


def get_signalwire_adapter() -> SignalWireAdapter:
    settings = get_settings()
    return SignalWireAdapter(
        project_id=settings.signalwire_project_id,
        api_token=settings.signalwire_api_token,
        space_url=settings.signalwire_space_url,
    )
