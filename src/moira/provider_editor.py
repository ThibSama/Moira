"""Package 7d — GTK provider profile editor ("Edit providers").

The editor manages local provider profiles and their Moira-owned Keyring
credentials. Keyring and persistence work runs off GTK through a bounded
generation pipeline: at most one operation is in flight plus one parked
(newest-wins replacement), and a stale completion — whose generation no
longer matches — never overwrites newer form state. The editor never
mutates Hermes configuration; it only writes Moira's own config file and
its own Keyring items.

Privacy: the UI shows only ``configured`` / ``not configured`` and
translated sanitized outcomes. Raw libsecret exceptions, paths, URLs,
profile JSON and secrets never reach the UI.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from .connection_test import ConnectionResult, ConnectionState, run_connection_test
from .i18n import tr
from .integrations import (
    MAX_PROFILES,
    ProviderKind,
    ProviderProfile,
)
from .persistence import load_settings, update_settings
from .profile_journal import (
    JournalEntry,
    JournalPhase,
    clear_journal,
    recover_pending_transaction,
    write_journal,
)
from .secrets import (
    BACKUP_PURPOSE,
    KeyringLookup,
    KeyringMutation,
    ProviderSecret,
    erase_provider_secret,
    has_provider_secret,
    inspect_provider_secret,
    store_provider_secret,
)

_ = tr

#: Kind order for the dropdown and display labels (translated at render).
_KIND_ORDER: tuple[ProviderKind, ...] = tuple(ProviderKind)
_KIND_LABELS = {
    ProviderKind.DEEPSEEK: "DeepSeek",
    ProviderKind.OPENAI_COMPATIBLE: "OpenAI compatible",
    ProviderKind.OPENROUTER: "OpenRouter",
    ProviderKind.ANTHROPIC: "Anthropic",
    ProviderKind.OPENAI: "OpenAI",
    ProviderKind.LOCAL: "Local",
    ProviderKind.CUSTOM: "Custom",
}

#: Connection-test state labels (translated at render; every state has a
#: key, so an unknown state can never reach the UI).
_CONNECTION_STATE_LABELS = {
    ConnectionState.CONNECTED: "Connected",
    ConnectionState.NOT_CONFIGURED: "Not configured",
    ConnectionState.AUTH_FAILED: "Authentication failed",
    ConnectionState.MODEL_NOT_FOUND: "Model not found",
    ConnectionState.UNREACHABLE: "Unreachable",
    ConnectionState.TLS_ERROR: "TLS error",
    ConnectionState.RATE_LIMITED: "Rate limited",
    ConnectionState.INVALID_RESPONSE: "Invalid response",
    ConnectionState.UNSUPPORTED: "Unsupported",
    ConnectionState.CANCELLED: "Cancelled",
}

#: Stable sanitized success text per operation kind.
_SUCCESS_TEXT = {
    "save_profile": "Profile saved.",
    "remove_profile": "Profile removed.",
    "remove_credential": "Credential removed.",
}


@dataclass(frozen=True, slots=True)
class ProfileOp:
    """One bounded off-GTK mutation, captured entirely on the GTK thread.

    Ops are typed DELTAS, never full profile tuples: each op carries the
    profile being upserted (``save_profile``) or the slug being removed,
    and is applied to the latest committed collection at execution time.
    Pending ops therefore never capture stale collections, so rapid edits
    cannot overwrite committed changes. ``old_slug`` marks a rename
    (create-new plus explicit old removal); ``credential`` may be blank
    (preserve/migrate the existing secret).
    """

    kind: str  # reload | save_profile | remove_profile | remove_credential
    profile: ProviderProfile | None = None
    slug: str = ""
    old_slug: str = ""
    credential: str = ""


@dataclass(frozen=True, slots=True)
class ProfileOpResult:
    """Sanitized outcome of one operation (never carries secrets or raw
    exceptions; ``reason`` is a stable translated key)."""

    ok: bool
    reason: str = ""
    profiles: tuple[ProviderProfile, ...] = ()
    configured: tuple[tuple[str, bool], ...] = ()
    kind: str = ""
    slug: str = ""


def _keyring_states(profiles: tuple[ProviderProfile, ...]) -> tuple[tuple[str, bool], ...]:
    """Slug → credential-configured for every profile (safe lookups)."""
    return tuple((profile.slug, has_provider_secret(profile.slug)) for profile in profiles)


def _persist_profiles(profiles: tuple[ProviderProfile, ...]) -> None:
    """Validate the whole collection and persist it through the single
    config boundary (raises on failure; unrelated fields are preserved)."""
    update_settings(lambda settings: replace(settings, provider_profiles=profiles))


def _ok_result(kind: str, profiles: tuple[ProviderProfile, ...], slug: str = "") -> ProfileOpResult:
    return ProfileOpResult(True, "", profiles, _keyring_states(profiles), kind, slug)


def _fail_result(kind: str, reason: str, slug: str = "") -> ProfileOpResult:
    return ProfileOpResult(False, reason, (), (), kind, slug)


def _journal_entry(
    op: ProfileOp,
    phase: JournalPhase,
    *,
    profile: ProviderProfile | None = None,
    old_slug: str = "",
    secret_slug: str = "",
    had_backup: bool = False,
) -> JournalEntry:
    slug = profile.slug if profile is not None else op.slug
    return JournalEntry(1, op.kind, phase, profile, old_slug, slug, secret_slug, had_backup)


def _execute_save_profile(op: ProfileOp) -> ProfileOpResult:
    """Recoverable save/edit/toggle/rename with a durable phase journal.

    Protocol (every side effect happens after its intent is journaled):

    1. ``staged`` — intent recorded; nothing mutated yet.
    2. backup any credential being overwritten at the target slug
       (Moira-owned ``backup`` Keyring entry) — lossless recovery.
    3. ``staged-secret`` — store/migrate the credential at the target.
    4. ``config-committed`` — persist the collection under the config
       lock (preserving every concurrent change).
    5. rename: clear the old slug's credential (create-new then old
       removal).
    6. clear the obsolete backup, clear the journal.

    On any failure the transaction is rolled back with the journal
    cleared ONLY when the rollback is durable; otherwise the journal is
    KEPT at its phase and ``recover_pending_transaction`` converges it
    on the next reload (never destroying the last recoverable state).
    """
    assert op.profile is not None
    profile = op.profile
    rename = bool(op.old_slug) and op.old_slug != profile.slug
    current = load_settings().provider_profiles
    base = [p for p in current if p.slug != profile.slug and p.slug != op.old_slug]
    if len(base) + 1 > MAX_PROFILES:
        return _fail_result(op.kind, "Operation failed.", profile.slug)

    value = op.credential.strip()
    if rename and not value:
        # Blank credential on rename migrates the existing secret — but a
        # UNAVAILABLE lookup is never absence: fail closed so the last
        # credential is never cleared on a failed lookup.
        inspection = inspect_provider_secret(op.old_slug)
        if inspection is None or inspection.state is KeyringLookup.UNAVAILABLE:
            return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
        value = inspection.value or ""

    secret_slug = profile.slug if value else ""
    had_backup = False
    target_inspection: ProviderSecret | None = None
    if value:
        # Strict target mapping: FOUND → durable backup before overwrite;
        # ABSENT → no backup; UNAVAILABLE/invalid → fail BEFORE any
        # journal or mutation (an unknown credential is never overwritten
        # without a backup).
        target_inspection = inspect_provider_secret(profile.slug)
        if target_inspection is None or target_inspection.state is KeyringLookup.UNAVAILABLE:
            return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
        had_backup = target_inspection.state is KeyringLookup.FOUND

    rollback_entry = _journal_entry(
        op,
        JournalPhase.STAGED_SECRET,
        profile=profile,
        old_slug=op.old_slug,
        secret_slug=secret_slug,
        had_backup=had_backup,
    )

    try:
        write_journal(
            _journal_entry(
                op,
                JournalPhase.STAGED,
                profile=profile,
                old_slug=op.old_slug,
                secret_slug=secret_slug,
                had_backup=had_backup,
            )
        )
        if value:
            if had_backup:
                assert target_inspection is not None and target_inspection.value is not None
                if (
                    store_provider_secret(profile.slug, target_inspection.value, BACKUP_PURPOSE)
                    is not KeyringMutation.DONE
                ):
                    # Backup intent could not be durably recorded: roll
                    # back nothing (nothing was stored) and fail closed.
                    clear_journal()
                    return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
            write_journal(
                _journal_entry(
                    op,
                    JournalPhase.STAGED_SECRET,
                    profile=profile,
                    old_slug=op.old_slug,
                    secret_slug=secret_slug,
                    had_backup=had_backup,
                )
            )
            if store_provider_secret(profile.slug, value) is not KeyringMutation.DONE:
                _rollback_staged(rollback_entry)
                return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
        new_collection = tuple(sorted(base + [profile], key=lambda p: p.slug))
        try:
            _persist_profiles(new_collection)
        except Exception:
            # Known config failure: the journal is STILL in its rollback /
            # no-op phase (the forward phase is only written AFTER a
            # successful persist), so recovery rolls back / no-ops —
            # never forward — with NO rewrite of the journal required.
            # A failed cleanup keeps that rollback/no-op journal, retried
            # idempotently.
            if secret_slug:
                _rollback_staged(rollback_entry)
            else:
                clear_journal()
            return _fail_result(op.kind, "Operation failed.", profile.slug)
        # Forward phase: written only after the config persist succeeded,
        # so a failed persist can never leave a forward-recovery journal.
        write_journal(
            _journal_entry(
                op,
                JournalPhase.CONFIG_COMMITTED,
                profile=profile,
                old_slug=op.old_slug,
                secret_slug=secret_slug,
                had_backup=had_backup,
            )
        )
        if rename:
            if erase_provider_secret(op.old_slug) is not KeyringMutation.DONE:
                # Restore the config FIRST; only transition to the
                # rollback/no-op phase once that restoration is durable.
                try:
                    _persist_profiles(current)
                except Exception:
                    # Config restoration failed: retain forward recovery
                    # (the journal stays at CONFIG_COMMITTED).
                    return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
                if secret_slug:
                    write_journal(rollback_entry)
                    _rollback_staged(rollback_entry)
                else:
                    write_journal(
                        _journal_entry(
                            op,
                            JournalPhase.STAGED,
                            profile=profile,
                            old_slug=op.old_slug,
                            secret_slug="",
                            had_backup=False,
                        )
                    )
                    clear_journal()
                return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
        if had_backup:
            # Mandatory backup cleanup: a Moira backup remaining after a
            # committed overwrite is never reported as success. Keep the
            # forward journal and return a sanitized failure; idempotent
            # recovery removes the backup before clearing the journal.
            if erase_provider_secret(profile.slug, BACKUP_PURPOSE) is not KeyringMutation.DONE:
                return _fail_result(op.kind, "Operation failed.", profile.slug)
        if not clear_journal():
            # A required journal remains: never report success. Recovery
            # completes the committed state forward and clears it later.
            return _fail_result(op.kind, "Operation failed.", profile.slug)
        return _ok_result(op.kind, new_collection, profile.slug)
    except Exception:
        # Unexpected failure: keep the journal — recovery converges to a
        # documented consistent state on the next reload.
        return _fail_result(op.kind, "Operation failed.", profile.slug)


def _rollback_staged(entry: JournalEntry) -> None:
    """Roll the staged-secret effect back (shared semantics with journal
    recovery): clear the staged value at the journaled ``secret_slug``,
    restore the backup, drop the backup. The journal is cleared only when
    the rollback is durable — otherwise it stays at the rollback phase
    and recovery retries it idempotently."""
    from .profile_journal import _rollback_staged_secret

    if _rollback_staged_secret(entry):
        clear_journal()


def _execute_remove_profile(op: ProfileOp) -> ProfileOpResult:
    """Confirmed removal with a durable phase journal.

    Either removes both the profile and its Moira credential, or leaves
    both unchanged. An absent credential is a successful no-op; an
    unavailable Keyring is a sanitized failure.
    """
    current = load_settings().provider_profiles
    new_collection = tuple(p for p in current if p.slug != op.slug)
    if len(new_collection) == len(current):
        return _ok_result(op.kind, current, op.slug)  # nothing to remove
    try:
        write_journal(_journal_entry(op, JournalPhase.STAGED))
        try:
            _persist_profiles(new_collection)
        except Exception:
            # Config unchanged and the journal is still in its no-op
            # phase (the forward phase is only written AFTER a successful
            # persist): recovery no-ops — the original profile and
            # credential are preserved. No rewrite of the journal needed.
            clear_journal()
            return _fail_result(op.kind, "Operation failed.", op.slug)
        # Forward phase: written only after the config persist succeeded.
        write_journal(_journal_entry(op, JournalPhase.CONFIG_COMMITTED))
        if erase_provider_secret(op.slug) is not KeyringMutation.DONE:
            try:
                _persist_profiles(current)
            except Exception:
                # Rollback persist failed: keep the journal at
                # config-committed — forward recovery completes the removal.
                return _fail_result(op.kind, "Keyring unavailable.", op.slug)
            # Config restoration durable: durably select the no-op phase
            # before cleanup (the credential was never cleared).
            write_journal(_journal_entry(op, JournalPhase.STAGED))
            clear_journal()
            return _fail_result(op.kind, "Keyring unavailable.", op.slug)
        if not clear_journal():
            # A required journal remains: never report success. Recovery
            # completes the removal forward and clears it later.
            return _fail_result(op.kind, "Operation failed.", op.slug)
        return _ok_result(op.kind, new_collection, op.slug)
    except Exception:
        return _fail_result(op.kind, "Operation failed.", op.slug)


def _execute_op(op: ProfileOp) -> ProfileOpResult:
    """Run one operation off GTK (executor thread). Never raises: every
    failure maps to a sanitized stable outcome."""
    try:
        if op.kind == "reload":
            # Recover any journaled operation before accepting the
            # persisted state (a failed recovery keeps the journal and
            # surfaces the translated "Recovery required." outcome).
            if not recover_pending_transaction():
                return ProfileOpResult(False, "Recovery required.", (), (), "reload", "")
            profiles = load_settings().provider_profiles
            return _ok_result("reload", profiles)
        if op.kind == "remove_credential":
            if erase_provider_secret(op.slug) is not KeyringMutation.DONE:
                return _fail_result(op.kind, "Keyring unavailable.", op.slug)
            return _ok_result(op.kind, load_settings().provider_profiles, op.slug)
        if op.kind == "save_profile":
            return _execute_save_profile(op)
        if op.kind == "remove_profile":
            return _execute_remove_profile(op)
    except Exception:
        return _fail_result(op.kind, "Operation failed.", op.slug)
    return _fail_result(op.kind, "Operation failed.", op.slug)


def _form_error_key(exc: ValueError) -> str:
    """Map a ProviderProfile validation error to a translated key."""
    message = str(exc)
    for fragment, key in (
        ("profile slug is reserved", "Slug is reserved."),
        ("profile slug must", "Invalid slug."),
        ("must not contain control characters", "Invalid value."),
        ("profile label", "Label is required."),
        ("remote profiles require an https", "Remote base URLs must use https."),
        ("local profiles require a loopback", "Local base URLs must use a loopback address."),
        (
            "remote profiles must not use a loopback",
            "Remote base URLs must not use a loopback address.",
        ),
        ("must not embed credentials", "Base URL must not embed credentials, query or fragment."),
        (
            "must not contain a query or fragment",
            "Base URL must not embed credentials, query or fragment.",
        ),
        ("base URL", "Invalid base URL."),
        ("profile model", "Invalid model."),
        ("profile hermes_label", "Invalid Hermes label."),
        ("profile kind", "Invalid kind."),
        ("profile enabled", "Invalid profile."),
        ("profile base_url", "Invalid base URL."),
    ):
        if fragment in message:
            return key
    return "Invalid profile."


class _ConnectionCoordinator:
    """One in-flight connection test plus one NEWEST pending request.

    A single invariant-preserving state machine owns every transition:
    exactly one of (idle) ``_inflight is None and _pending is None``,
    (running) ``_inflight == gen and _pending is None``, or (parked)
    ``_inflight == gen_a`` with ``_pending == (gen_b, ...)`` for a newer
    gen_b — ``_pending`` is NEVER set while ``_inflight`` is None, and
    submitters, runners and callbacks are never invoked under the lock.

    Reservation/commit protocol: ``request()`` reserves the slot under
    the lock; every external submit goes through ``_attempt()``, which
    RECHECKS shutdown under the lock immediately before committing the
    submit. If close wins between the reservation and the commit, the
    reservation and any parked work are released and zero submits
    happen — uncommitted work is never submitted after cancellation.
    Work already committed before close self-bounds (``_run`` checks
    cancellation before any Keyring read or spawn) and publishes
    nothing (the completion guard discards it).

    Failure recovery is ITERATIVE, never recursive: on a rejected
    submit (first or promoted) the failed generation is cleared and the
    newest parked request is detached ATOMICALLY under the lock; the
    failed request is completed deterministically through its own
    callback (a translated sanitized failure resetting its row from
    "Testing…") OUTSIDE the lock; then the detached request is
    attempted, looping until a submit is accepted or no parked request
    remains — a request that parks during a rejection is re-attached
    and attempted, so no accepted request can orphan. Runner
    exceptions complete deterministically through the same
    publish/promote path, ``cancel()`` invalidates in-flight and
    pending generations atomically, and the row-level token check
    (widget identity + render epoch) additionally discards results for
    edited, renamed, removed or disabled profiles.
    """

    def __init__(self, submit: Any, shutdown_event: threading.Event) -> None:
        self._submit = submit
        self._shutdown = shutdown_event
        self._lock = threading.Lock()
        self._generation = 0
        self._inflight: int | None = None
        self._pending: tuple[int, ProviderProfile, Any, Any] | None = None

    def request(self, profile: ProviderProfile, token: Any, callback: Any) -> bool:
        """Reserve the slot under the lock; the commit (shutdown recheck
        plus external submit) happens through ``_attempt`` outside the
        lock. Returns False when close already won, when close wins
        before the commit, or when THIS first submit was rejected."""
        with self._lock:
            if self._shutdown.is_set():
                return False  # close racing with request: no new work
            self._generation += 1
            generation = self._generation
            if self._inflight is not None:
                self._pending = (generation, profile, token, callback)  # newest wins
                return True
            self._inflight = generation
        return self._attempt((generation, profile, token, callback))

    def _run(self, generation: int, profile: ProviderProfile, token: Any, callback: Any) -> None:
        if self._shutdown.is_set():
            with self._lock:
                self._inflight = None
                self._pending = None  # close discards everything
            return  # queued work after close: zero Keyring, zero spawn
        try:
            result = run_connection_test(profile, cancel_event=self._shutdown)
        except Exception:
            # A runner failure still completes deterministically through
            # the SAME publish/promote path as a normal outcome.
            result = ConnectionResult(ConnectionState.UNREACHABLE, profile.slug)
        with self._lock:
            if self._shutdown.is_set():
                self._inflight = None
                self._pending = None
                return  # close during the run: never publish
            if generation != self._inflight:
                return  # superseded (cancelled): never publish
            pending = self._pending
            self._pending = None
            self._inflight = pending[0] if pending is not None else None
        try:
            callback(token, result)
        except Exception:
            pass  # a failing publisher must not wedge the coordinator
        if pending is not None:
            self._attempt(pending)

    def _attempt(self, request: tuple[int, ProviderProfile, Any, Any]) -> bool:
        """Commit ``request`` (whose generation is ALREADY reserved in
        ``_inflight``) through the reservation/commit protocol, or on
        rejection recover and ITERATIVELY attempt the newest parked
        request — never recursively. Returns True iff the ORIGINAL
        request's submit was accepted by the executor."""
        first = True
        while True:
            # Commit recheck: close winning here releases the reservation
            # and performs ZERO submits (uncommitted work is never
            # submitted after cancellation).
            with self._lock:
                if self._shutdown.is_set():
                    self._inflight = None
                    self._pending = None
                    return False
            try:
                self._submit(self._run, *request)
                return first
            except Exception:
                # Submit rejection: atomically clear the failed
                # generation and detach the newest parked request.
                generation, profile, token, callback = request
                parked, closed = self._recover_submit_failure(generation)
                if not closed:
                    # Deterministic rejection completion (row reset from
                    # "Testing…") OUTSIDE the lock; a request parking
                    # DURING the rejection must stay reachable.
                    self._reject(callback, token, profile)
                    if parked is None:
                        with self._lock:
                            if self._shutdown.is_set():
                                self._inflight = None
                                self._pending = None
                            elif self._inflight is None:
                                parked = self._pending
                                self._pending = None
                                if parked is not None:
                                    self._inflight = parked[0]
                if parked is None:
                    return False
                first = False
                request = parked

    def _recover_submit_failure(
        self, generation: int
    ) -> tuple[tuple[int, ProviderProfile, Any, Any] | None, bool]:
        """Atomically clear the failed generation and detach the newest
        parked request, handing it the slot. Returns ``(parked, closed)``:
        the request to attempt next (or None) and whether close already
        discarded everything (in which case nothing may publish)."""
        with self._lock:
            if self._shutdown.is_set():
                self._inflight = None
                self._pending = None
                return None, True
            if self._inflight == generation:
                self._inflight = None
            parked = self._pending
            self._pending = None
            if parked is not None:
                if self._inflight is None:
                    self._inflight = parked[0]
                else:
                    # Another attempt owns the slot: keep the detached
                    # request reachable through that attempt's completion.
                    self._pending = parked
                    parked = None
        return parked, False

    @staticmethod
    def _reject(callback: Any, token: Any, profile: ProviderProfile) -> None:
        """Deterministic rejection completion: the request's row is
        reset from "Testing…" to the translated sanitized failure —
        outside the lock, never raising."""
        try:
            callback(token, ConnectionResult(ConnectionState.UNREACHABLE, profile.slug))
        except Exception:
            pass

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._inflight = None
            self._pending = None


