import pytest

from yerk_ai.config import table
from yerk_ai.lifted.classify import FailureTable

T = FailureTable.load(table("failures.toml"))

# Claude Code's error types, as listed for the StopFailure matcher and the assistant `error` field.
CC_TYPES = ["rate_limit", "overloaded", "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
            "billing_error", "invalid_request", "model_not_found", "server_error", "max_output_tokens",
            "cloud_credential_error", "unknown"]


def test_every_claude_code_type_is_in_the_table():
    assert sorted(T.types) == sorted(CC_TYPES)


def test_every_class_has_an_action():
    assert set(T.types.values()) | {c for c, _, _ in T.patterns} <= set(T.actions)


@pytest.mark.parametrize("etype,cls,action,kind", [
    ("rate_limit", "quota", "hop_account", "rate_limit"),
    ("billing_error", "quota", "hop_account", "billing"),
    ("authentication_failed", "auth", "hop_account", "auth"),
    ("oauth_org_not_allowed", "auth", "hop_account", "auth"),
    ("account_on_hold", "auth", "hop_account", "auth"),
    ("cloud_credential_error", "auth", "hop_account", "auth"),
    ("overloaded", "transient", "rerun", None),
    ("server_error", "transient", "rerun", None),
    ("invalid_request", "config", "stop", None),
    ("model_not_found", "config", "stop", None),
    ("max_output_tokens", "unknown", "stop", None),
])
def test_by_type(etype, cls, action, kind):
    f = T.classify(etype, "")
    assert (f.cls, f.action, f.kind) == (cls, action, kind)


@pytest.mark.parametrize("text,cls", [
    ("Prompt is too long", "overflow"),
    ("input length and max_tokens exceed context limit", "overflow"),
    ("You've hit your session limit, resets 3pm", "quota"),
    ("Claude usage limit reached. Your limit will reset at 5pm", "quota"),
    ("You're out of usage credits", "quota"),
    ("Credit balance is too low", "quota"),
    ("Your subscription is disabled", "auth"),
    ("OAuth token has expired. Please run /login", "auth"),
    ("Invalid credentials (401)", "auth"),
    ("Error: 529 overloaded_error", "transient"),
    ("Connection reset by peer", "transient"),
    ("Request timed out", "transient"),
    ("No conversation found with session ID: abc", "config"),
    ("model_not_found: claude-x", "config"),
    ("something nobody has seen before", "unknown"),
])
def test_by_text(text, cls):
    assert T.classify("unknown", text).cls == cls


def test_known_type_beats_text():
    assert T.classify("overloaded", "Prompt is too long").cls == "transient"


def test_unlisted_type_falls_through_to_text():
    assert T.classify("some_new_type", "rate limit exceeded").cls == "quota"


def test_cooldown_doubles_and_caps():
    assert [T.cooldown_seconds("rate_limit", r) for r in range(4)] == [30, 60, 120, 240]
    assert T.cooldown_seconds("billing", 0) == 600
    assert T.cooldown_seconds("billing", 30) == 86400
