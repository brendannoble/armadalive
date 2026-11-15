"""Remembers a power profile and FPS limit for a game, a whole system, every other game (the base rule) or for when no game runs
(the idle rule), and applies it when that game starts or the last game ends."""
import json, logging, math, os, time
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from . import emusettings, persistence, nowplaying as esde

LOG = logging.getLogger(__name__)
CONFIG = os.path.expanduser("~/.config/armadalive/game_rules.json")


IDLE, BASE = "default:idle", "default:base"   # rule keys that belong to no game


def _valid_base(base):
    return isinstance(base, dict) and all(value is None or isinstance(value, str) for value in base.values())


def validate_settings(saved):
    if not isinstance(saved, dict):
        return False
    rules, base = saved.get("rules", {}), saved.get("base", {})
    if not isinstance(rules, dict) or not _valid_base(base):
        return False
    for key, rule in rules.items():
        if not isinstance(key, str) or ":" not in key or not isinstance(rule, dict):
            return False
        if not all(rule.get(field) is None or isinstance(rule.get(field), str) for field in ("profile", "title")):
            return False
        fps, res = rule.get("fps", 0), rule.get("res", 0)
        # Existing rules accept numeric frame limits and numeric text. Resolution
        # values cycle arithmetically; retain finite legacy values outside the UI list.
        if fps is not None and not isinstance(fps, (int, float, str)):
            return False
        try:
            if fps:
                int(fps)
        except (ValueError, OverflowError):
            return False
        if not isinstance(res, (int, float)) or (isinstance(res, float) and not math.isfinite(res)):
            return False
    return True


def _target(spec):
    return os.path.realpath(spec[0]), spec[1], spec[2]


def _read_undo(path):
    if os.path.islink(path):
        raise ValueError("emulator recovery file is a symbolic link")
    try:
        with open(path) as stream:
            entries = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(entries, list):
        raise ValueError("invalid emulator recovery file")
    undo = {}
    for entry in entries:
        if (not isinstance(entry, list) or len(entry) != 4
                or not all(isinstance(value, str) for value in entry)
                or not os.path.isabs(entry[0]) or any("\0" in value for value in entry)
                or any(char in entry[3] for char in "\r\n")
                or tuple(entry[:3]) in undo):
            raise ValueError("invalid emulator recovery entry")
        undo[tuple(entry[:3])] = entry[3]
    return undo


