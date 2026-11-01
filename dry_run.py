"""Run the whole webhook pipeline on the sample payloads, no Meta account needed.

  python dry_run.py --offline   canned AI step, no keys at all
  python dry_run.py             real Claude call (needs ANTHROPIC_API_KEY)

Each sample is signed the way Meta signs it, posted to the real FastAPI app,
verified, parsed and run through the guardrails. Graph API calls are recorded
and printed instead of sent. The first sample is then replayed to show that
a retried delivery is not answered twice.
"""
import argparse
import json
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from meta_assistant.config import Settings
from meta_assistant.handlers import Assistant
from meta_assistant.llm import AnthropicReplier, OfflineReplier
from meta_assistant.meta_api import RecordingMetaClient
from meta_assistant.server import create_app
from meta_assistant.signature import compute_signature
from meta_assistant.store import Store

SAMPLES = Path(__file__).parent / "samples"
DEMO_SECRET = "dry-run-demo-secret"
RUN = [
    ("instagram", "instagram_comment", "Instagram comment with a question"),
    ("instagram", "instagram_comment_price", "Instagram comment asking for a price"),
    ("instagram", "instagram_comment_own", "Our own comment (must be ignored)"),
    ("instagram", "instagram_dm", "Instagram DM"),
    ("instagram", "instagram_dm_echo", "Echo of our own DM (must be ignored)"),
    ("instagram", "instagram_dm_handover", "Instagram DM with a complaint"),
    ("whatsapp", "whatsapp_text", "WhatsApp booking request"),
    ("whatsapp", "whatsapp_followup", "WhatsApp question"),
    ("whatsapp", "whatsapp_image", "WhatsApp photo"),
    ("whatsapp", "whatsapp_status", "WhatsApp delivery receipt (must be ignored)"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="canned replies, no API key")
    args = ap.parse_args()

    settings = Settings(verify_token="demo", meta_app_secret=DEMO_SECRET, instagram_account_id="17841400000000001",
                        business_profile_path=str(SAMPLES / "business_profile.example.md"))
    store = Store(":memory:")
    meta = RecordingMetaClient()
    replier = OfflineReplier() if args.offline else AnthropicReplier(settings.model)
    assistant = Assistant(settings, store, replier, meta)
    client = TestClient(create_app(settings, assistant))

    print(f"Mode: {'offline (canned replies)' if args.offline else 'live Claude (' + settings.model + ')'}\n")
    for product, name, label in RUN + [RUN[0]]:
        raw = (SAMPLES / f"{name}.json").read_bytes()
        before_logs, before_calls = len(store.all_logs()), len(meta.calls)
        resp = client.post(f"/webhooks/{product}", content=raw,
                           headers={"X-Hub-Signature-256": compute_signature(DEMO_SECRET, raw)})
        replay = " (REPLAYED: same delivery again)" if before_logs and name == RUN[0][1] and len(store.all_logs()) == before_logs else ""
        print(f"== {label}{replay}")
        inbound = _inbound_text(json.loads(raw))
        if inbound:
            print(f"   in:   {inbound}")
        print(f"   HTTP {resp.status_code} {resp.text}")
        for row in store.all_logs()[before_logs:]:
            print(f"   action: {row['action']}  ({row['reason']})")
        for call in meta.calls[before_calls:]:
            if call[0] == "mark_whatsapp_read":
                continue
            print(f"   would call {call[0]}({call[1]}):")
            print(textwrap.indent(textwrap.fill(call[2], 90), "      | "))
        if len(store.all_logs()) == before_logs and len(meta.calls) == before_calls:
            print("   nothing sent (ignored or duplicate)")
        print()

    # Show the bad-signature path too.
    raw = (SAMPLES / "whatsapp_text.json").read_bytes()
    resp = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    print(f"== Forged request with a wrong signature\n   HTTP {resp.status_code} {resp.text}\n")
    sent = [r for r in store.all_logs() if r["reply"]]
    print(f"Reply log: {len(store.all_logs())} rows, {len(sent)} replies, "
          f"{sum(r['action'] == 'handover' for r in sent)} hand-overs to a human.")


def _inbound_text(payload: dict) -> str:
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            if "text" in v and isinstance(v["text"], str):
                return v["text"]
            for m in v.get("messages", []):
                return m.get("text", {}).get("body") or f"[{m.get('type')}]"
        for m in entry.get("messaging", []):
            return m.get("message", {}).get("text", "")
    return ""


if __name__ == "__main__":
    main()
