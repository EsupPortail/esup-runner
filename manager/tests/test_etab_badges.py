"""Tests for establishment badge presentation data."""

from app.core.etab_badges import build_etab_badge


def test_build_etab_badge_uses_known_establishment_codes():
    """Known production establishments keep explicit two-letter codes and colors."""
    expected = {
        "Université de Lorraine": ("UL", "etab-badge-0"),
        "Université de Montpellier": ("UM", "etab-badge-1"),
        "La Rochelle Université": ("LR", "etab-badge-2"),
        "Université Côte d'Azur": ("UC", "etab-badge-3"),
        "ESUP-Portail": ("EP", "etab-badge-4"),
    }

    for etab_name, (initials, color_class) in expected.items():
        badge = build_etab_badge(etab_name)
        assert badge == {
            "name": etab_name,
            "initials": initials,
            "color_class": color_class,
        }


def test_build_etab_badge_normalizes_accents_and_whitespace():
    """Equivalent names should share a code and color."""
    badge = build_etab_badge("  Universite   Cote d'Azur ")

    assert badge is not None
    assert badge["initials"] == "UC"
    assert badge["color_class"] == "etab-badge-3"
    assert badge["name"] == "Universite Cote d'Azur"


def test_build_etab_badge_has_stable_fallback_for_unknown_names():
    """Unknown establishments receive two initials and a repeatable color."""
    first = build_etab_badge("Université de Rennes")
    second = build_etab_badge("Université de Rennes")

    assert first == second
    assert first is not None
    assert first["initials"] == "UR"
    assert first["color_class"].startswith("etab-badge-")


def test_build_etab_badge_returns_none_for_empty_names():
    """Empty establishment names do not create an empty badge."""
    assert build_etab_badge(None) is None
    assert build_etab_badge("  ") is None


def test_build_etab_badge_covers_fallback_initials_shapes():
    """Fallback initials remain defined for punctuation and stop-word names."""
    assert build_etab_badge("!!!")["initials"] == "??"
    assert build_etab_badge("Paris Lyon")["initials"] == "PL"
    assert build_etab_badge("de la")["initials"] == "DL"
