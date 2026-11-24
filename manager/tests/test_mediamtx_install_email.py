"""Verify MediaMTX installer email rendering and delivery without real SMTP."""

import builtins
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import mediamtx_install_email as notifications

ROLE = Path(__file__).resolve().parents[1].name


@pytest.fixture
def smtp(tmp_path, monkeypatch):
    """Isolate dotenv configuration and capture complete MIME messages."""
    root = tmp_path / ROLE
    root.mkdir()
    (root / ".env").write_text(
        "SMTP_SERVER=smtp.example.invalid\n"
        "SMTP_PORT=587\nSMTP_USE_TLS=true\n"
        "SMTP_USERNAME=operator\nSMTP_PASSWORD='smtp-secret # ${LITERAL}'\n"
        "MANAGER_EMAIL=admin@example.invalid\n"
        "MANAGER_PUBLIC_URL=https://manager.example.invalid/prefix/\n"
    )
    for key in notifications.SETTING_KEYS:
        monkeypatch.delenv(key, raising=False)
    state = SimpleNamespace(root=root, messages=[], calls=[], failure=None)

    class SMTP:
        def __init__(self, *args, **kwargs):
            state.calls.append(("connect", args, kwargs))
            self.fail("connect")

        def fail(self, phase):
            if state.failure == phase:
                raise OSError("private-smtp-diagnostic")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def starttls(self, *, context):
            assert context.check_hostname
            self.fail("tls")
            state.calls.append(("tls",))

        def login(self, username, password):
            self.fail("login")
            state.calls.append(("login", username, password))

        def send_message(self, message):
            self.fail("send")
            state.messages.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))

    monkeypatch.setattr(notifications.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(notifications.socket, "getfqdn", lambda: "host.example.invalid")
    return state


def send_report(root, **overrides):
    """Send a typical update report with optional outcome overrides."""
    options = dict(
        root=root,
        command="update",
        previous_version="1.21.1",
        installed_version="1.21.3",
        changed=True,
    )
    options.update(overrides)
    return notifications.send_install_report(**options)


@pytest.mark.parametrize(
    "previous,changed,failed,expected",
    [
        (None, True, False, "MediaMTX 1.21.3 was installed."),
        ("1.21.1", True, False, "MediaMTX was updated from 1.21.1 to 1.21.3."),
        ("1.21.3", False, False, "No updates were applied to the MediaMTX executable."),
        ("1.21.1", False, True, "The MediaMTX command failed."),
        (None, True, True, "The MediaMTX command failed."),
    ],
)
def test_report_formats_and_actual_outcomes(smtp, previous, changed, failed, expected):
    assert (
        send_report(smtp.root, previous_version=previous, changed=changed, failed=failed) == "sent"
    )
    assert len(smtp.messages) == 1
    message = smtp.messages[0]
    assert ("FAILED" if failed else "SUCCESS") in message["Subject"]
    assert ("No updates" in message["Subject"]) == (not failed and not changed)
    assert message["To"] == "admin@example.invalid"
    assert message["From"] == "esup-runner@example.invalid"
    for subtype in ("plain", "html"):
        body = message.get_body((subtype,))
        assert body is not None
        text = body.get_content()
        assert expected in text
        assert "Host" in text and "Date" in text
        assert "https://manager.example.invalid/prefix/admin" in text
        assert "smtp-secret" not in text
        assert "private-smtp-diagnostic" not in text
        if failed:
            assert "not verified" in text
            assert "No updates" not in text
    html = message.get_body(("html",)).get_content()
    assert "cid:esup-runner-logo" in html
    assert any(part.get_content_type() == "image/png" for part in message.walk())
    assert smtp.calls == [
        ("connect", ("smtp.example.invalid", 587), {"timeout": 10}),
        ("tls",),
        ("login", "operator", "smtp-secret # ${LITERAL}"),
    ]


def test_environment_overrides_and_optional_authentication(smtp, monkeypatch):
    monkeypatch.setenv("SMTP_SERVER", "exported.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "")
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("SMTP_SENDER", "custom@example.invalid")
    assert send_report(smtp.root) == "sent"
    assert smtp.calls == [("connect", ("exported.example.invalid", 25), {"timeout": 10})]
    assert smtp.messages[0]["From"] == "custom@example.invalid"


def test_environment_only_settings_need_no_dotenv_file(smtp, monkeypatch):
    (smtp.root / ".env").unlink()
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.invalid")
    monkeypatch.setenv("MANAGER_EMAIL", "local-admin")
    assert send_report(smtp.root, installed_version=None) == "sent"
    assert smtp.messages[0]["From"] == "esup-runner@localhost"
    assert "unknown" in smtp.messages[0]["Subject"]


def test_unconfigured_smtp_is_skipped(smtp):
    (smtp.root / ".env").write_text("SMTP_SERVER=smtp.example.invalid\n")
    assert send_report(smtp.root) == "skipped"
    assert not smtp.calls


@pytest.mark.parametrize("component", ["manager", "runner"])
def test_component_smtp_fallback_rules(smtp, component):
    complete = (smtp.root / ".env").read_bytes()
    root = smtp.root.parent / component
    root.mkdir(exist_ok=True)
    sibling = root.parent / ("runner" if component == "manager" else "manager")
    sibling.mkdir(exist_ok=True)
    (sibling / ".env").write_bytes(complete)
    (root / ".env").write_text("SMTP_SERVER=incomplete.example.invalid\n")
    assert send_report(root) == ("sent" if component == "manager" else "skipped")
    if component == "manager":
        assert smtp.calls[0][1] == ("smtp.example.invalid", 587)
    else:
        assert not smtp.calls


@pytest.mark.parametrize("failure", ["connect", "tls", "login", "send"])
def test_smtp_errors_do_not_escape_or_expose_diagnostics(smtp, capsys, failure):
    smtp.failure = failure
    assert send_report(smtp.root) == "failed"
    assert not smtp.messages
    assert "private-smtp-diagnostic" not in str(capsys.readouterr())


@pytest.mark.parametrize("port", ["invalid", "0", "65536"])
def test_invalid_smtp_port_is_reported_without_sending(smtp, monkeypatch, port):
    monkeypatch.setenv("SMTP_PORT", port)
    assert send_report(smtp.root) == "failed"
    assert not smtp.calls


def test_missing_optional_dotenv_dependency_does_not_block_installer(smtp, monkeypatch):
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "dotenv":
            raise ModuleNotFoundError("dotenv")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert send_report(smtp.root) == "failed"
    assert not smtp.calls


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.invalid/admin/", "https://example.invalid/admin"),
        ("http://example.invalid/prefix", "http://example.invalid/prefix/admin"),
        ("javascript:alert(1)", ""),
        ("https://user:secret@example.invalid", ""),
        ("https://example.invalid?secret=value", ""),
        ("https://example.invalid/#secret", ""),
        ("http://[invalid", ""),
        ("", ""),
    ],
)
def test_manager_admin_links_are_safe(url, expected):
    settings = {"MANAGER_PUBLIC_URL": "", "MANAGER_URL": url}
    assert notifications.manager_admin_url(settings) == expected


def test_untrusted_detail_values_are_html_escaped(smtp, monkeypatch):
    monkeypatch.setattr(notifications.socket, "getfqdn", lambda: "<script>host</script>")
    assert send_report(smtp.root) == "sent"
    html = smtp.messages[0].get_body(("html",)).get_content()
    assert "<script>host</script>" not in html
    assert "&lt;script&gt;host&lt;/script&gt;" in html
