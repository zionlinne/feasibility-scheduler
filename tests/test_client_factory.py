"""create_client / Protocol 契约测试（不触网）。"""

from __future__ import annotations

import pytest

from feasibility_scheduler.client import SchedulerClient, create_client
from feasibility_scheduler.fake_client import FakeClient, Step
from feasibility_scheduler.http_client import HttpClient


def test_create_client_defaults_to_fake(monkeypatch):
    monkeypatch.delenv("FEASIBILITY_CLIENT", raising=False)
    client = create_client()
    assert isinstance(client, FakeClient)
    assert isinstance(client, SchedulerClient)


def test_create_client_env_http(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_CLIENT", "http")
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-test-not-real")
    client = create_client()
    assert isinstance(client, HttpClient)
    assert isinstance(client, SchedulerClient)
    assert client.provider == "88api"


def test_create_client_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_CLIENT", "http")
    client = create_client("fake", script=[Step.success()])
    assert isinstance(client, FakeClient)


def test_create_client_unknown_raises():
    with pytest.raises(ValueError, match="未知"):
        create_client("nope")