class ProviderEditor(Gtk.Window):
    """Modal editor window: list, add, edit, enable/disable, remove."""

    def __init__(
        self,
        *,
        submit: Any,
        on_profiles_changed: Any = None,
    ) -> None:
        super().__init__(title=_("Edit providers"))
        self.set_default_size(720, 520)
        self._submit = submit
        self._on_profiles_changed = on_profiles_changed
        self._shutdown = False
        self._shutdown_event = threading.Event()
        self._connection_coordinator = _ConnectionCoordinator(submit, self._shutdown_event)
        self._recovery_blocked = False
        self._in_flight = False
        self._pending_op: ProfileOp | None = None
        self._generation = 0
        self._profiles: tuple[ProviderProfile, ...] = ()
        self._configured: dict[str, bool] = {}
        self._editing_slug: str | None = None
        self._pending_removal: str | None = None
        self._rendering = False
        self._row_epoch = 0

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)

        header = Gtk.Box(spacing=8)
        title = Gtk.Label(label=_("Edit providers"), xalign=0)
        title.add_css_class("heading")
        header.append(title)
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("dim-label")
        header.append(self.status_label)
        root.append(header)

        self.add_button = Gtk.Button(label=_("Add provider"))
        self.add_button.connect("clicked", lambda *_args: self._show_form(None))
        root.append(self.add_button)

        self._list_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self.list_box)
        self._list_area.append(scroller)
        root.append(self._list_area)

        self._form_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._form_area.set_visible(False)
        self._build_form()
        root.append(self._form_area)

        self.set_child(root)
        self._render_list()
        # Initial state: load the persisted collection and keyring states.
        self._request_op(ProfileOp("reload"))

    # ── Bounded generation pipeline (off GTK) ──

    def _request_op(self, op: ProfileOp) -> None:
        if self._shutdown or self._recovery_blocked:
            return
        if self._in_flight:
            self._pending_op = op  # newest-wins replacement
            return
        self._start_op(op)

    def _start_op(self, op: ProfileOp) -> None:
        self._generation += 1
        generation = self._generation
        self._in_flight = True
        self.status_label.set_text(_("Loading…") if op.kind == "reload" else _("Saving…"))
        try:
            self._submit(self._run_op, op, generation, self._shutdown_event)
        except Exception:
            # Submit rejection (e.g. a closed executor): clear the slot,
            # show a fixed translated outcome, then accept later work.
            # The pending op is promoted immediately (at most one level of
            # recursion: the pending slot is emptied before promotion).
            self._in_flight = False
            self.status_label.set_text(_("Operation failed."))
            pending = self._pending_op
            self._pending_op = None
            if pending is not None:
                self._start_op(pending)

    def _run_op(self, op: ProfileOp, generation: int, shutdown_event: threading.Event) -> None:
        if shutdown_event.is_set():
            # Queued-not-started operations never write after shutdown.
            return
        result = _execute_op(op)
        GLib.idle_add(self._apply_op, result, generation)

    def _apply_op(self, result: ProfileOpResult, generation: int) -> bool:
        if self._shutdown or generation != self._generation:
            return False  # stale completion never overwrites newer form state
        self._in_flight = False
        pending = self._pending_op
        self._pending_op = None
        if result.ok:
            self._recovery_blocked = False
            self._profiles = result.profiles
            self._configured = dict(result.configured)
            self._render_list()
            self.status_label.set_text(_(_SUCCESS_TEXT.get(result.kind, "")))
            if result.kind in ("save_profile", "remove_profile"):
                self._show_list()
                if self._on_profiles_changed is not None:
                    self._on_profiles_changed()
        else:
            self._recovery_blocked = result.reason == "Recovery required."
            self.status_label.set_text(_(result.reason))
        if pending is not None and not self._recovery_blocked:
            # A failed reload recovery admits NO pending mutation: the
            # parked work is discarded (the persisted state is unknown);
            # only a later successful explicit reload re-enables mutations.
            self._start_op(pending)
        return False

    # ── Views ──

    def _show_list(self) -> None:
        if self._shutdown:
            return
        self._form_area.set_visible(False)
        self._list_area.set_visible(True)
        self._render_list()

    def _render_list(self) -> None:
        self._rendering = True
        try:
            self._row_epoch += 1  # every rebuild invalidates in-flight test results
            self.list_box.remove_all()
            self._row_widgets: dict[str, dict[str, Gtk.Widget]] = {}
            for profile in self._profiles:
                row = Gtk.ListBoxRow()
                row.set_child(self._build_row(profile))
                self.list_box.append(row)
        finally:
            self._rendering = False

    def _build_row(self, profile: ProviderProfile) -> Gtk.Widget:
        slug = profile.slug
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        top = Gtk.Box(spacing=8)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=profile.label, xalign=0)
        name.add_css_class("heading")
        info.append(name)
        model_text = profile.model or "—"
        detail = Gtk.Label(label=f"{_(_KIND_LABELS[profile.kind])} · {model_text}", xalign=0)
        detail.add_css_class("dim-label")
        info.append(detail)
        top.append(info)
        switch = Gtk.Switch(active=profile.enabled, valign=Gtk.Align.CENTER)
        switch.connect("state-set", self._on_enabled_toggled, slug)
        top.append(switch)
        box.append(top)

        actions = Gtk.Box(spacing=8)
        credential = Gtk.Label(
            label=f"{_('Credential')}: "
            f"{_('configured') if self._configured.get(slug, False) else _('not configured')}",
            xalign=0,
        )
        actions.append(credential)
        remove_credential = Gtk.Button(label=_("Remove credential"))
        remove_credential.set_visible(self._configured.get(slug, False))
        remove_credential.connect("clicked", self._on_remove_credential, slug)
        actions.append(remove_credential)
        test_button = Gtk.Button(label=_("Test connection"))
        test_button.connect("clicked", self._on_test_connection, slug)
        actions.append(test_button)
        test_status = Gtk.Label(label="", xalign=0)
        test_status.add_css_class("dim-label")
        actions.append(test_status)
        edit_button = Gtk.Button(label=_("Edit"))
        edit_button.connect("clicked", self._on_edit_clicked, slug)
        actions.append(edit_button)
        remove_button = Gtk.Button(label=_("Remove"))
        remove_button.connect("clicked", self._on_remove_clicked, slug)
        actions.append(remove_button)
        box.append(actions)

        confirm = Gtk.Box(spacing=8)
        confirm_label = Gtk.Label(label=_("Remove profile?"), xalign=0)
        confirm_note = Gtk.Label(
            label=_("This removes the profile and its Moira Keyring credential."), xalign=0
        )
        confirm_note.add_css_class("dim-label")
        confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        confirm_box.append(confirm_label)
        confirm_box.append(confirm_note)
        confirm.append(confirm_box)
        confirm_remove = Gtk.Button(label=_("Remove"))
        confirm_remove.add_css_class("destructive-action")
        confirm_remove.connect("clicked", self._on_confirm_remove, slug)
        confirm.append(confirm_remove)
        confirm_cancel = Gtk.Button(label=_("Cancel"))
        confirm_cancel.connect("clicked", self._on_cancel_remove, slug)
        confirm.append(confirm_cancel)
        confirm.set_visible(False)
        box.append(confirm)

        self._row_widgets[slug] = {
            "switch": switch,
            "credential": credential,
            "remove_credential": remove_credential,
            "test": test_button,
            "test_status": test_status,
            "edit": edit_button,
            "remove": remove_button,
            "confirm": confirm,
            "confirm_remove": confirm_remove,
            "confirm_cancel": confirm_cancel,
        }
        self._refresh_row_confirm(slug)
        return box

    def _refresh_row_confirm(self, slug: str) -> None:
        widgets = self._row_widgets.get(slug)
        if widgets is None:
            return
        confirming = self._pending_removal == slug
        widgets["confirm"].set_visible(confirming)
        widgets["remove_credential"].set_visible(
            not confirming and self._configured.get(slug, False)
        )
        widgets["test"].set_visible(not confirming)
        widgets["edit"].set_visible(not confirming)
        widgets["remove"].set_visible(not confirming)

    # ── Row actions ──

    def _on_enabled_toggled(self, switch: Gtk.Switch, state: bool, slug: str) -> bool:
        if self._shutdown or self._rendering:
            return False
        profile = next((p for p in self._profiles if p.slug == slug), None)
        if profile is None:
            return False
        updated = replace(profile, enabled=state)
        self._request_op(ProfileOp("save_profile", profile=updated))
        return False

    def _on_edit_clicked(self, _button: Any, slug: str) -> None:
        if self._shutdown:
            return
        profile = next((p for p in self._profiles if p.slug == slug), None)
        if profile is not None:
            self._show_form(profile)

    def _on_remove_clicked(self, _button: Any, slug: str) -> None:
        if self._shutdown:
            return
        self._pending_removal = slug
        self._refresh_row_confirm(slug)

    def _on_confirm_remove(self, _button: Any, slug: str) -> None:
        self._pending_removal = None
        if self._shutdown:
            return
        self._request_op(ProfileOp("remove_profile", slug=slug))

    def _on_cancel_remove(self, _button: Any, slug: str) -> None:
        if self._pending_removal == slug:
            self._pending_removal = None
        self._refresh_row_confirm(slug)

    def _on_remove_credential(self, _button: Any, slug: str) -> None:
        if self._shutdown:
            return
        self._request_op(ProfileOp("remove_credential", slug=slug))

    # ── Connection test ──

    def _on_test_connection(self, _button: Any, slug: str) -> None:
        """Explicit click only; runs one bounded, read-only test per
        request. Disabled profiles remain testable (the toggle controls
        usage, not testability); editing, renaming, removing, disabling
        or closing the profile discards any in-flight result."""
        if self._shutdown:
            return
        widgets = self._row_widgets.get(slug)
        if widgets is None:
            return
        profile = next((p for p in self._profiles if p.slug == slug), None)
        if profile is None:
            return
        widgets["test_status"].set_text(_("Testing…"))
        token = (slug, self._row_epoch, id(widgets))
        # The coordinator completes every request — including submit
        # rejections, which reset the row to a translated sanitized
        # failure through the callback — so a row is never stuck.
        self._connection_coordinator.request(profile, token, self._publish_test_result)

    def _publish_test_result(self, token: Any, result: ConnectionResult) -> None:
        GLib.idle_add(self._apply_test_result, token, result)

    def _apply_test_result(self, token: Any, result: ConnectionResult) -> None:
        slug, epoch, widgets_id = token
        widgets = self._row_widgets.get(slug)
        if widgets is None or id(widgets) != widgets_id or epoch != self._row_epoch:
            return  # edited, renamed, removed, disabled or closed: discard
        widgets["test_status"].set_text(_(_CONNECTION_STATE_LABELS[result.state]))

    # ── Add/edit form ──

    def _build_form(self) -> None:
        self.form_title = Gtk.Label(label=_("Add provider"), xalign=0)
        self.form_title.add_css_class("heading")
        self._form_area.append(self.form_title)

        self.slug_entry = Gtk.Entry(placeholder_text="deepseek-main")
        self._form_area.append(self._labeled(_("Slug"), self.slug_entry))
        self.label_entry = Gtk.Entry()
        self._form_area.append(self._labeled(_("Label"), self.label_entry))
        self.kind_dropdown = Gtk.DropDown.new_from_strings(
            [_(_KIND_LABELS[kind]) for kind in _KIND_ORDER]
        )
        self._form_area.append(self._labeled(_("Kind"), self.kind_dropdown))
        self.model_entry = Gtk.Entry(placeholder_text="deepseek-chat")
        self._form_area.append(self._labeled(_("Model"), self.model_entry))
        self.enabled_switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self._form_area.append(self._labeled(_("Enabled"), self.enabled_switch))
        self.base_url_entry = Gtk.Entry(placeholder_text="https://api.deepseek.com/v1")
        self._form_area.append(self._labeled(_("API base URL"), self.base_url_entry))
        self.hermes_label_entry = Gtk.Entry()
        self._form_area.append(self._labeled(_("Hermes label"), self.hermes_label_entry))
        self.api_key_entry = Gtk.PasswordEntry()
        self._form_area.append(self._labeled(_("API key"), self.api_key_entry))
        api_note = Gtk.Label(label=_("Leave blank to keep the current credential."), xalign=0)
        api_note.add_css_class("dim-label")
        self._form_area.append(api_note)
        self.slug_change_note = Gtk.Label(
            label=_("Changing the slug removes the previous profile and its credential."), xalign=0
        )
        self.slug_change_note.add_css_class("dim-label")
        self.slug_change_note.set_visible(False)
        self._form_area.append(self.slug_change_note)

        self.form_error = Gtk.Label(xalign=0)
        self.form_error.add_css_class("error")
        self._form_area.append(self.form_error)

        buttons = Gtk.Box(spacing=8)
        self.form_save_button = Gtk.Button(label=_("Save"))
        self.form_save_button.add_css_class("suggested-action")
        self.form_save_button.connect("clicked", self._on_form_save)
        buttons.append(self.form_save_button)
        self.form_cancel_button = Gtk.Button(label=_("Cancel"))
        self.form_cancel_button.connect("clicked", lambda *_args: self._show_list())
        buttons.append(self.form_cancel_button)
        self._form_area.append(buttons)

    @staticmethod
    def _labeled(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(label=label, xalign=0))
        box.append(widget)
        return box

    def _show_form(self, profile: ProviderProfile | None) -> None:
        if self._shutdown:
            return
        self._editing_slug = profile.slug if profile else None
        self._pending_removal = None
        self._list_area.set_visible(False)
        self._form_area.set_visible(True)
        self.form_title.set_text(_("Edit provider") if profile else _("Add provider"))
        self.slug_entry.set_text(profile.slug if profile else "")
        self.label_entry.set_text(profile.label if profile else "")
        self._set_kind(profile.kind if profile else ProviderKind.DEEPSEEK)
        self.model_entry.set_text(profile.model if profile else "")
        self.enabled_switch.set_active(profile.enabled if profile else True)
        self.base_url_entry.set_text(profile.base_url if profile else "")
        self.hermes_label_entry.set_text(profile.hermes_label if profile else "")
        self.api_key_entry.set_text("")
        self.form_error.set_text("")
        self.slug_change_note.set_visible(profile is not None)

    def _set_kind(self, kind: ProviderKind) -> None:
        self.kind_dropdown.set_selected(_KIND_ORDER.index(kind))

    def _form_kind(self) -> ProviderKind:
        return _KIND_ORDER[self.kind_dropdown.get_selected()]

    def _on_form_save(self, *_args: Any) -> None:
        if self._shutdown:
            return
        slug = self.slug_entry.get_text().strip()
        label = self.label_entry.get_text().strip()
        model = self.model_entry.get_text().strip()
        base_url = self.base_url_entry.get_text().strip()
        hermes_label = self.hermes_label_entry.get_text().strip()
        kind = self._form_kind()
        enabled = self.enabled_switch.get_active()
        api_key = self.api_key_entry.get_text()
        try:
            profile = ProviderProfile(
                slug=slug,
                label=label,
                kind=kind,
                model=model,
                enabled=enabled,
                base_url=base_url,
                hermes_label=hermes_label,
            )
        except ValueError as exc:
            self.form_error.set_text(_(_form_error_key(exc)))
            return
        existing = {p.slug for p in self._profiles}
        if self._editing_slug:
            existing.discard(self._editing_slug)
        if profile.slug in existing:
            self.form_error.set_text(_("Slug already in use."))
            return
        others = [p for p in self._profiles if p.slug != self._editing_slug]
        if len(others) + 1 > MAX_PROFILES:
            self.form_error.set_text(_("Too many profiles."))
            return
        self.form_error.set_text("")
        op = ProfileOp(
            "save_profile",
            profile=profile,
            old_slug=self._editing_slug or "",
            credential=api_key,
        )
        self._request_op(op)

    def shutdown(self) -> None:
        """Idempotent: stop accepting operations, drop the pending one and
        flag queued-not-started ``_run_op`` calls so they never write."""
        self._shutdown = True
        self._shutdown_event.set()
        self._pending_op = None
        self._connection_coordinator.cancel()
