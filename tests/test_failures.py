"""Checks the failure table in failures.toml, which says how Yerk AI reads a failed turn, a test per string: Claude Code's error types,
its stream events, and the error messages its error reference documents. Those messages are CC:19400-21300 in the docs mirror."""
import pytest

from yerk_ai.config import table
from yerk_ai.lifted.classify import FailureTable

T = FailureTable.load(table("failures.toml"))
CLASSES = ("transient", "overflow", "auth", "quota", "refusal", "fatal")
ACTIONS = ("rerun", "compact_then_rerun", "hop_account", "fresh_next_turn", "fresh_with_recap", "stop")

# Claude Code's error types, as listed for the StopFailure matcher and the assistant `error` field.
CC_TYPES = ["rate_limit", "overloaded", "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
            "billing_error", "invalid_request", "model_not_found", "server_error", "max_output_tokens",
            "cloud_credential_error", "unknown"]


def test_every_claude_code_type_is_in_the_table():
    assert sorted(T.types) == sorted(CC_TYPES)


def test_classes_and_actions_are_the_known_ones():
    entries = [*T.types.values(), *T.events.values()]
    assert set(T.actions) == set(CLASSES) and set(T.actions.values()) <= set(ACTIONS)
    assert {e["class"] for e in entries} | {c for _, c, _, _, _ in T.patterns} <= set(CLASSES)
    assert {a for *_, a, _ in T.patterns if a} <= set(ACTIONS)


@pytest.mark.parametrize("etype,cls,action,kind", [
    ("rate_limit", "quota", "hop_account", "rate_limit"),
    ("billing_error", "quota", "hop_account", "billing"),
    ("authentication_failed", "auth", "hop_account", "auth"),
    ("oauth_org_not_allowed", "auth", "hop_account", "auth"),
    ("account_on_hold", "auth", "hop_account", "auth"),
    ("cloud_credential_error", "auth", "hop_account", "auth"),
    ("overloaded", "transient", "rerun", None),
    ("server_error", "transient", "rerun", None),
    ("invalid_request", "fatal", "stop", None),
    ("model_not_found", "fatal", "stop", None),
    ("max_output_tokens", "fatal", "stop", None),
    ("unknown", "fatal", "stop", None),
])
def test_by_type(etype, cls, action, kind):
    f = T.classify(etype, "")
    assert (f.cls, f.action, f.kind, f.source) == (cls, action, kind, f"type:{etype}")


@pytest.mark.parametrize("event,cls,action,kind", [
    ("model_refusal_fallback", "refusal", "fresh_next_turn", None),
    ("rate_limit_rejected", "quota", "hop_account", "rate_limit"),
    ("credits_required", "quota", "hop_account", "billing"),
])
def test_by_event(event, cls, action, kind):
    f = T.classify(None, "", [event])
    assert (f.cls, f.action, f.kind, f.source) == (cls, action, kind, f"event:{event}")
    assert T.event(event) == f


@pytest.mark.parametrize("text,cls", [
    # server errors
    ("API Error: 500 Internal server error", "transient"),
    ("API Error: Repeated 529 Overloaded errors", "transient"),
    ("Request timed out", "transient"),
    ("No response from API", "transient"),
    ("The response above may be incomplete", "transient"),
    ("Agent terminated early due to an API error", "transient"),
    ("Error: 529 overloaded_error", "transient"),
    # usage limits
    ("You've hit your session limit", "quota"),
    ("You've hit your session limit, resets 3pm", "quota"),
    ("Claude usage limit reached. Your limit will reset at 5pm", "quota"),
    ("Usage credits required for 1M context", "quota"),
    ("Server is temporarily limiting requests", "transient"),
    ("Request rejected (429)", "quota"),
    ("You've hit your monthly spend limit", "quota"),
    ("Spend limit reached", "quota"),
    ("Credit balance is too low", "quota"),
    ("You're out of usage credits", "quota"),
    # authentication
    ("Not logged in · Please run /login", "auth"),
    ("Invalid API key · Please run /login", "auth"),
    ("Could not resolve authentication method", "auth"),
    ("This organization has been disabled", "auth"),
    ("Your organization has disabled Claude subscription access", "auth"),
    ("OAuth token revoked or expired", "auth"),
    ("OAuth token has expired. Please run /login", "auth"),
    ("API Error: 401 Invalid authentication credentials", "auth"),
    ("Login expired", "auth"),
    ("Claude login not accepted", "auth"),
    ("Your account is on hold", "auth"),
    ("claude.ai rejected the session token", "auth"),
    ("Invalid auth token", "auth"),
    ("Your account does not have access to Claude. Please login again", "auth"),
    ("Your subscription is disabled", "auth"),
    ("Invalid credentials (401)", "auth"),
    ("Could not refresh your login because another Claude Code process is refreshing it", "transient"),
    # network
    ("Unable to connect to API", "transient"),
    ("Unable to connect to Anthropic services", "transient"),
    ("Socket is closed", "transient"),
    ("API returned an empty or malformed response", "transient"),
    ("Streaming response ended before any complete data was received", "transient"),
    ("Connection reset by peer", "transient"),
    # request errors
    ("Prompt is too long", "overflow"),
    ("Context exceeds the token limit", "overflow"),
    ("Request too large", "overflow"),
    ("input length and max_tokens exceed context limit", "overflow"),
    ("Image was too large", "fatal"),
    ("Extra inputs are not permitted", "fatal"),
    ("Tool input schema is invalid", "fatal"),
    ("There's an issue with the selected model", "fatal"),
    ("Model is not a recognized model id", "fatal"),
    ("model_not_found: claude-x", "fatal"),
    ("No conversation found with session ID: abc", "fatal"),
    ("something nobody has seen before", "fatal"),
])
def test_by_text(text, cls):
    assert T.classify("unknown", text).cls == cls


def test_a_missing_session_starts_fresh_with_a_recap():
    f = T.classify(None, "No conversation found with session ID: 0f3c")
    assert (f.cls, f.action, f.source) == ("fatal", "fresh_with_recap", "pattern:session_missing")


def test_invalid_request_lets_the_text_speak_first():
    assert T.classify("invalid_request", "Prompt is too long").cls == "overflow"
    assert T.classify("invalid_request", "Extra inputs are not permitted").source == "pattern:request"
    assert T.classify("invalid_request", "").source == "type:invalid_request"


def test_known_type_beats_events_and_text():
    assert T.classify("overloaded", "Prompt is too long", ["rate_limit_rejected"]).cls == "transient"


def test_unlisted_type_falls_through_to_text_then_fatal():
    assert T.classify("some_new_type", "rate limit exceeded").cls == "quota"
    assert T.classify("some_new_type", "").source == "none"


def test_cooldown_doubles_and_caps():
    assert [T.cooldown_seconds("rate_limit", r) for r in range(4)] == [30, 60, 120, 240]
    assert T.cooldown_seconds("billing", 0) == 600
    assert T.cooldown_seconds("billing", 30) == 86400


def test_reruns_are_capped():
    assert [T.rerun_wait(n) for n in range(3)] == [2.0, 10.0, None]


# Read live from Claude Code 2.1.301 in the test room (Layer 2 run 1): a garbage token, and an
# empty config folder with no login, both under error type authentication_failed.
@pytest.mark.parametrize("text", ["Failed to authenticate. API Error: 401 Invalid bearer token",
                                  "Not logged in · Please run /login"])
def test_live_auth_strings(text):
    assert T.classify("authentication_failed", text).action == "hop_account"
    assert T.classify("unknown", text).cls == "auth"
