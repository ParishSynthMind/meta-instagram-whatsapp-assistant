#!/bin/bash
# Loads .env and starts the webhook server on 127.0.0.1 (PORT, default 8000).
cd "$(dirname "$0")"
set -a
[ -f .env ] && source .env
set +a
exec python -m meta_assistant.server   # PORT in .env, default 8000
