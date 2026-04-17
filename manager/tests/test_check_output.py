import app.core._check_output as out


def test_colorize_and_prefix_without_colors(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    assert out.colorize("plain", level="info") == "plain"
    assert out.format_prefix(level="warning") == out._PREFIXES["warning"]


def test_colorize_with_colors_enabled(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert out.colorize("plain", level="info") == f"{out._COLORS['info']}plain{out._RESET}"


def test_check_level_covers_all_outcomes():
    assert out.check_level(ok=True, required=True) == "info"
    assert out.check_level(ok=False, required=True) == "error"
    assert out.check_level(ok=False, required=False) == "warning"


def test_format_check_uses_computed_level(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    assert out.format_check("All good", ok=True, required=True) == (
        f"{out._PREFIXES['info']}: All good"
    )
    assert out.format_check("Optional missing", ok=False, required=False) == (
        f"{out._PREFIXES['warning']}: Optional missing"
    )
