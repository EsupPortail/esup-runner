"""Regression coverage for administration CSRF protection with real Basic auth."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from task_routes_helpers import make_task

from app.api.routes import admin, task
from app.core import auth, csrf
from app.core.config import config
from app.core.passwords import BcryptPasswordContext
from app.core.url_paths import RootPathProxyCompatibilityMiddleware

_ADMIN_FORM = {
    "admin_name": "new-admin",
    "admin_password": "new-password",
    "admin_password_confirm": "new-password",
}
_MUTATIONS = [
    ("/admin/credentials/admins", "data", _ADMIN_FORM),
    ("/admin/credentials/admins/admin/delete", "data", {}),
    ("/admin/credentials/tokens", "data", {"token_name": "new_token"}),
    ("/admin/credentials/tokens/client/delete", "data", {}),
    ("/admin/reload-config", "data", {}),
    ("/admin/toggle-theme", "data", {}),
    ("/tasks/delete-selected", "json", {"task_ids": ["t1"]}),
    ("/tasks/restart-selected", "json", {"task_ids": ["t1"]}),
    ("/tasks/stop-selected", "json", {"task_ids": ["t1"]}),
]


def _request(*, method="POST", authorization="Basic test", **headers):
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in {"authorization": authorization, **headers}.items()
            ],
        }
    )


def _app(root_path=""):
    application = FastAPI(root_path=root_path)
    application.include_router(admin.router)
    application.include_router(task.router)
    application.add_middleware(RootPathProxyCompatibilityMiddleware)
    return application


def _page_token(client, path="/admin/credentials"):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', response.text)
    assert match is not None
    for form in re.findall(r'<form\b[^>]*method="post".*?</form>', response.text, re.DOTALL):
        assert f'name="csrf_token" value="{match.group(1)}"' in form
    return match.group(1)


@pytest.fixture
def csrf_client(monkeypatch):
    password_context = BcryptPasswordContext(rounds=4)
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": password_context.hash("test-password")})
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"client": "test-api-token"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "")
    monkeypatch.setattr(config, "MANAGER_PUBLIC_URL", "https://manager.example")
    monkeypatch.setattr(auth, "_refresh_config_if_needed", lambda: None)
    monkeypatch.setattr(admin, "_PASSWORD_CONTEXT", password_context)
    effects = []

    def record(name, result=True):
        def operation(*_args, **_kwargs):
            effects.append(name)
            return result

        return operation

    for name in (
        "_upsert_admin_user_in_env",
        "_delete_admin_user_from_env",
        "_upsert_authorized_token_in_env",
        "_delete_authorized_token_from_env",
    ):
        monkeypatch.setattr(admin, name, record(name))
    monkeypatch.setattr(admin.config_module, "reload_config_env", record("reload", config))
    monkeypatch.setattr(admin.config_module, "publish_config_reload_event", record("publish"))
    monkeypatch.setattr(
        task, "get_task_from_state", record("lookup", make_task("t1", "r1", status="failed"))
    )
    monkeypatch.setattr(task, "delete_task_from_state", record("delete"))

    async def queue_task(*_args, **_kwargs):
        effects.append("restart")
        return {"task_id": "t1"}

    async def stop_task(*_args, **_kwargs):
        effects.append("stop")
        return "stopped", {"task_id": "t1"}

    monkeypatch.setattr(task, "_queue_task_execution", queue_task)
    monkeypatch.setattr(task, "_stop_selected_task", stop_task)
    with TestClient(_app(), base_url="http://internal:8081") as client:
        client.auth = ("admin", "test-password")
        yield client, effects


@pytest.mark.parametrize("path,body_type,payload", _MUTATIONS)
@pytest.mark.parametrize(
    "failure", ["foreign_origin", "missing_origin", "missing_token", "bad_token"]
)
def test_admin_mutations_reject_csrf_without_side_effects(
    csrf_client, path, body_type, payload, failure
):
    client, effects = csrf_client
    headers = {"Origin": "https://manager.example", "X-CSRF-Token": _page_token(client)}
    if failure == "foreign_origin":
        headers.update({"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"})
    elif failure == "missing_origin":
        del headers["Origin"]
    elif failure == "missing_token":
        del headers["X-CSRF-Token"]
    else:
        headers["X-CSRF-Token"] = "invalid"

    response = client.post(path, headers=headers, follow_redirects=False, **{body_type: payload})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert effects == []


@pytest.mark.parametrize("path,body_type,payload", _MUTATIONS)
def test_admin_mutations_accept_same_origin_and_page_token(csrf_client, path, body_type, payload):
    client, effects = csrf_client
    token = _page_token(client)
    headers = {"Origin": "https://manager.example", "Sec-Fetch-Site": "same-origin"}
    if body_type == "data":
        payload = {**payload, "csrf_token": token}
    else:
        headers["X-CSRF-Token"] = token
    response = client.post(path, headers=headers, follow_redirects=False, **{body_type: payload})
    assert response.status_code == (
        303 if path.startswith("/admin/") and path != "/admin/reload-config" else 200
    )
    if path == "/admin/toggle-theme":
        assert "theme=dark" in response.headers["set-cookie"]
    else:
        assert effects


def test_audit_cross_origin_admin_creation_is_rejected(csrf_client):
    client, effects = csrf_client
    response = client.post(
        "/admin/credentials/admins",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        data=_ADMIN_FORM,
    )
    assert response.status_code == 403
    assert effects == []


@pytest.mark.parametrize(
    "headers,expected_status",
    [
        ({"Referer": "https://manager.example/manager/admin/credentials?tab=1"}, 303),
        ({"Origin": "https://MANAGER.example:443"}, 303),
        ({"Origin": "https://manager.example", "Referer": "https://attacker.example"}, 303),
        ({"Origin": "null", "Referer": "https://manager.example/admin"}, 403),
        ({"Origin": "", "Referer": "https://manager.example/admin"}, 403),
        ({"Referer": "https://manager.example.attacker.example/admin"}, 403),
        ({"Origin": "https://sub.manager.example", "Sec-Fetch-Site": "same-site"}, 403),
        ({"Origin": "http://manager.example"}, 403),
        ({"Origin": "https://manager.example:444"}, 403),
        ({"Origin": "https://manager.example", "Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "https://attacker.example", "Host": "attacker.example"}, 403),
        (
            {
                "Origin": "https://attacker.example",
                "X-Forwarded-Host": "attacker.example",
                "X-Forwarded-Proto": "https",
            },
            403,
        ),
    ],
)
def test_origin_uses_public_configuration_and_referer_only_as_fallback(
    csrf_client, headers, expected_status
):
    client, effects = csrf_client
    response = client.post(
        "/admin/credentials/admins",
        headers=headers,
        data={**_ADMIN_FORM, "csrf_token": _page_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == expected_status
    assert bool(effects) is (expected_status == 303)


@pytest.mark.parametrize(
    "root_path,headers,expected_url",
    [
        ("", {"Origin": "https://manager.example"}, "https://manager.example"),
        (
            "/manager",
            {
                "Origin": "https://manager.example:8443",
                "Referer": "https://other.example/admin?private-query=secret",
                "X-Forwarded-Host": "other.example",
                "X-Forwarded-Proto": "http",
            },
            "https://manager.example:8443/manager",
        ),
        (
            "/manager",
            {
                "Referer": "https://manager.example/manager/admin/task/t1?private-query=secret#details"
            },
            "https://manager.example/manager",
        ),
        (
            "/gestion été",
            {"Origin": "https://[2001:db8::1]:8443"},
            "https://[2001:db8::1]:8443/gestion%20%C3%A9t%C3%A9",
        ),
    ],
)
def test_origin_error_suggests_public_url_without_deleting_task(
    csrf_client, monkeypatch, root_path, headers, expected_url
):
    _, effects = csrf_client
    monkeypatch.setattr(config, "MANAGER_PUBLIC_URL", "http://internal:8081")
    with TestClient(_app(root_path), base_url="http://internal:8081") as client:
        client.auth = ("admin", "test-password")
        response = client.post(
            "/tasks/delete-selected",
            headers={**headers, "X-CSRF-Token": _page_token(client)},
            json={"task_ids": ["t1"]},
        )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "Configure MANAGER_PUBLIC_URL in manager/.env" in detail
    assert f'Suggested value: MANAGER_PUBLIC_URL="{expected_url}".' in detail
    assert "restart the Manager and reload the page" in detail
    assert "other.example" not in detail
    assert "private-query" not in detail
    assert "secret" not in detail
    assert effects == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "null", "Referer": "https://manager.example/admin"},
        {"Origin": "https://user:password@manager.example"},
        {"Origin": "https://manager.example:invalid"},
        {"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_origin_error_does_not_suggest_invalid_or_cross_site_urls(csrf_client, headers):
    client, effects = csrf_client
    response = client.post(
        "/tasks/delete-selected",
        headers={**headers, "X-CSRF-Token": _page_token(client)},
        json={"task_ids": ["t1"]},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "Configure MANAGER_PUBLIC_URL in manager/.env" in detail
    assert "MANAGER_PUBLIC_URL=" not in detail
    assert "password" not in detail
    assert "attacker.example" not in detail
    assert effects == []


@pytest.mark.parametrize("root_path", ["", "/manager"])
@pytest.mark.parametrize("proxy_keeps_prefix", [False, True])
def test_form_token_survives_another_worker_and_reverse_proxy(
    csrf_client, monkeypatch, root_path, proxy_keeps_prefix
):
    _, effects = csrf_client
    monkeypatch.setattr(config, "MANAGER_PUBLIC_URL", f"https://manager.example{root_path}")
    path = f"{root_path if proxy_keeps_prefix else ''}/admin/credentials"
    with TestClient(_app(root_path), base_url="http://internal:8081") as first_worker:
        first_worker.auth = ("admin", "test-password")
        token = _page_token(first_worker, path)
    with TestClient(_app(root_path), base_url="http://internal:8081") as second_worker:
        second_worker.auth = ("admin", "test-password")
        response = second_worker.post(
            f"{path}/admins",
            headers={"Origin": "https://manager.example"},
            data={**_ADMIN_FORM, "csrf_token": token},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"{root_path}/admin/credentials?")
    assert effects == ["_upsert_admin_user_in_env", "reload", "publish"]


def test_header_token_takes_precedence_over_form_and_files_are_not_tokens(csrf_client):
    client, effects = csrf_client
    token = _page_token(client)
    response = client.post(
        "/admin/credentials/admins",
        headers={"Origin": "https://manager.example", "X-CSRF-Token": "invalid"},
        data={**_ADMIN_FORM, "csrf_token": token},
    )
    assert response.status_code == 403
    response = client.post(
        "/admin/reload-config",
        headers={"Origin": "https://manager.example"},
        files={"csrf_token": ("token.txt", token)},
    )
    assert response.status_code == 403
    assert effects == []


def test_csrf_token_does_not_replace_basic_authentication(csrf_client):
    client, effects = csrf_client
    token = _page_token(client)
    client.auth = None
    response = client.post(
        "/admin/credentials/admins",
        headers={"Origin": "https://manager.example", "X-CSRF-Token": token},
        data=_ADMIN_FORM,
    )
    assert response.status_code == 401
    assert effects == []


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://EXAMPLE.org/a?b=c", ("https", "example.org", 443)),
        ("http://example.org", ("http", "example.org", 80)),
        ("http://[::1]:8080/manager", ("http", "::1", 8080)),
        ("https://example.org:0", ("https", "example.org", 0)),
        ("null", None),
        ("/admin", None),
        ("ftp://example.org", None),
        ("https://user:password@example.org", None),
        ("https://example.org:invalid", None),
        ("https://[invalid", None),
        ("https://example.org\\@attacker.example", None),
        ("https://example.org\n", None),
    ],
)
def test_url_origin_normalization(value, expected):
    assert csrf._url_origin(value) == expected


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.asyncio
async def test_safe_methods_do_not_require_csrf(method):
    await csrf.verify_csrf(_request(method=method))


@pytest.mark.parametrize("age,valid", [(-1, False), (0, True), (28799, True), (28800, False)])
def test_token_expiry(monkeypatch, age, valid):
    monkeypatch.setattr(csrf.time, "time", lambda: 100_000)
    request = _request()
    token = csrf.build_csrf_token(request)
    monkeypatch.setattr(csrf.time, "time", lambda: 100_000 + age)
    assert csrf._valid_token(request, token) is valid


def test_tokens_are_randomized_and_bound_to_authentication_and_deployment(monkeypatch):
    request = _request()
    token = csrf.build_csrf_token(request)
    assert token != csrf.build_csrf_token(request)
    assert csrf._valid_token(request, token)
    assert not csrf._valid_token(_request(authorization="Basic other"), token)
    assert not csrf._valid_token(request, "9999999999999" + token)
    assert not csrf._valid_token(request, "é")
    monkeypatch.setattr(config, "MANAGER_PUBLIC_URL", "https://different.example")
    assert not csrf._valid_token(request, token)


def test_invalid_public_origin_fails_closed(csrf_client, monkeypatch):
    client, effects = csrf_client
    monkeypatch.setattr(config, "MANAGER_PUBLIC_URL", "invalid")
    response = client.post("/admin/reload-config", headers={"Origin": "invalid"})
    assert response.status_code == 403
    assert effects == []


def test_every_admin_mutation_declares_csrf_protection():
    mutations = set()
    for route in [*admin.router.routes, *task.router.routes]:
        if not isinstance(route, APIRoute) or not route.methods - {"GET", "HEAD", "OPTIONS"}:
            continue
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        if auth.verify_admin in dependencies:
            assert csrf.verify_csrf in dependencies, route.path
            mutations.add(
                route.path.replace("{admin_name}", "admin").replace("{token_name}", "client")
            )
        if auth.verify_token in dependencies:
            assert csrf.verify_csrf not in dependencies, route.path
    assert mutations == {path for path, _, _ in _MUTATIONS}
