import pytest
import requests

from etsy_auto_print.notify import Notifier


def test_send_with_no_channels_configured_only_logs():
    Notifier().send("title", "message")  # must not raise


def test_send_hits_ntfy_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)))
    Notifier(ntfy_url="https://ntfy.sh/topic").send("Held", "reason")
    assert len(calls) == 1
    url, kw = calls[0]
    assert url == "https://ntfy.sh/topic"
    assert kw["headers"]["Title"] == "Held"
    assert kw["data"] == b"reason"


def test_send_hits_pushover_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)))
    Notifier(pushover_user_key="u", pushover_api_token="t").send("Held", "reason")
    assert len(calls) == 1
    url, kw = calls[0]
    assert url == "https://api.pushover.net/1/messages.json"
    assert kw["data"]["user"] == "u"
    assert kw["data"]["token"] == "t"
    assert kw["data"]["title"] == "Held"
    assert kw["data"]["message"] == "reason"


def test_send_hits_both_channels_when_both_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append(url))
    Notifier(ntfy_url="https://ntfy.sh/topic", pushover_user_key="u", pushover_api_token="t").send(
        "Held", "reason"
    )
    assert calls == ["https://ntfy.sh/topic", "https://api.pushover.net/1/messages.json"]


def test_pushover_requires_both_key_and_token(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append(url))
    Notifier(pushover_user_key="u", pushover_api_token=None).send("Held", "reason")
    assert calls == []


def test_delivery_failure_is_swallowed_not_raised(monkeypatch):
    def boom(url, **kw):
        raise requests.RequestException("network down")

    monkeypatch.setattr(requests, "post", boom)
    Notifier(ntfy_url="https://ntfy.sh/topic").send("title", "message")  # must not raise
