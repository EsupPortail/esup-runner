from datetime import datetime, timedelta

import pytest
from task_routes_helpers import clean_state, make_runner, make_task

from app.__version__ import __version__
from app.core.auth import api_token_role
from app.core.config import ConfigValidationError, config
from app.core.runner_store import RunnerStore
from app.core.state import runners, tasks
from app.models.live import LiveInfo
from scripts.generate_token import ask_token_label

__all__ = ["clean_state"]


@pytest.fixture(autouse=True)
def isolate_route_registries(monkeypatch):
    from app.api.routes import runner, task

    monkeypatch.setattr(runner, "runners", runners)
    monkeypatch.setattr(task, "runners", runners)


def headers(token):
    return {"Authorization": f"Bearer {token}", "X-Runner-Version": __version__}


def test_application_and_runner_roles(client, clean_state):
    app_headers = headers("test-token")
    runner_headers = headers("tok")
    registration = {"id": "r1", "url": "http://r1.example", "task_types": ["encoding"]}

    assert (
        client.post("/runner/register", headers=app_headers, json=registration).status_code == 403
    )
    assert (
        client.post("/runner/register", headers=runner_headers, json=registration).status_code
        == 200
    )
    assert client.post("/runner/heartbeat/r1", headers=app_headers).status_code == 403
    assert client.post("/runner/heartbeat/r1", headers=runner_headers).status_code == 200
    assert (
        client.post(
            "/task/completion", headers=app_headers, json={"task_id": "t1", "status": "completed"}
        ).status_code
        == 403
    )

    request = {
        "etab_name": "UM",
        "app_name": "pod",
        "task_type": "encoding",
        "source_url": "https://example.com/video.mp4",
        "notify_url": "",
    }
    assert client.post("/task/execute", headers=runner_headers, json=request).status_code == 403
    assert client.get("/task/list", headers=runner_headers).status_code == 403
    assert client.get("/api/tasks", headers=runner_headers).status_code == 403


def test_application_only_accesses_its_own_tasks(client, clean_state):
    tasks["owned"] = make_task("owned", "r1", status="running")
    tasks["other"] = make_task("other", "r1", status="failed")
    tasks["other"].client_token = "pod-token"
    tasks["unowned"] = make_task("unowned", "r1", status="running")
    tasks["unowned"].client_token = None
    app_headers = headers("test-token")

    assert set(client.get("/task/list", headers=app_headers).json()) == {"owned"}
    assert [
        task["id"] for task in client.get("/api/tasks", headers=app_headers).json()["tasks"]
    ] == ["owned"]
    response = client.get("/task/status/owned", headers=app_headers)
    assert response.status_code == 200
    assert "client_token" not in response.json()
    for task_id in ("other", "unowned"):
        for url in (
            f"/task/status/{task_id}",
            f"/task/result/{task_id}",
            f"/task/result/{task_id}/file/output.mp4",
        ):
            response = client.get(url, headers=app_headers)
            assert response.status_code == 404
            assert response.json() == {"detail": "Task not found"}
        assert client.post(f"/task/stop/{task_id}", headers=app_headers).status_code == 404
    assert client.get("/task/status/other", headers=headers("pod-token")).status_code == 200


def test_runner_status_preserves_recovery_and_pod_metadata(client, clean_state):
    runners["r1"] = make_runner("r1", token="tok")
    runners["r2"] = make_runner("r2", token="tok-bad")
    tasks["own"] = make_task("own", "r1", status="failed")
    tasks["own"].run_id = "run-1"
    tasks["other"] = make_task("other", "r2", status="running")
    tasks["live"] = make_task("live", "r2", status="running")
    tasks["live"].live = LiveInfo(source_id="room", event_id="event", automatic=True)
    runner_headers = headers("tok")

    status = client.get("/task/status/own", headers=runner_headers)
    assert status.json() == {
        "task_id": "own",
        "run_id": "run-1",
        "acknowledged_at": None,
        "etab_name": "UM",
    }
    assert client.get("/task/status/other", headers=runner_headers).status_code == 404
    assert client.get("/task/status/live", headers=runner_headers).json() == {"etab_name": "UM"}
    assert client.get("/task/result/own", headers=runner_headers).status_code == 403


@pytest.mark.parametrize("found_on_retry", [False, True])
def test_deduplication_uses_only_the_submitting_token(
    client, clean_state, monkeypatch, found_on_retry
):
    from app.api.routes import task as routes

    existing = make_task("existing", "r1", status="running")
    existing.notify_url = ""
    request = {
        "etab_name": existing.etab_name,
        "app_name": existing.app_name,
        "task_type": existing.task_type,
        "source_url": existing.source_url,
        "notify_url": "",
    }
    monkeypatch.setattr(config, "PRIORITIES_ENABLED", False)

    def submit(token):
        matches = {existing.task_id: existing}
        snapshots = iter(({} if found_on_retry else matches, matches))
        monkeypatch.setattr(routes, "get_tasks_snapshot", lambda: next(snapshots))
        return client.post("/task/execute", headers=headers(token), json=request)

    assert submit("test-token").json() == {"task_id": "existing", "status": "running"}
    assert submit("pod-token").status_code == 503


@pytest.mark.parametrize("shared", [False, True])
def test_expired_runner_remains_bound_to_its_token(tmp_path, shared):
    path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=shared, state_file=str(path))
    first = make_runner("r1", token="tok")
    assert store.register(first)
    now = datetime.now()
    assert not store.expire_inactive("r1", now - timedelta(minutes=1))
    assert store.expire_inactive("r1", now)
    if shared:
        store = RunnerStore(shared_enabled=True, state_file=str(path))
    assert "r1" not in store
    assert not store.register(first.model_copy(update={"token": "tok-bad"}))
    assert store.register(first)


def test_registry_excludes_old_application_registrations(tmp_path):
    path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=True, state_file=str(path))
    assert store.register(make_runner("old", token="test-token"))
    assert store.register(make_runner("worker", token="tok"))

    secured = RunnerStore(
        shared_enabled=True,
        state_file=str(path),
        is_token_allowed=lambda token: api_token_role(token) == "runners",
    )
    assert list(secured) == ["worker"]
    assert secured.get("old") is None
    assert secured.try_reserve("old") is None
    assert secured.try_reserve("worker") is not None


def test_config_rejects_unknown_labels_and_secrets_shared_between_roles(monkeypatch):
    monkeypatch.setattr(
        config,
        "AUTHORIZED_TOKENS",
        {"runners": "shared", "app": "shared", "legacy": "legacy-token"},
    )
    with pytest.raises(ConfigValidationError) as error:
        config._validate_authorized_tokens()
    assert "Invalid token variable name 'AUTHORIZED_TOKENS__legacy'" in str(error.value)
    assert "API tokens must use distinct values" in str(error.value)
    assert api_token_role("shared") is None


@pytest.mark.parametrize("label", ["runners", "app", "runners_gpu_um", "app_pod_um"])
def test_token_generator_prompts_for_role_prefix(monkeypatch, capsys, label):
    entries = iter(("legacy", "app.invalid", label))
    monkeypatch.setattr("builtins.input", lambda _: next(entries))
    assert ask_token_label() == label
    assert capsys.readouterr().out.count("Start with app or runners") == 2
