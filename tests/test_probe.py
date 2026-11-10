"""The auto probe: the models.json entry, the endpoint, and the number the provider reports."""

from __future__ import annotations

import io
import json
import urllib.request

import pytest

from mech_council import probe

MODEL = '{"providers": {"omnissiah": {"baseUrl": "http://provider.test:8080/v1", "apiKey": "none", "models": [{"id": "m"}]}}}'


@pytest.fixture
def ground(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".pi").mkdir(parents=True)
    agent = tmp_path / "agent"
    agent.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    return project, agent


def _answer(monkeypatch, payload, *, error=None):
    """Stub urlopen: serve a JSON body, or raise."""

    def fake_urlopen(request, timeout=None):
        if error is not None:
            raise error
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_the_probe_reads_the_limit_the_provider_reports(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text(MODEL)
    _answer(monkeypatch, {"server": {"max_num_seqs": 8}})
    assert probe.probe("omnissiah") == (8, None)


def test_a_projects_models_json_is_not_pi_s_so_the_probe_ignores_it(ground, monkeypatch):
    project, agent = ground
    (agent / "models.json").write_text(MODEL)
    (project / ".pi" / "models.json").write_text(MODEL.replace("provider.test:8080", "provider.test:9090"))
    _answer(monkeypatch, {"server": {"max_num_seqs": 4}})
    assert probe.probe("omnissiah") == (4, None)
    entry = probe.provider_entry("omnissiah")
    assert entry is not None and entry["baseUrl"].endswith("8080/v1"), "pi reads only the agent dir's models.json"


def test_a_provider_that_answers_but_reports_no_limit_is_untagged(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text(MODEL)
    _answer(monkeypatch, {"server": {"model": "qwen35", "uptime_s": 90}})
    _limit, reason = probe.probe("omnissiah")
    assert _limit is None
    assert reason is not None and "reports no recognized limit" in reason


def test_a_provider_that_answers_nothing_is_unreachable(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text(MODEL)
    _answer(monkeypatch, None, error=OSError("connection refused"))
    _limit, reason = probe.probe("omnissiah")
    assert _limit is None
    assert reason is not None and "no limit report" in reason


def test_a_builtin_provider_has_no_entry_and_no_endpoint(ground, monkeypatch):
    _, _agent = ground
    _answer(monkeypatch, {"server": {"max_num_seqs": 8}})
    _limit, reason = probe.probe("openai")
    assert _limit is None
    assert reason is not None and "no models.json entry" in reason


def test_an_unparseable_models_json_is_said_to_be_unparseable(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text('{"providers": {"omnissiah": {"baseUrl": "http://provider.test:8080/v1"},},}')
    _limit, reason = probe.probe("omnissiah")
    assert _limit is None
    assert reason is not None and "does not parse" in reason
    assert "no models.json entry" not in reason, "a broken file is not a missing entry"


def test_a_models_json_that_parses_to_a_list_is_said_to_be_malformed(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text("[1, 2]")
    _limit, reason = probe.probe("omnissiah")
    assert _limit is None
    assert reason is not None and "is malformed" in reason
    assert "no models.json entry" not in reason, "a broken file is not a missing entry"


def test_a_models_json_whose_providers_is_not_an_object_is_said_to_be_malformed(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text('{"providers": [1]}')
    _limit, reason = probe.probe("omnissiah")
    assert _limit is None
    assert reason is not None and "is malformed" in reason
    assert "no models.json entry" not in reason, "a broken file is not a missing entry"


def test_a_models_json_without_providers_lacks_the_entry_rather_than_being_malformed(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text("{}")
    _limit, reason = probe.probe("omnissiah")
    assert _limit is None
    assert reason is not None and "no models.json entry" in reason


def test_an_entry_without_a_base_url_has_no_endpoint(ground, monkeypatch):
    _, agent = ground
    (agent / "models.json").write_text('{"providers": {"local": {"models": [{"id": "m"}]}}}')
    _limit, reason = probe.probe("local")
    assert _limit is None
    assert reason is not None and "no baseUrl" in reason
