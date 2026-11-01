"""Thin Graph API client for the four calls this service makes.

Instagram (Instagram API with Instagram Login, graph.instagram.com):
  POST /{comment-id}/replies        public reply under a comment
  POST /me/messages                 DM to an Instagram-scoped user ID
WhatsApp Cloud API (graph.facebook.com):
  POST /{phone-number-id}/messages  text message, and the "read" receipt
"""
import httpx


class MetaAPIError(RuntimeError):
    pass


class MetaClient:
    def __init__(self, settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=15.0)

    def _post(self, url: str, token: str, *, json: dict | None = None, data: dict | None = None) -> dict:
        resp = self.http.post(url, json=json, data=data, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 300:
            try:
                err = resp.json().get("error", {})
                detail = f"{err.get('code')} {err.get('type')}: {err.get('message')}"
            except Exception:
                detail = resp.text[:300]
            raise MetaAPIError(f"Graph API {resp.status_code} on {url.split('?')[0]}: {detail}")
        return resp.json()

    def reply_to_comment(self, comment_id: str, text: str) -> dict:
        url = f"https://graph.instagram.com/{self.s.graph_version}/{comment_id}/replies"
        return self._post(url, self.s.instagram_access_token, data={"message": text})

    def send_instagram_dm(self, igsid: str, text: str) -> dict:
        url = f"https://graph.instagram.com/{self.s.graph_version}/me/messages"
        return self._post(url, self.s.instagram_access_token,
                          json={"recipient": {"id": igsid}, "message": {"text": text}})

    def send_whatsapp_text(self, to: str, text: str, reply_to: str | None = None) -> dict:
        url = f"https://graph.facebook.com/{self.s.graph_version}/{self.s.whatsapp_phone_number_id}/messages"
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        if reply_to:
            body["context"] = {"message_id": reply_to}
        return self._post(url, self.s.whatsapp_access_token, json=body)

    def mark_whatsapp_read(self, message_id: str) -> dict:
        url = f"https://graph.facebook.com/{self.s.graph_version}/{self.s.whatsapp_phone_number_id}/messages"
        return self._post(url, self.s.whatsapp_access_token,
                          json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id})


class RecordingMetaClient:
    """Stands in for MetaClient in dry runs and tests: records every call, sends nothing."""

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[tuple] = []
        self.fail_on = fail_on or set()

    def _record(self, name: str, *args) -> dict:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise MetaAPIError(f"simulated failure in {name}")
        return {"recorded": True}

    def reply_to_comment(self, comment_id, text):
        return self._record("reply_to_comment", comment_id, text)

    def send_instagram_dm(self, igsid, text):
        return self._record("send_instagram_dm", igsid, text)

    def send_whatsapp_text(self, to, text, reply_to=None):
        return self._record("send_whatsapp_text", to, text)

    def mark_whatsapp_read(self, message_id):
        return self._record("mark_whatsapp_read", message_id)
