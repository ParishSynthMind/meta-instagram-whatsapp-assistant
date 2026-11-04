"""Settings, read from environment variables (or a local .env loaded by run.sh)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HANDOVER_KEYWORDS = [
    "human", "real person", "agent", "manager", "speak to someone", "call me",
    "complaint", "complain", "refund", "cancel", "lawyer", "legal", "chargeback",
]
DEFAULT_CRISIS_KEYWORDS = [
    "suicide", "kill myself", "self harm", "self-harm", "end my life",
    "want to die", "overdose", "emergency", "abuse",
]


def _csv(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Webhook security
    verify_token: str = ""
    meta_app_secret: str = ""          # signs WhatsApp (and Facebook-Login Instagram) webhooks
    instagram_app_secret: str = ""     # signs Instagram-Login webhooks; falls back to meta_app_secret

    # Graph API
    graph_version: str = "v26.0"
    instagram_account_id: str = ""     # your own IG professional account ID (to skip your own comments)
    instagram_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_allowed_numbers: set[str] = field(default_factory=set)  # empty = anyone may message
    page_access_token: str = ""        # Facebook Page token with leads_retrieval, for Lead Ads

    # Behaviour
    enable_instagram: bool = True
    enable_whatsapp: bool = True
    enable_leads: bool = True
    reply_to_comments: bool = True
    dry_run: bool = False              # log what would be sent, never call the Graph API
    model: str = "claude-sonnet-5-5"
    business_profile_path: str = "samples/business_profile.example.md"
    handover_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_HANDOVER_KEYWORDS))
    crisis_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_CRISIS_KEYWORDS))
    per_user_hourly_limit: int = 5
    daily_reply_cap: int = 200
    human_takeover_hours: int = 24
    history_turns: int = 10

    # Storage
    db_path: str = "data/assistant.db"
    reply_log_path: str = "data/reply-log.jsonl"
    lead_log_path: str = "data/leads.jsonl"

    @property
    def instagram_secret(self) -> str:
        return self.instagram_app_secret or self.meta_app_secret

    def business_profile(self) -> str:
        path = Path(self.business_profile_path)
        return path.read_text() if path.exists() else ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            verify_token=e("META_VERIFY_TOKEN", ""),
            meta_app_secret=e("META_APP_SECRET", ""),
            instagram_app_secret=e("INSTAGRAM_APP_SECRET", ""),
            graph_version=e("GRAPH_API_VERSION", "v26.0"),
            instagram_account_id=e("INSTAGRAM_ACCOUNT_ID", ""),
            instagram_access_token=e("INSTAGRAM_ACCESS_TOKEN", ""),
            whatsapp_phone_number_id=e("WHATSAPP_PHONE_NUMBER_ID", ""),
            whatsapp_access_token=e("WHATSAPP_ACCESS_TOKEN", ""),
            whatsapp_allowed_numbers={n.lstrip("+") for n in _csv("WHATSAPP_ALLOWED_NUMBERS")},
            page_access_token=e("PAGE_ACCESS_TOKEN", ""),
            enable_instagram=_bool("ENABLE_INSTAGRAM", True),
            enable_whatsapp=_bool("ENABLE_WHATSAPP", True),
            enable_leads=_bool("ENABLE_LEADS", True),
            reply_to_comments=_bool("REPLY_TO_COMMENTS", True),
            dry_run=_bool("DRY_RUN", False),
            model=e("CLAUDE_MODEL", "claude-sonnet-5-5"),
            business_profile_path=e("BUSINESS_PROFILE_PATH", "samples/business_profile.example.md"),
            handover_keywords=_csv("HANDOVER_KEYWORDS", DEFAULT_HANDOVER_KEYWORDS),
            crisis_keywords=_csv("CRISIS_KEYWORDS", DEFAULT_CRISIS_KEYWORDS),
            per_user_hourly_limit=int(e("PER_USER_HOURLY_LIMIT", "5")),
            daily_reply_cap=int(e("DAILY_REPLY_CAP", "200")),
            human_takeover_hours=int(e("HUMAN_TAKEOVER_HOURS", "24")),
            history_turns=int(e("HISTORY_TURNS", "10")),
            db_path=e("DB_PATH", "data/assistant.db"),
            reply_log_path=e("REPLY_LOG_PATH", "data/reply-log.jsonl"),
            lead_log_path=e("LEAD_LOG_PATH", "data/leads.jsonl"),
        )
