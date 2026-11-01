"""Turn webhook payloads into events, and decide what (if anything) to send back.

Order of checks for every event, cheapest and safest first:
  1. dedupe (Meta retries deliveries for up to 36 hours)
  2. skip our own comments and DM echoes (no reply loops)
  3. WhatsApp allowlist, if one is set
  4. human takeover active for this person -> stay silent
  5. crisis / hand-over keywords -> fixed message, no model call
  6. per-person hourly limit and daily cap
  7. Claude drafts a reply, and may flag that a human should take over
  8. outbound check for prices, promises and length -> generic safe message if it fails
  9. send, store history, log
"""
import logging
import time
from dataclasses import dataclass, field

from . import guardrails
from .llm import Replier

log = logging.getLogger("meta_assistant")


@dataclass
class Event:
    channel: str            # instagram_comment | instagram_dm | whatsapp
    event_id: str
    user_id: str
    text: str
    username: str = ""
    msg_type: str = "text"
    extra: dict = field(default_factory=dict)


def parse_instagram(payload: dict, own_account_id: str = "") -> tuple[list[Event], list[str]]:
    """Returns (events, skipped_reasons)."""
    events, skipped = [], []
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for change in entry.get("changes", []):
            if change.get("field") not in ("comments", "live_comments"):
                skipped.append(f"ignored field {change.get('field')}")
                continue
            v = change.get("value", {})
            sender = v.get("from", {})
            if str(sender.get("id")) in {own_account_id, account_id} - {""}:
                skipped.append("own comment")
                continue
            events.append(Event("instagram_comment", f"igc:{v.get('id')}", str(sender.get("id", "")),
                                v.get("text", ""), sender.get("username", ""),
                                extra={"comment_id": v.get("id"), "media_id": v.get("media", {}).get("id")}))
        for m in entry.get("messaging", []):
            msg = m.get("message")
            if not msg:
                skipped.append("non-message messaging event")  # reads, reactions, postbacks
                continue
            if msg.get("is_echo") or str(m.get("sender", {}).get("id")) in {own_account_id, account_id} - {""}:
                skipped.append("echo of our own message")
                continue
            msg_type = "text" if msg.get("text") else "attachment"
            events.append(Event("instagram_dm", f"igm:{msg.get('mid')}", str(m["sender"]["id"]),
                                msg.get("text", ""), msg_type=msg_type))
    return events, skipped


def parse_whatsapp(payload: dict) -> tuple[list[Event], list[str]]:
    events, skipped = [], []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            if v.get("statuses"):
                skipped.append(f"{len(v['statuses'])} delivery status update(s)")
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
            for msg in v.get("messages", []):
                msg_type = msg.get("type", "")
                text = msg.get("text", {}).get("body", "") if msg_type == "text" else ""
                if msg_type == "button":
                    text, msg_type = msg.get("button", {}).get("text", ""), "text"
                events.append(Event("whatsapp", f"wa:{msg.get('id')}", str(msg.get("from", "")), text,
                                    names.get(msg.get("from"), ""), msg_type,
                                    extra={"message_id": msg.get("id")}))
    return events, skipped