class GameRules(persistence.OwnerWriter, QObject):
    changed = Signal()
    notice = Signal(str, str)   # saved | removed | loaded | failed, and what it was about

    def __init__(self, nowplaying, gamescope, system):
        super().__init__()
        self._np, self._gs, self._sys = nowplaying, gamescope, system
        self._identities = nowplaying.identities
        self._file_version = esde._stamp(CONFIG)
        saved = persistence.read_json(CONFIG, validate_settings, {})
        self._saved = saved
        self._rules, self._on = saved.get("rules", {}), bool(saved.get("enabled", True))
        self._base = saved.get("base", {})
        self._base_error = ""
        # Rule recovery and device recovery stock have separate validity. A
        # damaged rule must not discard newer originals from the primary file.
        try:
            with open(CONFIG) as stream:
                primary = json.load(stream)
            if isinstance(primary, dict) and "base" in primary:
                if _valid_base(primary["base"]):
                    self._base = primary["base"]
                else:
                    self._base_error = "Cannot save game rules: invalid device-local emulator originals"
        except (OSError, ValueError, RecursionError):
            pass  # Keep originals from the validated recovery copy, if available.
        self._check_deferred = self._restoring = False
        self._applied, self._loaded = "", ""
        self._pending_rule = None
        self._undo_path = os.path.join(os.path.dirname(CONFIG), "emulator_undo.json")
        self._undo, self._undo_uncertain = None, False
        self._launch_key, self._launch_deadline = "", 0
        self._expired_launch_key = ""
        self._launch_hold = QTimer(self)
        self._launch_hold.setSingleShot(True)
        self._launch_hold.timeout.connect(self._launch_unobserved)
        self._checked = False
        self._scopes = self._current_scopes(nowplaying.state)
        self.history = None      # set by the app: lets a rule show the name of its game, not only the key
        nowplaying.changed.connect(self._check)
        self._identities.changed.connect(self._check)
        self._watch_legacy_rules()
        if self._prepare_undo():
            self._restore_emulators(keep=self._running_target())
        system.profileFinished.connect(self._profile_finished)
        gamescope.fpsLimitFinished.connect(self._fps_finished)
        system.profileRequested.connect(self._profile_requested)
        gamescope.fpsLimitRequested.connect(self._fps_requested)

    @Property("QVariant", notify=changed)
    def state(self):
        cur = self._np.state
        return dict(enabled=self._on, rules=sorted((dict(v, key=k, **self._describe(k, v)) for k, v in self._rules.items()), key=lambda r: (r["kind"], r["name"].lower())),
                    gameKey=self._game_key(cur), systemKey=self._system_key(cur), resOk=cur.get("system") in emusettings.SPECS,
                    idleKey=IDLE, baseKey=BASE, loaded=self._loaded, writesPaused=self._writes_paused)

    def _describe(self, key, rule):
        """What the rules list shows for a key: its kind and the name of the game or system, never only the Steam id."""
        if key in (IDLE, BASE):
            return dict(kind="idle" if key == IDLE else "base", name="")
        kind, _, rest = key.partition(":")
        if kind == "game":
            return dict(kind="game", name=rule.get("title") or (self.history.title_of(rest) if self.history else "") or rest)
        return dict(kind="system", name=rest)

    def _current_scopes(self, state):
        return self._game_key(state), self._system_key(state), state.get("system") in emusettings.SPECS

    @staticmethod
    def _canonical_game_key(state):
        return f"game:{state['key']}" if state.get("key") else ""

    def _legacy_game_key(self, state):
        legacy = esde._legacy_key_for_game(state.get("key", ""))
        return f"game:{legacy}" if legacy and self._identities.identity(legacy) == state["key"] else ""

    def _game_key(self, state):
        canonical = self._canonical_game_key(state)
        legacy = self._legacy_game_key(state)
        return legacy if canonical not in self._rules and legacy in self._rules else canonical

    def _pending_legacy_rule(self, state):
        if self._canonical_game_key(state) in self._rules:
            return ""
        legacy = esde._legacy_key_for_game(state.get("key", ""))
        return f"game:{legacy}" if (f"game:{legacy}" in self._rules
                                   and self._identities.identity(legacy) is None) else ""

    def _watch_legacy_rules(self):
        self._identities.watch_rules(key[5:] for key in self._rules if key.startswith("game:"))

    def _migrate_game_rules(self):
        if self._file_version != esde._stamp(CONFIG):
            return False
        previous = self._rules
        updated = dict(previous)
        for key, (target, _) in self._identities.rule_aliases().items():
            legacy, canonical = f"game:{key}", f"game:{target}"
            if legacy in updated and canonical not in updated:
                updated[canonical] = updated.pop(legacy)
        if updated == previous:
            return False
        self._rules = updated
        try:
            self._save()
        except (OSError, ValueError) as error:
            self._rules = previous
            # Replacement can succeed before directory sync reports failure.
            # Retry only that exact candidate, never a different restored file.
            expected = dict(self._saved, enabled=self._on, rules=updated, base=self._base)
            try:
                with open(CONFIG) as stream:
                    content = json.load(stream)
                    info = os.fstat(stream.fileno())
                version = info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
                if content == expected and version == esde._stamp(CONFIG):
                    self._file_version = version
            except (OSError, ValueError, RecursionError):
                pass
            LOG.warning("Could not migrate game rules: %s", error)
            return False
        return True

    @staticmethod
    def _system_key(state):
        return f"system:{state['system']}" if state.get("system") and state.get("kind") == "game" else ""

    def pauseWrites(self, *, restoring=False):
        self._restoring = restoring
        super().pauseWrites(restoring=restoring)
        # Emulator originals can change on the owner thread between explicit
        # rule edits. Persist them before backup captures the local rollback state.
        snapshot = dict(self._saved, enabled=self._on, rules=self._rules, base=self._base)
        try:
            with open(CONFIG) as stream:
                if json.load(stream) == snapshot:
                    return
        except FileNotFoundError:
            if not self._saved and not self._rules and not self._base and self._on:
                return
        except (ValueError, RecursionError):
            pass
        self._save()

    def _save(self):
        if self._base_error:
            raise ValueError(self._base_error)
        saved = dict(self._saved, enabled=self._on, rules=self._rules, base=self._base)
        if not validate_settings(saved):
            raise ValueError("invalid game rules")
        self._file_version = persistence.write_json(CONFIG, saved, validate=validate_settings, indent=1,
                               follow_symlinks=True, require_writable=True, allow_nan=True)
        self._watch_legacy_rules()
        self.changed.emit()

    @staticmethod
    def _label(key, cur):
        if key == IDLE:
            return "idle"
        if key == BASE:
            return "base"
        return cur.get("title") or key.split(":", 1)[1] if key.startswith("game:") else key.split(":", 1)[1]

    @Slot(str)
    def toggle(self, scope):
        """Save the current profile and FPS limit for this game, system, as the idle or base rule, or forget the rule if there is one."""
        if self._writes_paused:
            return
        cur = self._np.state
        key = {"game": self._game_key(cur), "system": self._system_key(cur), "idle": IDLE, "base": BASE}.get(scope, "")
        if not key:
            return
        if key in self._rules:
            del self._rules[key]
            if scope == "game" and key == self._canonical_game_key(cur):
                self._rules.pop(self._legacy_game_key(cur), None)
            code = "removed"
        else:
            self._rules[key] = dict(profile=self._sys.stats.get("profile", ""), fps=self._gs.state.get("limit", 0))
            if scope == "game" and cur.get("title"):
                self._rules[key]["title"] = cur["title"]
            code = "saved"
        self._save()
        self.notice.emit(code, self._label(key, cur) + (f" · {self._summary(self._rules[key])}" if code == "saved" else ""))

    @staticmethod
    def _summary(rule):
        return (rule.get("profile") or "–") + (f", {rule['fps']} FPS" if rule.get("fps") else "")

    @Slot()
    def cycleRes(self):
        """Step this game's render resolution through off, 1x ... 5x; a game without a rule gets one that only holds the resolution."""
        if self._writes_paused:
            return
        key = self._game_key(self._np.state)
        if not key or self._np.state.get("system") not in emusettings.SPECS:
            return
        rule = self._rules.setdefault(key, dict(profile="", fps=0))
        if self._np.state.get("title"):
            rule["title"] = self._np.state["title"]
        rule["res"] = (rule.get("res", 0) + 1) % (emusettings.MAX + 1)
        self._save()

    @Slot(str)
    def remove(self, key):
        if self._writes_paused:
            return
        if self._rules.pop(key, None) is not None:
            self._save()
            self.notice.emit("removed", self._label(key, self._np.state))

    @Slot(bool)
    def setEnabled(self, on):
        if self._writes_paused:
            return
        self._on = bool(on)
        if not self._on:
            self._pending_rule = None
        self._save()
        if not self._on:
            self._launch_key = ""
            self._launch_hold.stop()
            if self._prepare_undo():
                running = self._running_target()
                self._restore_emulators(keep=running)
                if self._undo and (running == "all" or running in self._undo):
                    self.notice.emit("resolutionRestorePending", self._np.state.get("title", ""))

    def _write_undo(self, undo):
        # Replacement/unlink may become visible before directory sync fails.
        self._undo_uncertain = True
        if undo:
            persistence.write_json(self._undo_path, [[*target, original] for target, original in undo.items()])
        else:
            persistence.remove(self._undo_path)
        self._undo, self._undo_uncertain = undo, False

    def _prepare_undo(self):
        if self._writes_paused or self._base_error or not self._np.processes_known:
            return False
        try:
            if self._undo is None:
                self._undo = _read_undo(self._undo_path)
            if self._undo_uncertain:
                self._write_undo(self._undo)
            originals, handed_off = {}, set()
            for system, original in self._base.items():
                if original is None or system not in emusettings.SPECS:
                    continue  # Retain device-local values we cannot hand off.
                if any(char in original for char in "\r\n\0"):
                    raise ValueError("invalid legacy emulator original")
                target = _target(emusettings.SPECS[system])
                if not os.path.isfile(target[0]):
                    continue  # Its original remains local until the target is available.
                if target not in self._undo:
                    if target in originals and originals[target] != original:
                        raise ValueError("conflicting legacy emulator originals")
                    originals[target] = original
                handed_off.add(system)
            if originals:
                self._write_undo({**self._undo, **originals})
            if handed_off:
                previous = self._base
                self._base = {key: value for key, value in previous.items() if key not in handed_off}
                try:
                    saved = dict(self._saved, enabled=self._on, rules=self._rules, base=self._base)
                    if not validate_settings(saved):
                        raise ValueError("invalid game rules")
                    # Handoff edits each copy's recovery stock without replacing
                    # the backup's independently useful previous rules.
                    self._file_version = persistence.write_json(
                        CONFIG, saved, indent=1, follow_symlinks=True,
                        require_writable=True, allow_nan=True)
                    self._retire_legacy_backup()
                except (OSError, ValueError):
                    self._base = previous
                    raise
            elif self._undo:
                # Restart after primary handoff but before backup-copy handoff.
                self._retire_legacy_backup()
            if self._undo:
                # A prior handoff replacement may already be visible after a
                # failed sync. Durability is still required on a no-change retry.
                persistence._sync_directory(os.path.dirname(os.path.realpath(CONFIG)))
        except (OSError, ValueError, RecursionError) as error:
            LOG.warning("Could not prepare emulator recovery: %s", error)
            return False
        return True

    def _retire_legacy_backup(self):
        # The reader falls back beside the lexical path; the validated writer
        # keeps a copy beside the resolved primary. Preserve each copy's rules.
        paths = {os.path.realpath(os.fspath(CONFIG) + ".bak"),
                 os.path.realpath(os.path.realpath(CONFIG) + ".bak")}
        for path in sorted(paths):
            try:
                with open(path) as stream:
                    saved = json.load(stream)
            except (FileNotFoundError, ValueError, RecursionError):
                continue  # An invalid copy cannot be selected by the reader.
            if not validate_settings(saved):
                continue
            previous = saved.get("base", {})
            remaining = {system: original for system, original in previous.items()
                         if original is None or system not in emusettings.SPECS
                         or _target(emusettings.SPECS[system]) not in self._undo}
            if remaining != previous:
                persistence.write_json(path, dict(saved, base=remaining), indent=1,
                                       follow_symlinks=True, require_writable=True, allow_nan=True)
            persistence._sync_directory(os.path.dirname(path))

    def _running_target(self):
        cur = self._np.state
        if cur.get("playing") and cur.get("system") in emusettings.SPECS:
            return _target(emusettings.SPECS[cur["system"]])
        if getattr(self._np, "running", False):
            return "all"  # Steam's overlay can conceal the running emulator.
        return None

    def _restore_emulators(self, keep=None, *, also_keep=None):
        if not self._undo or keep == "all":
            return True
        supported = {_target(spec) for spec in emusettings.SPECS.values()}
        protected = {keep, also_keep}
        remaining = dict(self._undo)
        for target, original in self._undo.items():
            if target in protected:
                continue
            try:
                if target not in supported or not emusettings.update_setting((*target, "{}"), lambda _: original):
                    raise OSError("emulator configuration unavailable")
                persistence._sync_directory(os.path.dirname(target[0]))
                del remaining[target]
            except (OSError, ValueError) as error:
                LOG.warning("Could not restore emulator resolution: %s", error)
        if remaining != self._undo:
            try:
                self._write_undo(remaining)
            except (OSError, ValueError) as error:
                LOG.warning("Could not clear emulator recovery: %s", error)
        return not self._undo_uncertain and all(target in protected for target in self._undo)

    def _resolution(self, cur):
        rule = self._rules.get(self._game_key(cur)) or self._rules.get(self._system_key(cur)) or {}
        return rule.get("res")

    def _resolution_pending(self, cur):
        pending = self._pending_legacy_rule(cur)
        if not pending or not self._rules[pending]:
            return False
        fallback = self._rules.get(self._system_key(cur), {}).get("res") or 0
        return (self._rules[pending].get("res") or 0) != fallback

    def prepare_esde_launch(self, system, path, title):
        """None means pending proof, empty text means ready, other text blocks launch."""
        if system not in emusettings.SPECS:
            return ""
        if self._writes_paused:
            return "Game rules are paused during backup"
        cur, _ = esde._esde_selection(dict(event="game-select", system=system, path=path, name=title))
        if not cur.get("key"):
            return "Could not identify the selected game"
        self._identities.describe(cur)
        if self._on and self._resolution_pending(cur):
            return None
        if not self._np.processes_known:
            # A known no-op needs no process observation. Recovery evidence or a
            # requested override must wait for the existing essential scanner.
            needs_change = (self._on and self._resolution(cur)) or self._base or self._undo or os.path.lexists(self._undo_path)
            return None if needs_change else ""
        running = self._running_target()
        if running is not None:
            if not self._prepare_undo():
                return "Could not prepare emulator resolution recovery"
            target = _target(emusettings.SPECS[system])
            resolution = self._resolution(cur) if self._on else None
            if resolution and (type(resolution) is not int or not 1 <= resolution <= emusettings.MAX):
                return "Choose a supported emulator resolution"
            desired = emusettings.SPECS[system][3].format(resolution) if resolution else None
            protected = (set(self._undo) | {target}) if running == "all" else ({target} if running == target else set())
            for active in protected:
                expected = desired if active == target and desired is not None else self._undo.get(active)
                if expected is not None and emusettings.read_setting((*active, "{}")) != expected:
                    return "Close the running game before changing emulator resolution"
        self._expired_launch_key = ""
        problem = self._emulator(cur)
        if not problem and self._on and self._resolution(cur):
            if self._launch_key != cur["key"]:
                self._launch_key = cur["key"]
                self._launch_deadline = time.monotonic() + 25
                self._launch_hold.start(25000)
        return problem

    @Slot()
    def _launch_unobserved(self):
        self._expired_launch_key = self._launch_key
        self._launch_key = ""
        self._launch_hold.stop()
        if self._writes_paused:
            self._check_deferred = True
            return
        if self._prepare_undo():
            self._restore_emulators(keep=self._running_target())

    def _emulator(self, cur):
        if self._writes_paused:
            return "Game rules are paused during backup"
        running = self._running_target()
        if running == "all":
            return ""
        if not self._prepare_undo():
            return "Could not prepare emulator resolution recovery"
        spec = emusettings.SPECS.get(cur.get("system")) if cur.get("kind") == "game" else None
        resolution = self._resolution(cur) if self._on and spec and not self._resolution_pending(cur) else None
        target = _target(spec) if spec else None
        if not self._restore_emulators(keep=running, also_keep=target if resolution else None):
            return "Could not restore emulator resolution"
        if target == running or cur.get("playing") or not resolution:
            return ""
        # Preserve compatible saved values, but do not send invalid resolutions
        # to an emulator. No configuration migration is implied by refusal.
        if type(resolution) is not int or not 1 <= resolution <= emusettings.MAX:
            return "Choose a supported emulator resolution"
        value = spec[3].format(resolution)

        def remember(original):
            if original is None:
                raise ValueError("emulator resolution option is missing")
            if original != value and (self._undo_uncertain or target not in self._undo):
                self._write_undo({target: original, **self._undo})
            return value

        try:
            if emusettings.update_setting((*target, spec[3]), remember):
                return ""
        except (OSError, ValueError) as error:
            LOG.warning("Could not apply emulator resolution: %s", error)
        return "Could not apply emulator resolution"

    def _apply(self, rule, label):
        self._pending_rule = None
        detail = f"{label} · {self._summary(rule)}"
        requests = {}
        if rule.get("profile"):
            requests["profile"] = self._sys.setProfile(rule["profile"])
        if rule.get("fps"):
            requests["fps"] = self._gs.setFpsLimit(int(rule["fps"]))
        self._pending_rule = (requests, set(requests), detail) if requests else None
        if self._loaded:
            self._loaded = ""
            self.changed.emit()
        # Resolution-only rules retain their existing preparation path in _emulator.
        if not requests:
            self._show_loaded(detail)

    def _show_loaded(self, detail):
        self._loaded = detail
        self.changed.emit()
        self.notice.emit("loaded", detail)

    @Slot(object, bool)
    def _profile_finished(self, request, succeeded):
        self._finish_request("profile", request, succeeded)

    @Slot(object, bool)
    def _fps_finished(self, request, succeeded):
        self._finish_request("fps", request, succeeded)

    @Slot(object)
    def _profile_requested(self, request):
        self._superseded("profile", request)

    @Slot(object)
    def _fps_requested(self, request):
        self._superseded("fps", request)

    def _superseded(self, channel, request):
        if self._pending_rule is not None:
            requests, _, _ = self._pending_rule
            if channel in requests and requests[channel] != request:
                self._pending_rule = None

    def _finish_request(self, channel, request, succeeded):
        if self._pending_rule is None:
            return
        requests, remaining, detail = self._pending_rule
        if requests.get(channel) != request:
            return
        if not succeeded:
            self._pending_rule = None
            self.notice.emit("failed", detail)
        else:
            remaining.discard(channel)
            if not remaining:
                self._pending_rule = None
                self._show_loaded(detail)

    def resumeWrites(self):
        super().resumeWrites()
        if self._restoring:
            self._file_version = esde._stamp(CONFIG)
            self._restoring = False
        if self._check_deferred:
            self._check_deferred = False
            self._check()

    def _check(self):
        if self._writes_paused:
            self._check_deferred = True
            return
        cur = self._np.state
        migrated = self._migrate_game_rules()
        scopes = self._current_scopes(cur)
        if migrated or scopes != self._scopes:
            self._scopes = scopes
            self.changed.emit()
        if self._launch_key and time.monotonic() >= self._launch_deadline:
            self._launch_unobserved()
        if self._launch_key and cur.get("playing") and cur.get("key") == self._launch_key:
            self._launch_key = ""
            self._launch_hold.stop()
        if self._expired_launch_key != cur.get("key"):
            self._expired_launch_key = ""
        if not self._launch_key:
            if self._expired_launch_key:
                if self._prepare_undo():
                    self._restore_emulators(keep=self._running_target())
            else:
                self._emulator(cur)
        if not cur.get("playing"):
            # Steam's own UI in front of a running game is not "no game": only when the process is gone does the idle rule apply
            if self._applied and not getattr(self._np, "running", False):
                self._applied = ""      # the next game that starts gets its rule again
                self._pending_rule = None
                if self._on and IDLE in self._rules:
                    self._apply(self._rules[IDLE], "idle")
            return
        game = cur.get("key", "")
        if game != self._applied:
            self._applied = game
            self._checked = False
            self._pending_rule = None
        if not self._on or not game or self._checked or self._pending_legacy_rule(cur):
            return
        self._checked = True
        system = cur.get("system")
        if system in emusettings.SPECS:
            spec = emusettings.SPECS[system]
            resolution = self._resolution(cur)
            expected = spec[3].format(resolution) if resolution else (self._undo or {}).get(_target(spec))
            if expected is not None and emusettings.read(system) != expected:
                self.notice.emit("resolutionNextLaunch", cur.get("title") or game)
        for key, label in ((self._game_key(cur), "game"), (self._system_key(cur), "system"), (BASE, "base")):
            if key in self._rules:
                self._apply(self._rules[key], f"{cur.get('title') or cur.get('key')}" if label == "game" else label)
                return
