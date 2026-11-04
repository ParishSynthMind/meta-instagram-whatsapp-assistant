import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from meta_assistant import guardrails
from meta_assistant.config import Settings
from meta_assistant.handlers import Assistant, parse_instagram, parse_whatsapp
from meta_assistant.llm import AnthropicReplier, Draft, build_messages
from meta_assistant.meta_api import MetaAPIError, MetaClient, RecordingMetaClient
from meta_assistant.server import create_app
from meta_assistant.signature import compute_signature, is_valid_signature
from meta_assistant.store import Store

SAMPLES = Path(__file__).parent.parent / "samples"
SECRET = "test-app-secret"
IG_SECRET = "test-instagram-secret"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text())


class FakeReplier:
    def __init__(self, reply="Happy to help! We're open Friday from 14:00.", handover=False, fail=False):
        self.reply, self.handover, self.fail = reply, handover, fail
        self.calls = []

    def draft(self, channel, history, message, business_profile):
        self.calls.append({"channel": channel, "history": history, "message": message})
        if self.fail:
            raise RuntimeError("API down")
        return Draft(self.reply, self.handover, "test")


class Clock:
    def __init__(self, t=1_793_030_400.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def settings():
    return Settings(verify_token="verify-me", meta_app_secret=SECRET, instagram_app_secret=IG_SECRET,
                    instagram_account_id="17841400000000001", business_profile_path=str(SAMPLES / "business_profile.example.md"))


def make(settings, replier=None, meta=None, clock=None, alert=None):
    clock = clock or Clock()
    store = Store(":memory:", clock=clock)
    replier = replier or FakeReplier()
    meta = meta or RecordingMetaClient()
    return Assistant(settings, store, replier, meta, alert=alert, clock=clock), store, replier, meta


def post(client, product, payload, secret):
    raw = json.dumps(payload).encode()
    return client.post(f"/webhooks/{product}", content=raw,
                       headers={"X-Hub-Signature-256": compute_signature(secret, raw), "Content-Type": "application/json"})


# Signature ---------------------------------------------------------------------

def test_signature_roundtrip_and_rejections():
    body = b'{"object":"instagram"}'
    sig = compute_signature(SECRET, body)
    assert sig.startswith("sha256=")
    assert is_valid_signature(SECRET, body, sig)
    assert is_valid_signature(SECRET, body, sig.upper().replace("SHA256=", "sha256="))
    assert not is_valid_signature(SECRET, body + b" ", sig)
    assert not is_valid_signature("other", body, sig)
    assert not is_valid_signature(SECRET, body, sig.split("=", 1)[1])  # no prefix
    assert not is_valid_signature(SECRET, body, None)
    assert not is_valid_signature("", body, sig)


# HTTP layer --------------------------------------------------------------------

def test_verification_handshake(settings):
    assistant, *_ = make(settings)
    client = TestClient(create_app(settings, assistant))
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"})
    assert bad.status_code == 403
    assert client.get("/webhooks/telegram").status_code == 404


def test_bad_signature_is_rejected_and_nothing_runs(settings):
    assistant, store, replier, meta = make(settings)
    client = TestClient(create_app(settings, assistant))
    raw = json.dumps(sample("whatsapp_text")).encode()
    r = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": compute_signature("wrong", raw)})
    assert r.status_code == 403
    r = client.post("/webhooks/whatsapp", content=raw)
    assert r.status_code == 403
    assert meta.calls == [] and replier.calls == [] and store.all_logs() == []


def test_each_product_uses_its_own_secret(settings):
    assistant, *_ = make(settings)
    client = TestClient(create_app(settings, assistant))
    assert post(client, "instagram", sample("instagram_dm"), SECRET).status_code == 403
    assert post(client, "instagram", sample("instagram_dm"), IG_SECRET).status_code == 200
    assert post(client, "whatsapp", sample("whatsapp_text"), IG_SECRET).status_code == 403


def test_signed_whatsapp_message_gets_ai_reply(settings):
    assistant, store, replier, meta = make(settings)
    client = TestClient(create_app(settings, assistant))
    r = post(client, "whatsapp", sample("whatsapp_text"), SECRET)
    assert r.status_code == 200 and r.text == "EVENT_RECEIVED"
    assert meta.calls[0] == ("mark_whatsapp_read", "wamid.DEMO000000000000000000000001")
    assert meta.calls[1][0:2] == ("send_whatsapp_text", "15550001111")
    logs = store.all_logs()
    assert logs[-1]["action"] == "ai_reply" and logs[-1]["sent"] == 1


def test_wrong_object_and_bad_json_are_not_processed(settings):
    assistant, store, replier, meta = make(settings)
    client = TestClient(create_app(settings, assistant))
    assert post(client, "whatsapp", {"object": "page", "entry": []}, SECRET).text == "ignored"
    raw = b"not json"
    r = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": compute_signature(SECRET, raw)})
    assert r.status_code == 400
    assert meta.calls == []


# Parsing -----------------------------------------------------------------------

def test_parsers_skip_own_comments_echoes_and_statuses():
    events, skipped = parse_instagram(sample("instagram_comment_own"), "17841400000000001")
    assert events == [] and skipped == ["own comment"]
    events, skipped = parse_instagram(sample("instagram_dm_echo"), "17841400000000001")
    assert events == [] and "echo" in skipped[0]
    events, skipped = parse_whatsapp(sample("whatsapp_status"))
    assert events == [] and "status" in skipped[0]
    events, _ = parse_instagram(sample("instagram_comment"), "17841400000000001")
    assert events[0].channel == "instagram_comment" and events[0].extra["comment_id"] == "18100000000000001"


# Guardrails and flows ----------------------------------------------------------

def test_comment_reply_goes_to_comment_endpoint(settings):
    assistant, store, replier, meta = make(settings)
    assistant.process_payload("instagram", sample("instagram_comment"))
    assert meta.calls == [("reply_to_comment", "18100000000000001", replier.reply)]
    assert replier.calls[0]["history"] == []  # comments never carry DM history


def test_duplicate_delivery_is_answered_once(settings):
    assistant, store, replier, meta = make(settings)
    assistant.process_payload("instagram", sample("instagram_dm"))
    second = assistant.process_payload("instagram", sample("instagram_dm"))
    assert second[0]["action"] == "duplicate"
    assert len([c for c in meta.calls if c[0] == "send_instagram_dm"]) == 1


def test_handover_keyword_skips_model_and_silences_bot(settings):
    assistant, store, replier, meta = make(settings)
    res = assistant.process_payload("instagram", sample("instagram_dm_handover"))
    assert res[0]["action"] == "handover" and replier.calls == []
    assert meta.calls[0][2] == guardrails.HANDOVER_REPLY["instagram_dm"]
    follow = sample("instagram_dm_handover")
    follow["entry"][0]["messaging"][0]["message"].update(mid="another-mid", text="hello?")
    res = assistant.process_payload("instagram", follow)
    assert res[0]["action"] == "skipped" and "takeover" in res[0]["reason"]
    assert len(meta.calls) == 1


def test_takeover_expires(settings):
    clock = Clock()
    assistant, store, replier, meta = make(settings, clock=clock)
    store.set_takeover("whatsapp", "15550001111", settings.human_takeover_hours, "test")
    assert store.takeover_active("whatsapp", "15550001111")
    clock.t += settings.human_takeover_hours * 3600 + 1
    assert not store.takeover_active("whatsapp", "15550001111")


def test_crisis_message_alerts_a_person(settings):
    alerts = []
    assistant, store, replier, meta = make(settings, alert=lambda ev, reason: alerts.append(reason))
    payload = sample("whatsapp_text")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "I want to end my life"
    res = assistant.process_payload("whatsapp", payload)
    assert res[0]["action"] == "crisis" and replier.calls == []
    assert alerts and "end my life" in alerts[0]
    assert meta.calls[-1][2] == guardrails.CRISIS_REPLY


def test_price_in_model_output_is_blocked(settings):
    assistant, store, replier, meta = make(settings, replier=FakeReplier(reply="Gel nails are 120 AED, see you soon!"))
    res = assistant.process_payload("instagram", sample("instagram_comment_price"))
    assert res[0]["action"] == "handover"
    assert "120 AED" not in meta.calls[0][2]
    assert "price" in res[0]["reason"] and "draft not sent" in res[0]["reason"]


def test_model_handover_sends_its_safe_draft_and_flags_takeover(settings):
    draft = "Thanks! A team member will confirm your Saturday booking personally."
    assistant, store, replier, meta = make(settings, replier=FakeReplier(reply=draft, handover=True))
    res = assistant.process_payload("whatsapp", sample("whatsapp_text"))
    assert res[0]["action"] == "handover" and meta.calls[-1][2] == draft
    assert store.takeover_active("whatsapp", "15550001111")


def test_model_handover_with_unsafe_draft_uses_generic_message(settings):
    assistant, store, replier, meta = make(settings, replier=FakeReplier(reply="Sure, 50% off for you!", handover=True))
    res = assistant.process_payload("whatsapp", sample("whatsapp_text"))
    assert meta.calls[-1][2] == guardrails.HANDOVER_REPLY["whatsapp"] and "draft not sent" in res[0]["reason"]


def test_model_can_ask_for_a_human(settings):
    assistant, store, replier, meta = make(settings, replier=FakeReplier(reply="", handover=True))
    res = assistant.process_payload("whatsapp", sample("whatsapp_text"))
    assert res[0]["action"] == "handover"
    assert store.takeover_active("whatsapp", "15550001111")


@pytest.mark.parametrize("text,ok", [
    ("We're open Friday from 2pm, see you then!", True),
    ("It's $40 for a full set.", False),
    ("Prices start from 99 dirhams", False),
    ("The price is 45", False),
    ("Prices are on the menu in store and the team will confirm.", True),
    ("I promise it'll be ready by tomorrow", False),
    ("We guarantee no chips.", False),
    ("You can get 20% off this week", False),
    ("", False),
    ("x" * 400, False),
])
def test_outbound_check(text, ok):
    assert guardrails.check_outbound(text, "instagram_comment").ok is ok


def test_inbound_keywords_match_whole_words_only():
    hk, ck = ["agent", "refund"], ["suicide"]
    assert guardrails.check_inbound("Can I talk to an agent?", hk, ck).action == "handover"
    assert guardrails.check_inbound("I love your reagent-free polish", hk, ck).action == "reply"
    assert guardrails.check_inbound("thinking about suicide", hk, ck).action == "crisis"


def test_per_user_hourly_limit(settings):
    settings.per_user_hourly_limit = 2
    assistant, store, replier, meta = make(settings)
    for i in range(3):
        p = sample("whatsapp_text")
        p["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = f"wamid.{i}"
        res = assistant.process_payload("whatsapp", p)
    assert res[0]["action"] == "rate_limited"
    assert len(replier.calls) == 2


def test_daily_cap(settings):
    settings.daily_reply_cap = 1
    assistant, store, replier, meta = make(settings)
    assistant.process_payload("whatsapp", sample("whatsapp_text"))
    res = assistant.process_payload("whatsapp", sample("whatsapp_followup"))
    assert res[0]["action"] == "rate_limited" and "daily cap" in res[0]["reason"]


def test_whatsapp_allowlist(settings):
    settings.whatsapp_allowed_numbers = {"15559999999"}
    assistant, store, replier, meta = make(settings)
    res = assistant.process_payload("whatsapp", sample("whatsapp_text"))
    assert res[0]["action"] == "skipped" and "allowlist" in res[0]["reason"]
    assert meta.calls == [] and replier.calls == []


def test_non_text_gets_fixed_reply(settings):
    assistant, store, replier, meta = make(settings)
    res = assistant.process_payload("whatsapp", sample("whatsapp_image"))
    assert res[0]["action"] == "fixed_reply" and replier.calls == []
    assert meta.calls[-1][2] == guardrails.UNSUPPORTED_REPLY


def test_conversation_memory_across_messages(settings):
    assistant, store, replier, meta = make(settings)
    assistant.process_payload("whatsapp", sample("whatsapp_text"))
    p = sample("whatsapp_text")
    msg = p["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.update(id="wamid.second", text={"body": "Great, and is there parking?"})
    assistant.process_payload("whatsapp", p)
    history = replier.calls[1]["history"]
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert "pedicure" in history[0]["text"]


def test_ai_failure_sends_nothing_and_is_logged(settings):
    assistant, store, replier, meta = make(settings, replier=FakeReplier(fail=True))
    client = TestClient(create_app(settings, assistant))
    assert post(client, "whatsapp", sample("whatsapp_text"), SECRET).status_code == 200
    assert [c[0] for c in meta.calls] == ["mark_whatsapp_read"]
    assert store.all_logs()[-1]["action"] == "error"


def test_send_failure_keeps_reply_in_log(settings):
    assistant, store, replier, meta = make(settings, meta=RecordingMetaClient(fail_on={"send_instagram_dm"}))
    res = assistant.process_payload("instagram", sample("instagram_dm"))
    assert res[0]["sent"] is False and "simulated failure" in res[0]["error"]
    assert res[0]["reply"] == replier.reply


def test_dry_run_never_calls_meta(settings):
    settings.dry_run = True
    assistant, store, replier, meta = make(settings)
    assistant.process_payload("whatsapp", sample("whatsapp_text"))
    assistant.process_payload("instagram", sample("instagram_comment"))
    assert meta.calls == []
    assert all(r["reason"].startswith("[dry run]") for r in store.all_logs())


def test_reply_log_jsonl_written(tmp_path, settings):
    store = Store(str(tmp_path / "a.db"), str(tmp_path / "log.jsonl"))
    assistant = Assistant(settings, store, FakeReplier(), RecordingMetaClient())
    assistant.process_payload("instagram", sample("instagram_dm"))
    rows = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert rows[0]["action"] == "ai_reply" and rows[0]["channel"] == "instagram_dm"


# Claude and Graph API clients (mocked) ----------------------------------------

class FakeAnthropic:
    def __init__(self, text='{"reply": "Hi!", "handover": false, "reason": "greeting"}', stop="end_turn"):
        self.kwargs = None
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])

        self.beta = SimpleNamespace(messages=Messages())


def test_anthropic_replier_request_and_parse():
    fake = FakeAnthropic()
    draft = AnthropicReplier(client=fake).draft("whatsapp", [], "hello", "PROFILE")
    assert draft == Draft("Hi!", False, "greeting")
    kw = fake.kwargs
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "PROFILE" in kw["system"] and "<customer_message>" in kw["messages"][-1]["content"]


def test_anthropic_replier_refusal_becomes_handover():
    draft = AnthropicReplier(client=FakeAnthropic(stop="refusal", text="")).draft("whatsapp", [], "x", "")
    assert draft.handover and draft.reply == ""


def test_build_messages_never_starts_with_assistant():
    msgs = build_messages("instagram_dm", [{"role": "assistant", "text": "old"}, {"role": "user", "text": "hi"},
                                           {"role": "assistant", "text": "hello"}], "new")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


def test_meta_client_builds_correct_requests(settings):
    settings.whatsapp_phone_number_id = "100000000000002"
    settings.whatsapp_access_token = "wa-token"
    settings.instagram_access_token = "ig-token"
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = MetaClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.send_whatsapp_text("15550001111", "hi", reply_to="wamid.X")
    client.reply_to_comment("18100000000000001", "thanks")
    client.send_instagram_dm("1234567890000001", "hello")
    wa, comment, dm = seen
    assert str(wa.url) == "https://graph.facebook.com/v26.0/100000000000002/messages"
    assert wa.headers["Authorization"] == "Bearer wa-token"
    body = json.loads(wa.content)
    assert body["text"]["body"] == "hi" and body["context"]["message_id"] == "wamid.X"
    assert str(comment.url) == "https://graph.instagram.com/v26.0/18100000000000001/replies"
    assert json.loads(dm.content) == {"recipient": {"id": "1234567890000001"}, "message": {"text": "hello"}}


def test_meta_client_error_is_readable_and_has_no_token(settings):
    settings.whatsapp_access_token = "secret-token-value"

    def handler(request):
        return httpx.Response(400, json={"error": {"code": 131030, "type": "OAuthException",
                                                   "message": "Recipient phone number not in allowed list"}})

    client = MetaClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(MetaAPIError) as exc:
        client.send_whatsapp_text("15550001111", "hi")
    assert "131030" in str(exc.value) and "secret-token-value" not in str(exc.value)


# Lead Ads ------------------------------------------------------------------

LEAD = {"id": "300000000000003", "created_time": "2026-11-04T10:00:00+0000", "form_id": "200000000000002",
        "field_data": [{"name": "full_name", "values": ["Test Lead"]},
                       {"name": "email", "values": ["test@example.com"]}]}


def test_parse_leadgen_ignores_other_page_fields():
    payload = sample("page_leadgen")
    payload["entry"][0]["changes"].append({"field": "feed", "value": {}})
    from meta_assistant.handlers import parse_leadgen
    leads, skipped = parse_leadgen(payload)
    assert [l["leadgen_id"] for l in leads] == ["300000000000003"]
    assert skipped == ["ignored page field feed"]


def test_lead_is_fetched_stored_and_deduped(settings):
    meta = RecordingMetaClient(leads={"300000000000003": LEAD})
    a, store, replier, _ = make(settings, meta=meta)
    first = a.process_payload("leads", sample("page_leadgen"))
    again = a.process_payload("leads", sample("page_leadgen"))
    assert first[0]["status"] == "captured"
    assert first[0]["fields"] == {"full_name": "Test Lead", "email": "test@example.com"}
    assert again[0]["status"] == "duplicate"
    assert [c[0] for c in meta.calls] == ["get_lead"]
    assert replier.calls == []          # leads never go to the model
    assert store.all_leads()[0]["fields"]["email"] == "test@example.com"


def test_lead_fetch_failure_is_kept_not_raised(settings):
    a, store, _, _ = make(settings, meta=RecordingMetaClient(fail_on={"get_lead"}))
    out = a.process_payload("leads", sample("page_leadgen"))
    assert out[0]["status"] == "fetch_failed" and "simulated failure" in out[0]["error"]
    assert store.all_leads()[0]["leadgen_id"] == "300000000000003"


def test_lead_webhook_signature_and_object(settings):
    meta = RecordingMetaClient(leads={"300000000000003": LEAD})
    a, store, _, _ = make(settings, meta=meta)
    client = TestClient(create_app(settings, a))
    raw = json.dumps(sample("page_leadgen")).encode()
    assert client.post("/webhooks/leads", content=raw, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403
    ok = client.post("/webhooks/leads", content=raw, headers={"X-Hub-Signature-256": compute_signature(SECRET, raw)})
    assert ok.status_code == 200
    assert store.all_leads()[0]["status"] == "captured"
    assert client.get("/webhooks/leads", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                  "hub.challenge": "42"}).text == "42"


def test_meta_client_get_lead_uses_page_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["fields"] = request.url.params["fields"]
        return httpx.Response(200, json=LEAD)

    s = Settings(page_access_token="page-token")
    client = MetaClient(s, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get_lead("300000000000003")["form_id"] == "200000000000002"
    assert seen["auth"] == "Bearer page-token" and "field_data" in seen["fields"]
