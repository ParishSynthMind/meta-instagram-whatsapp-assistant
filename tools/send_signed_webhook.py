"""Sign a sample payload with your app secret and POST it to a running server,
exactly as Meta would. Useful for testing a deployed or tunnelled endpoint.

  python tools/send_signed_webhook.py https://<public-url>/webhooks/whatsapp samples/whatsapp_text.json

Reads META_APP_SECRET (or INSTAGRAM_APP_SECRET with --instagram) from the environment.
"""
import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from meta_assistant.signature import compute_signature  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("payload")
    ap.add_argument("--instagram", action="store_true", help="sign with INSTAGRAM_APP_SECRET")
    args = ap.parse_args()
    secret = os.environ.get("INSTAGRAM_APP_SECRET" if args.instagram else "META_APP_SECRET") \
        or os.environ.get("META_APP_SECRET", "")
    if not secret:
        sys.exit("Set META_APP_SECRET (or INSTAGRAM_APP_SECRET) first.")
    raw = Path(args.payload).read_bytes()
    r = httpx.post(args.url, content=raw, timeout=20,
                   headers={"X-Hub-Signature-256": compute_signature(secret, raw), "Content-Type": "application/json"})
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
