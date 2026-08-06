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
    "No quota observations for this range": "Aucune observation de quota sur cette plage",
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
    "Exact tokens temporarily unavailable": ("Jetons exacts temporairement indisponibles"),
    "Exact token data invalid": "Données de jetons exactes invalides",
    "First": "Premier",
    "Last": "Dernier",
    # ── Token display ──
    "token activity": "activité des jetons",
    "Daily total": "Total journalier",
    "Total": "Total",
    "Daily": "Journalier",
    "15-min samples": "échantillons de 15 min",
    "Lifetime": "Durée de vie",
    "Peak day": "Pic journalier",
    "Current streak": "Série actuelle",
    "Longest streak": "Série la plus longue",
    "Longest turn": "Tour le plus long",
    "Codex summary": "Résumé Codex",
    "Summary": "Résumé",
    "account-wide": "à l'échelle du compte",
    "Source": "Source",
    # ── Package 4: exact daily indicators ──
    "Reported days": "Jours rapportés",
    "Avg/day": "Moy./jour",
    "Peak": "Pic",
    "Peak share": "Part du pic",
    # ── Package 5: quota card (used/remaining/countdown, compact) ──
    "Disabled": "Désactivé",
    "used": "utilisé",
    "remaining": "restant",
    "resets in ": "réinitialisation dans ",
    "in ": "dans ",
    "resets ": "réinitialisation ",
    "enabled": "activé",
    "disabled": "désactivé",
    "No reading": "Aucune lecture",
    "no percentage": "pas de pourcentage",
    # ── Package 5: settings page ──
    "Collect Claude": "Collecter Claude",
    "Collect Codex": "Collecter Codex",
    "Native desktop notifications": "Notifications de bureau natives",
    "Send native test notification": "Envoyer une notification de test native",
    "Native notifications are unavailable.": "Notifications natives indisponibles.",
    "Test failed: invalid settings.": "Échec du test : paramètres invalides.",
    "Test failed: keyring unavailable.": "Échec du test : trousseau indisponible.",
    "Test failed: notification unavailable.": "Échec du test : notification indisponible.",
    "Test failed: native notification unavailable.": (
        "Échec du test : notification native indisponible."
    ),
    "Compact mode": "Mode compact",
    "Claude thresholds (%)": "Seuils Claude (%)",
    "Codex thresholds (%)": "Seuils Codex (%)",
    "Claude reset alerts": "Alertes de réinitialisation Claude",
    "Codex reset alerts": "Alertes de réinitialisation Codex",
    "Claude error alerts": "Alertes d'erreur Claude",
    "Codex error alerts": "Alertes d'erreur Codex",
    "Check for updates": "Vérifier les mises à jour",
    "Checking for updates…": "Vérification des mises à jour…",
    "A new version is available: ": "Une nouvelle version est disponible : ",
    "Moira is up to date.": "Moira est à jour.",
    "Update check failed.": "Échec de la vérification des mises à jour.",
    "The update server returned an invalid response.": (  # noqa: E501
        "Le serveur de mise à jour a renvoyé une réponse invalide."
    ),
    # ── Package 5: diagnostics ──
    "Diagnostics": "Diagnostics",
    "Copy": "Copier",
    "Copied.": "Copié.",
    "Last refresh": "Dernière actualisation",
    "Next refresh": "Prochaine actualisation",
    "History writer": "Enregistreur d'historique",
    "channel": "canal",
    "Native channel": "Canal natif",
    "configured": "configuré",
    "not configured": "non configuré",
    "no data": "aucune donnée",
    "Copy quota status": "Copier l'état des quotas",
    "Quota status copied.": "État des quotas copié.",
    "Copy history summary": "Copier le résumé de l'historique",
    "History summary copied.": "Résumé de l'historique copié.",
    "Diagnostics copied.": "Diagnostics copiés.",
    # ── Package 5: export and delete-all ──
    "Export CSV": "Exporter en CSV",
    "Export JSON": "Exporter en JSON",
    "Delete all history…": "Tout supprimer de l'historique…",
    "Delete all history?": "Supprimer tout l'historique ?",
    "This removes every stored observation. Settings, keyring and current quota state are kept.": (  # noqa: E501
        "Cela supprime toutes les observations enregistrées. "
        "Les paramètres, le trousseau et l'état actuel des quotas sont conservés."
    ),
    "Cancel": "Annuler",
    "Delete": "Supprimer",
    "Exported": "Exporté",
    "rows": "lignes",
    "Export cancelled.": "Export annulé.",
    "Export failed.": "Échec de l'export.",
    "Exporting…": "Export en cours…",
    "Deleting…": "Suppression…",
    "Export history": "Exporter l'historique",
    "Nothing to export.": "Rien à exporter.",
    "Deletion failed.": "Échec de la suppression.",
    "History deleted.": "Historique supprimé.",
    "History is already empty.": "L'historique est déjà vide.",
    # ── Package 5: typed NTFY outcome statuses ──
    "sent": "envoyée",
    "invalid configuration": "configuration invalide",
    "network failure": "échec réseau",
    "timed out": "délai dépassé",
    "server error": "erreur du serveur",
    # ── Package 6: agent activity ──
    "Agent activity": "Activité des agents",
    "Agent is working": "L'agent travaille",
    "Agent integrations": "Intégrations des agents",
    "Claude Code": "Claude Code",
    "Codex CLI": "Codex CLI",
    "Hermes": "Hermes",
    "Active": "Actif",
    "Completed": "Terminé",
    "Failed": "Échec",
    "Interrupted": "Interrompu",
    "{count} active": "{count} actifs",
    "Set up": "Configurer",
    "Remove": "Retirer",
    "Test": "Tester",
    "Checking…": "Vérification…",
    "Last event: ": "Dernier événement : ",
    "Agent activity is unavailable.": "L'activité des agents est indisponible.",
    "Claude Code hooks installed.": "Hooks Claude Code installés.",
    "Claude Code integration is not installed.": ("L'intégration Claude Code n'est pas installée."),
    "Hermes is unavailable: ": "Hermes est indisponible : ",
    "not installed": "non installé",
    "version unknown": "version inconnue",
    "version probe failed": "échec de la sonde de version",
    "hooks probe failed": "échec de la sonde de hooks",
    "shell hooks unsupported": "hooks shell non pris en charge",
    "shell hooks available.": "hooks shell disponibles.",
    "Codex activity: Moira-owned app-server sessions only.": (
        "Activité Codex : sessions app-server appartenant à Moira uniquement."
    ),
    "Codex completions only — session ownership unavailable.": (
        "Complétions Codex uniquement — session indisponible."
    ),
    "Codex activity is unsupported.": "L'activité Codex n'est pas prise en charge.",
    "Codex CLI is not installed.": "Le CLI Codex n'est pas installé.",
    "Codex hooks are not supported by this version of Codex.": (
        "Les hooks Codex ne sont pas pris en charge par cette version de Codex."
    ),
    "The Codex hooks feature is disabled.": (
        "La fonctionnalité de hooks Codex est désactivée."
    ),
    "Codex CLI hooks installed and verified.": (
        "Hooks du CLI Codex installés et vérifiés."
    ),
    "Codex CLI hooks installed — approve the Codex hook trust prompt.": (
        "Hooks du CLI Codex installés — approuvez la demande de confiance des hooks Codex."
    ),
    "Codex CLI hook callbacks verified.": "Rappels de hooks du CLI Codex vérifiés.",
    "Codex turn notifications verified (real app-server session).": (
        "Notifications de tour Codex vérifiées (session app-server réelle)."
    ),
    "Callbacks verified.": "Rappels vérifiés.",
    "Callback verification failed.": "Échec de la vérification des rappels.",
    # ── Package 7a: Integrations page and inventory ──
    "Integrations": "Intégrations",
    "Agents": "Agents",
    "Providers and models": "Fournisseurs et modèles",
    "Refresh": "Actualiser",
    "Not configured": "Non configuré",
    "Not installed": "Non installé",
    "Unsupported": "Non pris en charge",
    "Temporarily unavailable": "Temporairement indisponible",
    "Invalid": "Invalide",
    "Activity": "Activité",
    "Quota percentage": "Pourcentage de quota",
    "Exact tokens": "Jetons exacts",
    "Balance": "Solde",
    "Cost": "Coût",
    "Main": "Principal",
    "Named": "Nommé",
    "Inventory refreshed.": "Inventaire actualisé.",
    "Inventory unavailable: ": "Inventaire indisponible : ",
    "No model assignments discovered.": "Aucune affectation de modèle découverte.",
    "hermes CLI not found": "commande hermes introuvable",
    "help probe failed": "échec de la sonde d'aide",
    "config surface unsupported": "surface config non prise en charge",
    "config probe failed": "échec de la sonde config",
    "config get unsupported": "config get non pris en charge",
    "config output oversized": "sortie config trop volumineuse",
    "config output malformed": "sortie config malformée",
    "config output incomplete": "sortie config incomplète",
    "config output invalid": "sortie config invalide",
    "collection disabled": "collecte désactivée",
    "no reading yet": "aucune lecture pour l'instant",
    "no exact token data yet": "aucune donnée de jetons exacts pour l'instant",
    "checking": "vérification",
    "deferred": "différé",
    "no default model": "aucun modèle par défaut",
    "inventory probe failed": "échec de la sonde d'inventaire",
    "Claude remains percentage-only": "Claude reste en pourcentage uniquement",
    # ── Package 7d: provider profiles and Keyring credentials ──
    "Edit providers": "Modifier les fournisseurs",
    "Add provider": "Ajouter un fournisseur",
    "Edit provider": "Modifier le fournisseur",
    "Slug": "Identifiant",
    "Label": "Libellé",
    "Kind": "Type",
    "Model": "Modèle",
    "Enabled": "Activé",
    "API base URL": "URL de base de l'API",
    "Hermes label": "Libellé Hermes",
    "API key": "Clé API",
    "Leave blank to keep the current credential.": (
        "Laisser vide pour conserver l'identifiant actuel."
    ),
    "Changing the slug removes the previous profile and its credential.": (
        "Changer l'identifiant retire le profil précédent et son identifiant du trousseau."
    ),
    "Credential": "Identifiant",
    "Remove credential": "Retirer l'identifiant",
    "Remove profile?": "Retirer le profil ?",
    "This removes the profile and its Moira Keyring credential.": (
        "Cela retire le profil et son identifiant Moira du trousseau."
    ),
    "Profile saved.": "Profil enregistré.",
    "Profile removed.": "Profil retiré.",
    "Credential removed.": "Identifiant retiré.",
    "Keyring unavailable.": "Trousseau indisponible.",
    "Operation failed.": "Échec de l'opération.",
    "Recovery required.": "Récupération requise.",
    "Test connection": "Tester la connexion",
    "Testing…": "Test en cours…",
    "Connected": "Connecté",
    "Authentication failed": "Échec d'authentification",
    "Model not found": "Modèle introuvable",
    "Unreachable": "Injoignable",
    "TLS error": "Erreur TLS",
    "Rate limited": "Limite de débit atteinte",
    "Invalid response": "Réponse invalide",
    "Cancelled": "Annulé",
    # ── Package 7p: exact DeepSeek balance refresh ──
    "Refresh balance": "Actualiser le solde",
    "Checking balance…": "Vérification du solde…",
    "Not checked": "Non vérifié",
    "Balance available": "Solde disponible",
    "Insufficient balance": "Solde insuffisant",
    "Server error": "Erreur serveur",
    "Granted": "Octroyé",
    "Topped up": "Rechargé",
    "Invalid profile.": "Profil invalide.",
    "Invalid value.": "Valeur invalide.",
    "Remote base URLs must not use a loopback address.": (
        "Les URL de base distantes ne doivent pas utiliser une adresse de boucle locale."
    ),
    "Slug is reserved.": "Identifiant réservé.",
    "Invalid slug.": "Identifiant invalide.",
    "Label is required.": "Le libellé est requis.",
    "Slug already in use.": "Identifiant déjà utilisé.",
    "Remote base URLs must use https.": "Les URL de base distantes doivent utiliser https.",
    "Local base URLs must use a loopback address.": (
        "Les URL de base locales doivent utiliser une adresse de boucle locale."
    ),
    "Base URL must not embed credentials, query or fragment.": (
        "L'URL de base ne doit pas contenir d'identifiants, de requête ou de fragment."
    ),
    "Invalid base URL.": "URL de base invalide.",
    "Invalid model.": "Modèle invalide.",
    "Invalid Hermes label.": "Libellé Hermes invalide.",
    "Invalid kind.": "Type invalide.",
    "Too many profiles.": "Trop de profils.",
    "Saving…": "Enregistrement…",
    "OpenAI compatible": "Compatible OpenAI",
    "Local": "Local",
    "Custom": "Personnalisé",
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
