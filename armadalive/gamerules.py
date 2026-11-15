"""Remembers a power profile and FPS limit for a game, a whole system, every other game (the base rule) or for when no game runs
(the idle rule), and applies it when that game starts or the last game ends."""
import json, logging, math, os
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
        self._checked = False
        self._scopes = self._current_scopes(nowplaying.state)
        self.history = None      # set by the app: lets a rule show the name of its game, not only the key
        nowplaying.changed.connect(self._check)
        self._identities.changed.connect(self._check)
        self._watch_legacy_rules()
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

    def _emulator(self, cur):
        """Just before a game starts (while it is selected in ES-DE) its emulator gets the saved resolution; a game without one
        gets back what the emulator had before. shortcut: only the first override remembers the original value."""
        system = cur.get("system")
        if self._pending_legacy_rule(cur):
            return
        if cur.get("kind") != "game" or cur.get("playing") or system not in emusettings.SPECS:
            return
        rule = self._rules.get(self._game_key(cur)) or self._rules.get(self._system_key(cur)) or {}
        if rule.get("res"):
            if system not in self._base:
                self._base[system] = emusettings.read(system)
            if self._base[system] is not None:
                emusettings.write(system, rule["res"])
        elif self._base.get(system) is not None:
            emusettings.write(system, self._base.pop(system))

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
        if self._on:
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
        for key, label in ((self._game_key(cur), "game"), (self._system_key(cur), "system"), (BASE, "base")):
            if key in self._rules:
                self._apply(self._rules[key], f"{cur.get('title') or cur.get('key')}" if label == "game" else label)
                return
