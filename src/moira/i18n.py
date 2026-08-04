"""Internationalization: French for French locales, English fallback.

Uses a native Python translation table — functionally equivalent to gettext
but with no compiled .mo files or locale binary dependencies. Detects the
locale from the environment (LANG/LC_ALL/LC_MESSAGES) at call time. No manual
language selector.
"""

from __future__ import annotations

import locale
import os
from collections.abc import Callable

# French translation catalog. Keys are English source strings used throughout the codebase.
# Every visible string passes through tr(); missing keys fall back to the English source.
_FRENCH: dict[str, str] = {
    # ── Window / general ──
    "Moira": "Moira",
    "Quotas": "Quotas",
    "Notifications": "Notifications",
    "Refresh now": "Actualiser maintenant",
    "About Moira": "À propos de Moira",
    # ── Quota card headings ──
    "Claude": "Claude",
    "Codex": "Codex",
    # ── Status messages ──
    "Loading…": "Chargement…",
    "Available": "Disponible",
    "Stale — showing last successful values": "Obsolète — affichage des dernières valeurs valides",
    "Unavailable — no reading": "Indisponible — aucune lecture",
    "Not refreshed yet": "Pas encore actualisé",
    # ── Settings page ──
    "Enable NTFY alerts": "Activer les alertes NTFY",
    "Server URL": "URL du serveur",
    "Topic": "Sujet",
    "Optional access token": "Jeton d'accès facultatif",
    "Leave blank to keep current keyring token": (  # noqa: E501
        "Laisser vide pour conserver le jeton du trousseau"
    ),
    "Thresholds (%)": "Seuils (%)",
    "Alert when a quota resets": "Alerter lors d'une réinitialisation de quota",
    "Alert on refresh errors": "Alerter en cas d'erreur d'actualisation",
    "Start automatically on login": "Démarrer automatiquement à la connexion",
    "Refresh interval": "Intervalle d'actualisation",
    "minutes": "minutes",
    "Save settings": "Enregistrer les paramètres",
    "Send test notification": "Envoyer une notification de test",
    "Set up Claude integration": "Configurer l'intégration Claude",
    "Remove Claude integration": "Retirer l'intégration Claude",
    "Create desktop shortcut": "Créer un raccourci sur le bureau",
    "Remove desktop shortcut": "Retirer le raccourci du bureau",
    # ── Settings status ──
    "Settings saved. Token is stored only in GNOME Keyring.": (
        "Paramètres enregistrés. Le jeton est stocké uniquement dans le trousseau GNOME."
    ),
    "Could not save settings: ": "Impossible d'enregistrer les paramètres : ",
    "Invalid settings: ": "Paramètres invalides : ",
    "Sending test…": "Envoi du test…",
    "Test notification sent.": "Notification de test envoyée.",
    "Test failed: ": "Échec du test : ",
    "Claude integration installed. Complete one Claude response to populate quotas.": (
        "Intégration Claude installée. Terminez une réponse Claude pour renseigner les quotas."
    ),
    "Claude integration is already installed.": "L'intégration Claude est déjà installée.",
    "Claude integration was not changed: ": "L'intégration Claude n'a pas été modifiée : ",
    "Claude integration removed and the previous status line restored.": (
        "Intégration Claude retirée et l'ancienne ligne de statut restaurée."
    ),
    "Claude integration is not installed.": "L'intégration Claude n'est pas installée.",
    "Desktop shortcut created: ": "Raccourci bureau créé : ",
    "Desktop shortcut already exists: ": "Le raccourci bureau existe déjà : ",
    "Desktop shortcut removed.": "Raccourci bureau supprimé.",
    "Desktop shortcut is already absent.": "Le raccourci bureau est déjà absent.",
    "Desktop shortcut is unavailable: ": "Raccourci bureau indisponible : ",
    # ── About dialog ──
    "Claude and Codex quota monitor for Ubuntu": "Moniteur de quotas Claude et Codex pour Ubuntu",
    # ── Test notification ──
    "Moira test": "Test Moira",
    "Notifications are configured correctly.": "Les notifications sont correctement configurées.",
    # ── Quota card: detail labels ──
    "Resets ": "Réinitialisation ",
    " remaining": " restant",
    # ── Refresh state ──
    "Last refresh: ": "Dernière actualisation : ",
    " · ": " · ",
    " · Source: ": " · Source : ",
    "Next refresh: ": "Prochaine actualisation : ",
    # ── Exhaustion messages ──
    "Weekly quota exhausted — usage blocked until reset": (
        "Quota hebdomadaire épuisé — utilisation bloquée jusqu'à la réinitialisation"
    ),
    "Unavailable until weekly reset": "Indisponible jusqu'à la réinitialisation hebdomadaire",
    "Five-hour quota disabled due to weekly exhaustion": (
        "Quota de cinq heures désactivé en raison de l'épuisement hebdomadaire"
    ),
    # ── NTFY notification messages ──
    "Claude quota exhausted": "Quota Claude épuisé",
    "Codex quota exhausted": "Quota Codex épuisé",
    "Claude quota recovered": "Quota Claude rétabli",
    "Codex quota recovered": "Quota Codex rétabli",
    "Weekly usage has reached 100%. Usage is blocked until the weekly reset.": (
        "L'utilisation hebdomadaire a atteint 100 %. "
        "L'utilisation est bloquée jusqu'à la réinitialisation hebdomadaire."
    ),
    "Weekly quota has reset and usage is available again.": (
        "Le quota hebdomadaire a été réinitialisé et l'utilisation est à nouveau possible."
    ),
    "Claude quota reset": "Quota Claude réinitialisé",
    "Codex quota reset": "Quota Codex réinitialisé",
    # ── Error alert ──
    "Claude quota error": "Erreur de quota Claude",
    "Codex quota error": "Erreur de quota Codex",
    "Moira could not refresh quota data.": "Moira n'a pas pu actualiser les données de quota.",
    # ── Threshold alerts ──
    "Usage reached ": "Utilisation atteinte ",
    "%.": "%.",
    " quota entered a new window.": " — le quota a entamé une nouvelle fenêtre.",
    # ── History tab ──
    "History": "Historique",
    "Range": "Plage",
    "Filter": "Filtre",
    "All": "Tous",
    "No history data for this range": "Aucune donnée d'historique pour cette plage",
    "Database unavailable": "Base de données indisponible",
    "Schema mismatch": "Incompatibilité de schéma",
    "No observations": "Aucune observation",
    "Latest": "Dernier",
    "Min": "Min",
    "Max": "Max",
    "Count": "Nombre",
    "Resets": "Réinitialisations",
    "No data": "Aucune donnée",
    "No history database": "Aucune base de données d'historique",
    "Exact token usage is not available": "L'utilisation exacte des jetons n'est pas disponible",
    "First": "Premier",
    "Last": "Dernier",
    # ── Token display ──
    "token activity": "activité des jetons",
    "Total": "Total",
    "Input": "Entrée",
    "Cached": "Cache",
    "Output": "Sortie",
    "Reasoning": "Raisonnement",
    "Source": "Source",
}


def detect_language() -> str:
    """Detect the user's language code from the environment.

    Returns a lowercase language code like 'fr' or 'en'.
    Checks LC_ALL, LC_MESSAGES, and LANG in order.
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            code = value.split(".")[0].split("_")[0].lower()
            if code:
                return code
    # Also try the C locale's default
    try:
        loc = locale.getlocale()[0]
        if loc:
            return loc.split("_")[0].lower()
    except (ValueError, TypeError):
        pass
    return "en"


def is_french() -> bool:
    return detect_language() == "fr"


def tr(source: str) -> str:
    """Translate a source string to the user's locale, or return it unchanged.

    This is the native equivalent of gettext's _().
    """
    if is_french():
        return _FRENCH.get(source, source)
    return source


def make_translator() -> Callable[[str], str]:
    """Return a callable bound to the current locale at call time.

    Useful for passing as `_` to functions that need a translator.
    The returned callable re-checks the locale on each call so tests
    can switch locales mid-run.
    """
    return tr
