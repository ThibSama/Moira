"""Deterministic tests for internationalization: French locale and English fallback."""

import os
from contextlib import AbstractContextManager
from unittest.mock import patch

from moira.i18n import detect_language, is_french, tr


def _set_locale(
    lang: str | None = None, lc_all: str | None = None, lc_messages: str | None = None
) -> AbstractContextManager[None]:
    env = {}
    if lang is not None:
        env["LANG"] = lang
    else:
        env["LANG"] = ""
    if lc_all is not None:
        env["LC_ALL"] = lc_all
    else:
        env["LC_ALL"] = ""
    if lc_messages is not None:
        env["LC_MESSAGES"] = lc_messages
    else:
        env["LC_MESSAGES"] = ""
    return patch.dict(os.environ, env, clear=False)


def test_french_locale_detected() -> None:
    with _set_locale("fr_FR.UTF-8"):
        assert detect_language() == "fr"
        assert is_french()


def test_english_locale_detected() -> None:
    with _set_locale("en_US.UTF-8"):
        assert detect_language() == "en"
        assert not is_french()


def test_french_fallback_for_other_locales() -> None:
    with _set_locale("de_DE.UTF-8"):
        assert detect_language() == "de"
        assert not is_french()


def test_tr_returns_french_for_french_locale() -> None:
    with _set_locale("fr_FR.UTF-8"):
        assert tr("Loading…") == "Chargement…"
        assert tr("Moira") == "Moira"
        assert tr("Refresh now") == "Actualiser maintenant"


def test_tr_returns_english_for_english_locale() -> None:
    with _set_locale("en_US.UTF-8"):
        assert tr("Loading…") == "Loading…"
        assert tr("Refresh now") == "Refresh now"


def test_tr_fallback_for_untranslated_key() -> None:
    with _set_locale("fr_FR.UTF-8"):
        assert tr("This key does not exist") == "This key does not exist"


def test_tr_french_coverage_core_strings() -> None:
    """Verify key UI strings have French translations."""
    with _set_locale("fr_FR.UTF-8"):
        assert tr("Quotas") == "Quotas"
        assert tr("Notifications") == "Notifications"
        assert tr("Save settings") == "Enregistrer les paramètres"
        assert tr("Claude and Codex quota monitor for Ubuntu") != (
            "Claude and Codex quota monitor for Ubuntu"
        )


def test_lc_all_overrides_lang() -> None:
    with _set_locale("en_US.UTF-8", lc_all="fr_FR.UTF-8"):
        assert detect_language() == "fr"


def test_lc_messages_overrides_lang() -> None:
    with _set_locale("en_US.UTF-8", lc_messages="fr_FR.UTF-8"):
        assert detect_language() == "fr"


def test_empty_environment_defaults_to_english() -> None:
    with (
        patch.dict(os.environ, {"LANG": "", "LC_ALL": "", "LC_MESSAGES": ""}, clear=False),
        patch("moira.i18n.locale.getlocale", return_value=("C", None)),
    ):
        result = detect_language()
        # Should be "c" or "en" — definitely not a real language from system locale
        assert result in {"en", "c", "posix"}


def test_exhaustion_strings_translated() -> None:
    with _set_locale("fr_FR.UTF-8"):
        assert tr("Weekly quota exhausted — usage blocked until reset") != (
            "Weekly quota exhausted — usage blocked until reset"
        )
        assert tr("Unavailable until weekly reset") != "Unavailable until weekly reset"


def test_french_token_placeholder_correct() -> None:
    """The French translation must use 'jeton', not the typo 'jetre'."""
    with _set_locale("fr_FR.UTF-8"):
        translated = tr("Leave blank to keep current keyring token")
        assert "jetre" not in translated
        assert "jeton" in translated