class Assistant:
    def __init__(self, settings, store, replier: Replier, meta, alert=None, clock=time.time):
        self.s = settings
        self.store = store
        self.replier = replier
        self.meta = meta
        self.alert = alert or (lambda event, reason: log.warning("ALERT %s %s: %s", event.channel, event.user_id, reason))
        self.clock = clock

    # Entry point from the webhook ------------------------------------------
    def process_payload(self, product: str, payload: dict) -> list[dict]:
        if product == "instagram":
            events, skipped = parse_instagram(payload, self.s.instagram_account_id)
        else:
            events, skipped = parse_whatsapp(payload)
        for reason in skipped:
            log.info("skipped: %s", reason)
        results = []
        for event in events:
            try:
                results.append(self.handle(event))
            except Exception as exc:  # never let one bad event break the batch
                log.exception("unhandled error on %s", event.event_id)
                results.append(self._log(event, "", "error", f"unhandled: {exc}"))
        return results

    # One event ---------------------------------------------------------------
    def handle(self, ev: Event) -> dict:
        if not self.store.claim_event(ev.event_id, ev.channel):
            return {"event_id": ev.event_id, "action": "duplicate", "sent": False}
        if ev.channel == "instagram_comment" and not self.s.reply_to_comments:
            return self._log(ev, "", "skipped", "comment replies switched off")
        if ev.channel == "whatsapp" and self.s.whatsapp_allowed_numbers \
                and ev.user_id.lstrip("+") not in self.s.whatsapp_allowed_numbers:
            return self._log(ev, "", "skipped", "sender not on WhatsApp allowlist")
        if ev.channel == "whatsapp":
            self._mark_read(ev)
        if self.store.takeover_active(ev.channel, ev.user_id):
            self.store.add_history(ev.channel, ev.user_id, "user", ev.text)
            return self._log(ev, "", "skipped", "human takeover active")
        if ev.msg_type != "text" or not ev.text.strip():
            return self._send(ev, guardrails.UNSUPPORTED_REPLY, "fixed_reply", f"unsupported type {ev.msg_type}")

        decision = guardrails.check_inbound(ev.text, self.s.handover_keywords, self.s.crisis_keywords)
        if decision.action == "crisis":
            self.store.set_takeover(ev.channel, ev.user_id, self.s.human_takeover_hours, "crisis")
            self.alert(ev, f"crisis keyword '{decision.matched}'")
            reply = guardrails.CRISIS_REPLY if ev.channel != "instagram_comment" \
                else guardrails.HANDOVER_REPLY["instagram_comment"]
            return self._send(ev, reply, "crisis", f"keyword '{decision.matched}'")
        if decision.action == "handover":
            return self._handover(ev, f"keyword '{decision.matched}'")

        now = self.clock()
        if self.store.replies_since(now - 3600, ev.user_id) >= self.s.per_user_hourly_limit:
            return self._log(ev, "", "rate_limited", f"over {self.s.per_user_hourly_limit} replies/hour for this person")
        day_start = now - (now % 86400)
        if self.store.replies_since(day_start) >= self.s.daily_reply_cap:
            return self._log(ev, "", "rate_limited", f"daily cap of {self.s.daily_reply_cap} reached")

        history = [] if ev.channel == "instagram_comment" else \
            self.store.recent_history(ev.channel, ev.user_id, self.s.history_turns)
        try:
            draft = self.replier.draft(ev.channel, history, ev.text, self.s.business_profile())
        except Exception as exc:
            # No reply rather than a 5xx: Meta would retry and risk a double reply later.
            return self._log(ev, "", "error", f"AI step failed: {exc}")

        check = guardrails.check_outbound(draft.reply, ev.channel)
        if draft.handover:
            if check.ok:  # a safe, specific holding reply beats the generic one; a person still takes over
                self.store.set_takeover(ev.channel, ev.user_id, self.s.human_takeover_hours, draft.reason)
                return self._send(ev, draft.reply, "handover", f"model asked for a human: {draft.reason}")
            return self._handover(ev, f"model asked for a human: {draft.reason}", draft.reply)
        if not check.ok:
            return self._handover(ev, "blocked by outbound guardrail: " + "; ".join(check.violations), draft.reply)
        return self._send(ev, draft.reply, "ai_reply", draft.reason)

    # Helpers ---------------------------------------------------------------
    def _handover(self, ev: Event, reason: str, blocked_draft: str = "") -> dict:
        self.store.set_takeover(ev.channel, ev.user_id, self.s.human_takeover_hours, reason)
        if blocked_draft:
            reason += f" | draft not sent: {blocked_draft!r}"
        return self._send(ev, guardrails.HANDOVER_REPLY[ev.channel], "handover", reason)

    def _send(self, ev: Event, reply: str, action: str, reason: str) -> dict:
        try:
            if self.s.dry_run:
                reason = f"[dry run] {reason}"
            elif ev.channel == "instagram_comment":
                self.meta.reply_to_comment(ev.extra["comment_id"], reply)
            elif ev.channel == "instagram_dm":
                self.meta.send_instagram_dm(ev.user_id, reply)
            else:
                self.meta.send_whatsapp_text(ev.user_id, reply, ev.extra.get("message_id"))
        except Exception as exc:
            # The reply is kept in the log even though delivery failed.
            return self._log(ev, reply, action, reason, sent=False, error=str(exc))
        if ev.channel != "instagram_comment":
            self.store.add_history(ev.channel, ev.user_id, "user", ev.text)
            self.store.add_history(ev.channel, ev.user_id, "assistant", reply)
        return self._log(ev, reply, action, reason, sent=not self.s.dry_run)

    def _mark_read(self, ev: Event) -> None:
        if self.s.dry_run:
            return
        try:
            self.meta.mark_whatsapp_read(ev.extra["message_id"])
        except Exception as exc:
            log.info("read receipt failed for %s: %s", ev.event_id, exc)

    def _log(self, ev: Event, reply: str, action: str, reason: str, sent: bool = False, error: str = "") -> dict:
        row = self.store.log(channel=ev.channel, user_id=ev.user_id, event_id=ev.event_id, inbound=ev.text,
                             reply=reply, action=action, reason=reason, sent=sent, error=error)
        log.info("%s %s -> %s (%s)%s", ev.channel, ev.event_id, action, reason, f" ERROR {error}" if error else "")
        return row
