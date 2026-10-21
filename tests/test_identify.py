"""Reconnaissance d'une photo par Claude (client simulé, pas d'appel réseau)."""
import json
from types import SimpleNamespace

import pytest

import app as sleevr

JPEG = b"\xff\xd8\xff" + b"0" * 200


class FakeMessages:
    def __init__(self, reply, stop="end_turn"):
        self.reply, self.stop, self.kwargs = reply, stop, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=json.dumps(self.reply))])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sleevr, "DB_PATH", str(tmp_path / "cartes.db"))
    monkeypatch.setattr(sleevr, "SIGNUP_CODE", "")
    sleevr.init_db()
    c = sleevr.app.test_client()
    c.post("/api/register", json={"username": "alex", "password": "motdepasse"})
    return c


def fake_ai(monkeypatch, reply, stop="end_turn"):
    msgs = FakeMessages(reply, stop)
    monkeypatch.setattr(sleevr, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(sleevr, "_ai_client", SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    return msgs


def test_disabled_without_key(client, monkeypatch):
    monkeypatch.setattr(sleevr, "ANTHROPIC_API_KEY", "")
    assert client.post("/api/identify", data=JPEG).status_code == 404
    assert client.get("/api/session").get_json()["ai"] is False


def test_identifies_card(client, monkeypatch):
    msgs = fake_ai(monkeypatch, {"game": "onepiece", "name": "Monkey.D.Luffy ", "number": "OP05-119", "confidence": "high"})
    r = client.post("/api/identify", data=JPEG)
    assert r.get_json() == {"game": "onepiece", "name": "Monkey.D.Luffy", "number": "OP05-119", "confidence": "high"}
    assert msgs.kwargs["model"] == "claude-opus-5-5"
    assert msgs.kwargs["fallbacks"] == "default"
    assert msgs.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_refusal_is_an_error(client, monkeypatch):
    fake_ai(monkeypatch, {}, stop="refusal")
    assert client.post("/api/identify", data=JPEG).status_code == 502


def test_rejects_non_jpeg(client, monkeypatch):
    fake_ai(monkeypatch, {})
    assert client.post("/api/identify", data=b"pas une photo" * 20).status_code == 415
