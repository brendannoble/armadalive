"""What is being played right now: follows ES-DE through the state file its event hook writes."""
import glob, json, os, random, re, time
import xml.etree.ElementTree as ET
from functools import lru_cache
from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from .poller import Poller
from . import steamlib

ES = os.path.expanduser(os.environ.get("ARMADALIVE_ESDE_DIR", "~/ES-DE"))
MEDIA, GAMELISTS = ES + "/downloaded_media", ES + "/gamelists"
CHEAT_DIRS = [os.path.expanduser(p) for p in (
    "~/.var/app/org.libretro.RetroArch/config/retroarch/cheats", "~/.config/retroarch/cheats")]
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
STATE = os.path.join(RUNTIME, "armadalive", "state.json")

STEAM_LIBRARIES = [os.path.expanduser(p) for p in ("~/.local/share/Steam", "/run/media/armada/EXT")]
# emulators recognised by the name of their executable (RetroArch, AppImages, Flatpaks); a file manager is not one of them
EMULATORS = ("retroarch", "eden", "azahar", "cemu", "armsx2", "pcsx2", "dolphin-emu", "ppsspp", "rpcs3", "xemu", "duckstation",
             "vita3k", "mgba", "melonds", "flycast", "redream", "xenia", "shadps4", "citra", "yuzu", "ryujinx", "mednafen", "scummvm", "dosbox")
_cheats = {}
LUDO = os.path.expanduser("~/.config/ludo/library_snapshot.json")


def _first(pattern):
    hit = sorted(glob.glob(pattern))
    return hit[0] if hit else ""


def _url(path):
    return QUrl.fromLocalFile(path).toString() if path else ""


def _system_logo(system):
    return _first(glob.escape(ES) + f"/themes/*/_inc/systems/logos/{glob.escape(system)}.svg")


def _file_stamp(path):
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def _rom_configuration():
    from . import esdelib

    return _rom_dir(), esdelib.cached_systems(ES)

def _system_folder(system, root, definition):
    from .esdelib import system_folder

    return os.path.abspath(system_folder(system, root, definition))

def _rom_folder(system):
    root, definitions = _rom_configuration()
    return _system_folder(system, root, definitions.get(system))

def _relative_below(path, folder):
    prefix = folder.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else ""

def _same_file(path, candidate):
    try:
        return os.path.samefile(path, candidate)
    except OSError:
        return False

def rom_relative(system, path, folder=None):
    """Normalize a ROM path under its system folder, or return empty if unknown."""
    expanded = os.path.expanduser(path)
    path = os.path.normpath(expanded)
    if not os.path.isabs(expanded):
        return path if path not in (".", "..") and not path.startswith("../") else ""
    folder = folder if folder is not None else _rom_folder(system)
    relative = _relative_below(path, folder) if ".." not in expanded.split("/") else ""
    if relative:
        return relative
    physical = os.path.realpath(expanded)
    if relative := _relative_below(physical, os.path.realpath(folder)):
        return relative
    # Bind mounts need an inode check once the other spelling supplies a candidate.
    alternate = physical.partition(f"/roms/{system}/")[2]
    if alternate and _same_file(expanded, os.path.join(folder, alternate)):
        return alternate
    return ""

def rom_key(system, relative):
    return f"esde:{system}/{relative}" if relative else ""

def legacy_key_parts(key):
    system, sep, stem = key.partition("/")
    if (sep and stem and "/" not in stem and re.fullmatch(r"[\w.-]+", system)
            and system not in (".", "..", "steam", "ludo")):
        return system, stem
    return None

def _raise_walk_error(error):
    raise error

def _legacy_roms(folder, extensions, entries, basenames, stems, versions=None):
    """Map old basename stems to their ROM path only when one candidate exists."""
    from .esdelib import rom_files

    ambiguous = {os.path.splitext(base)[0] for base, relative in basenames.items() if not relative}
    stems = stems - ambiguous
    if not stems:
        return {}
    matches = {}
    try:
        for relative in rom_files(folder, extensions or None, onerror=_raise_walk_error, stems=stems,
                                  versions=versions, proof=True):
            stem = os.path.splitext(os.path.basename(relative))[0]
            matches[stem] = relative if stem not in matches else ""
    except OSError:
        return {}
    for relative in entries:
        stem = os.path.splitext(os.path.basename(relative))[0]
        if stem in matches and matches[stem] != relative:
            matches[stem] = ""
    return {stem: relative for stem, relative in matches.items() if relative}

def _stamp(path):
    try:
        return _file_stamp(path)
    except OSError:
        return None


def _proof_stamp(path):
    """Absence is evidence; an unreadable target is not evidence of absence."""
    try:
        return _file_stamp(path)
    except FileNotFoundError:
        return None


@lru_cache(maxsize=64)
def _parse_gamelist(path, stamp, system, folder, location):
    entries, basenames = {}, {}
    for game in ET.parse(path).getroot().iter("game"):
        raw = game.findtext("path") or ""
        relative = rom_relative(system, raw, folder)
        if relative:
            entries[relative] = game
        elif os.path.isabs(os.path.expanduser(raw)):
            basenames[os.path.basename(raw)] = ""
    for relative in entries:
        base = os.path.basename(relative)
        basenames[base] = "" if base in basenames else relative
    return entries, basenames


def _gamelist(system, folder, gamelists=None, *, strict=False):
    path = os.path.join(gamelists or GAMELISTS, system, "gamelist.xml")
    try:
        return _parse_gamelist(path, _file_stamp(path), system, folder,
                               (os.path.realpath(folder), _stamp(folder)))
    except FileNotFoundError:
        return {}, {}
    except (OSError, ET.ParseError):
        if strict:
            raise
        return {}, {}


def _gamelist_path(system, path, folder, basenames):
    relative = rom_relative(system, path, folder)
    if not relative and os.path.isabs(os.path.expanduser(path)):
        return basenames.get(os.path.basename(path), "")
    return relative

def _game_title(system, path, folder=None, index=None):
    """The name ES-DE's gamelist gives a game (empty if it has no entry)."""
    folder = folder if folder is not None else _rom_folder(system)
    entries, basenames = index if index is not None else _gamelist(system, folder)
    entry = entries.get(_gamelist_path(system, path, folder, basenames))
    return (entry.findtext("name") or "") if entry is not None else ""

def resolve_legacy_keys(keys, gamelists, *, versions=None):
    """Return verified legacy identities and their canonical key and absolute path."""
    by_system = {}
    for key in keys:
        if parts := legacy_key_parts(key):
            system, stem = parts
            by_system.setdefault(system, set()).add(stem)
    if not by_system:
        return {}
    root, definitions = _rom_configuration()
    aliases = {}
    for system, stems in by_system.items():
        definition = definitions.get(system) or {}
        folder = _system_folder(system, root, definition)
        try:
            entries, basenames = _gamelist(system, folder, gamelists, strict=True)
        except (OSError, ET.ParseError):
            continue
        for stem, relative in _legacy_roms(folder, definition.get("extensions"), entries, basenames, stems, versions).items():
            aliases[f"{system}/{stem}"] = rom_key(system, relative), os.path.join(folder, relative)
    return aliases

def _legacy_key_for_game(key):
    if not key.startswith("esde:"):
        return ""
    system, _, relative = key.removeprefix("esde:").partition("/")
    return f"{system}/{os.path.splitext(os.path.basename(relative))[0]}"

def _cheat_file(path):
    global _cheats
    if not _cheats:  # shortcut: indexed once per run, restart ArmadaLive after adding .cht files
        found = {}
        for base in CHEAT_DIRS:
            for root, _, files in os.walk(base):
                for f in files:
                    if f.endswith(".cht"):
                        found.setdefault(f[:-4].lower(), os.path.join(root, f))
        _cheats = found
    return _cheats.get(os.path.splitext(os.path.basename(path))[0].lower(), "")


def read_cheats(path):
    f = _cheat_file(path)
    if not f:
        return []
    kv = dict(re.findall(r'^(cheat\d+_(?:desc|code))\s*=\s*"(.*)"\s*$', open(f, errors="replace").read(), re.M))
    return [{"desc": kv[k], "code": kv.get(k.replace("desc", "code"), "")}
            for k in sorted(kv, key=lambda k: int(re.search(r"\d+", k).group())) if k.endswith("_desc")]


def _rom_dir():
    try:
        with open(ES + "/settings/es_settings.xml", encoding="utf-8") as settings:
            value = re.search(r'name="ROMDirectory" value="([^"]*)"', settings.read()).group(1)
    except (OSError, AttributeError):
        value = ""
    return os.path.expanduser(value or "/run/media/armada/EXT/Emulation/roms").rstrip("/")

def _custom_folders(root, definitions):
    return [(_system_folder(system, root, definition), system, frozenset(definition.get("extensions", ())))
            for system, definition in definitions.items() if definition.get("path")]


def _split_rom_path(path):
    system, sep, game = path.partition("/")
    if sep and game and re.fullmatch(r"[\w.-]+", system) and system not in (".", ".."):
        return system, game
    return "", ""


def _unique_custom(matches):
    if not matches:
        return "", ""
    # The closest containing root wins. Equivalent physical roots have the
    # same relative depth regardless of the length of a symlink's spelling.
    depth = min(distance for _, _, distance in matches)
    closest = {(system, relative) for system, relative, distance in matches if distance == depth}
    return next(iter(closest)) if len(closest) == 1 else ("", "")


def _rom_path(path, root, custom=()):
    """Infer a system only when configured roots and extensions identify it uniquely."""
    if not os.path.isabs(path):
        path = path.partition("=")[2] if path.startswith("-") else ""
        if not os.path.isabs(path):
            return "", ""
    root = os.path.abspath(root or "/")
    configured = {system: folder for folder, system, _ in custom}
    physical = os.path.realpath(path)
    lexical = os.path.normpath(path) if ".." not in path.split("/") else ""
    extension = os.path.splitext(path)[1].lower()
    matches = []
    for folder, system, extensions in custom:
        if extensions and extension not in extensions:
            continue
        logical = _relative_below(lexical, folder) if lexical else ""
        resolved = _relative_below(physical, os.path.realpath(folder))
        relative = logical or resolved
        if relative:
            matches.append((system, relative, (resolved or logical).count("/")))
    if matches:
        return _unique_custom(matches)
    for spelling, base in ((lexical, root), (physical, os.path.realpath(root))):
        system, relative = _split_rom_path(_relative_below(spelling, base))
        if system and system not in configured:
            return system, relative
    # Bind mounts can have no common physical spelling. The marker supplies a
    # candidate only; actual file equivalence must prove the alternative path.
    system, relative = _split_rom_path(physical.partition("/roms/")[2])
    if system:
        matches = [(name, relative, relative.count("/")) for folder, name, extensions in custom
                   if (not extensions or extension in extensions) and _same_file(path, os.path.join(folder, relative))]
        if matches:
            return _unique_custom(matches)
        if system not in configured and _same_file(path, os.path.join(root, system, relative)):
            return system, relative
    return "", ""


def running_game():
    """The ES-DE style event of an emulator that is running right now, found by the ROM it was started with."""
    rom_dir, definitions = _rom_configuration()
    custom, best = _custom_folders(rom_dir, definitions), None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if not os.path.basename(args[0].decode(errors="replace")).lower().startswith(EMULATORS):
            continue
        for arg in args[1:]:
            text = arg.decode(errors="replace")
            system, game = _rom_path(text, rom_dir, custom)
            if game and (best is None or int(pid) > best[0]):  # the newest process wins
                best = (int(pid), system, game)
    if not best:
        return None
    _, system, game = best
    folder = _system_folder(system, rom_dir, definitions.get(system))
    return dict(event="game-start", system=system, path=os.path.join(folder, game), name=os.path.splitext(os.path.basename(game))[0], pid=best[0])

def steam_running():
    """True while Steam runs some game of its own (`reaper SteamLaunch AppId=...`): a Steam game or a shortcut, but not ES-DE
    itself, whose emulators are found by their ROM."""
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                args = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0")
            except OSError:
                continue
            if "SteamLaunch" in args and any(a.startswith("AppId=") for a in args) and "es-de" not in " ".join(args).lower():
                return True
    return False


def _shortcut_title(appid):
    found = steamlib.shortcut(appid)
    if found and not os.path.basename(found[1].strip('"')).lower().startswith("es-de"):
        return found[0] or str(appid)
    return None


def describe_shortcut(appid):
    """A running non-Steam shortcut that is not ES-DE (TotK, Moonlight, ...), or None."""
    if (title := _shortcut_title(appid)) is not None:
        return dict(_steam_selection(appid, title, playing=True), **_steam_art(appid, True))
    return None


def esde_running():
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if os.path.basename(open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].decode(errors="replace")).lower().startswith("es-de"):
                    return True
            except OSError:
                pass
    return False


def _steam_selection(appid, name, playing=False):
    return dict(kind="game", title=name or str(appid), logo="", fanart="", manual="", hasCheats=False, playing=playing,
                key=f"steam/{appid}", system="steam")


def _steam_art(appid, allow_grid):
    steam = os.path.expanduser("~/.local/share/Steam")
    cache = glob.escape(f"{steam}/appcache/librarycache/{appid}")
    logo, hero = _first(cache + "/logo.png"), _first(cache + "/library_hero.jpg")
    if allow_grid:
        grid = glob.escape(f"{steam}/userdata") + f"/*/config/grid/{glob.escape(str(appid))}"
        logo, hero = logo or _first(grid + "_logo.*"), hero or _first(grid + "_hero.*")
    return dict(logo=_url(logo), fanart=_url(hero))


def describe_steam_ui(appid, name):
    """An app highlighted in Steam's interface, with library or shortcut artwork."""
    out = _steam_selection(appid, name)
    out.update(_steam_art(appid, True))
    return out


@lru_cache(maxsize=1)
def _parse_ludo_library(path, stamp):
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    return data.get("server_url", "").rstrip("/"), {g["rom_id"]: g for g in data.get("games", [])}


def _ludo_library():
    try:
        return _parse_ludo_library(LUDO, _file_stamp(LUDO))
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return "", {}


def _ludo_selection(appid, name):
    rom_id = int(appid.split(":")[1])
    return dict(kind="game", title=name or str(rom_id), logo="", fanart="", manual="", hasCheats=False,
                playing=False, key=f"ludo/{rom_id}", system="steam", path="")


def _ludo_art(appid, screenshot):
    rom_id = int(appid.split(":")[1])
    server, games = _ludo_library()
    path = (games.get(rom_id) or {}).get("local_path", "")
    art = dict(logo="", fanart="", manual="")
    if path:
        root, definitions = _rom_configuration()
        system, relative = _rom_path(path, root, _custom_folders(root, definitions))
        if not system:
            system, relative = _split_rom_path(path.partition("/roms/")[2])
        system = system or os.path.basename(os.path.dirname(path))
        if _valid_system(system):
            art = _game_art(system, os.path.splitext(relative or os.path.basename(path))[0])
    if not art["fanart"] and server and screenshot:
        art["fanart"] = server + screenshot
    return art


def describe_ludo(appid, name, screenshot):
    out = _ludo_selection(appid, name)
    out.update(_ludo_art(appid, screenshot))
    return out


def _steam_title(appid):
    for lib in STEAM_LIBRARIES:
        try:
            with open(f"{lib}/steamapps/appmanifest_{appid}.acf", encoding="utf-8", errors="replace") as manifest:
                match = re.search(r'"name"\s+"(.*?)"', manifest.read(128 * 1024))
                if match is not None:
                    return match.group(1)
        except OSError:
            continue
    return None


def describe_steam(appid):
    """A running Steam game: title from its manifest and local artwork, or None if unknown."""
    if (title := _steam_title(appid)) is not None:
        out = _steam_selection(appid, title, playing=True)
        out.update(_steam_art(appid, False))
        return out
    return None


def _focused_game(appid):
    if appid is None:
        return None
    title = _shortcut_title(appid) if int(appid) >= 2**31 else _steam_title(appid)
    return appid, title


def _valid_system(system):
    return re.fullmatch(r"[\w.-]+", system) and system not in (".", "..")


def _esde_selection(event):
    """Foreground identity and play state, available before title or artwork lookups."""
    ev, system, path = event.get("event", "idle"), event.get("system", ""), event.get("path", "")
    out = dict(kind="idle", title="ArmadaLive", logo="", fanart="", manual="", hasCheats=False, playing=False, key="", system="", path="")
    request = None
    if not re.fullmatch(r"[\w.-]+", system) or system in (".", ".."):
        return out, request
    if ev == "system-select":
        out.update(kind="system", system=system, title=event.get("systemName") or system)
        request = ("system", system, event.get("t", 0))
    elif path and ev not in ("quit", "startup"):
        root, definitions = _rom_configuration()
        definition = definitions.get(system) or {}
        folder = _system_folder(system, root, definition)
        rom = rom_relative(system, path, folder)
        out.update(kind="game", system=system, path=path, playing=ev == "game-start",
                   title=event.get("name") or os.path.splitext(os.path.basename(path))[0],
                   key=rom_key(system, rom), legacyKey="", romPath=os.path.join(folder, rom) if rom else "")
        request = ("game", system, path, folder, frozenset(definition.get("extensions", ())), rom)
    return out, request

def _game_art(system, relative):
    def media(kind):
        folder = glob.escape(f"{MEDIA}/{system}/{kind}/")
        return _first(folder + glob.escape(relative) + ".*") or _first(folder + glob.escape(os.path.basename(relative)) + ".*")

    return dict(logo=_url(media("marquees")), fanart=_url(media("fanart") or media("screenshots")), manual=_url(media("manuals")))

def _read_metadata(request):
    if request is None:
        return None
    kind, *args = request
    if kind == "game":
        system, path, folder, extensions, rom = args
        relative = _gamelist_path(system, path, folder, _gamelist(system, folder)[1])
        fields = dict(_game_art(system, os.path.splitext(relative or os.path.basename(path))[0]), hasCheats=bool(_cheat_file(path)))
        if title := _game_title(system, path):
            fields["title"] = title
    elif kind == "ludo":
        fields = _ludo_art(*args)
    elif kind == "steam":
        fields = _steam_art(*args)
    else:
        system, seed = args
        fan = sorted(glob.glob(glob.escape(f"{MEDIA}/{system}/fanart") + "/*"))
        fields = dict(logo=_url(_system_logo(system)), fanart=_url(random.Random(seed).choice(fan) if fan else ""))
    return request, fields


def describe(event):
    """Synchronous snapshot for callers that explicitly need a complete description."""
    state, request = _esde_selection(event)
    if request is not None:
        state.update(_read_metadata(request)[1])
    return state


def _proof_context(request):
    rules, history, source = request
    keys = set(rules) | set(history)
    if not keys:
        return ()
    root, definitions = _rom_configuration()
    systems = sorted({legacy_key_parts(key)[0] for key in keys})
    folders = []
    for system in systems:
        definition = definitions.get(system) or {}
        folder = _system_folder(system, root, definition)
        folders.append((system, folder, os.path.realpath(folder), _stamp(folder),
                        tuple(sorted(definition.get("extensions", ())))))
    lists = tuple((path, _stamp(os.path.join(path, system, "gamelist.xml")))
                  for path in sorted({GAMELISTS, source}) for system in systems)
    return tuple(folders), lists


def _read_proofs(request):
    rules, history, source = request
    before = _proof_context(request)
    if not before:
        return request, before, {}, {}
    by_source = {GAMELISTS: set(rules)}
    by_source.setdefault(source, set()).update(history)
    versions = {}
    aliases = {path: resolve_legacy_keys(keys, path, versions=versions) for path, keys in by_source.items() if keys}
    after = _proof_context(request)
    unchanged = all(_proof_stamp(path) == stamp for path, stamp in versions.items())
    return request, after if before == after and unchanged else None, aliases, versions


def _dependency_status(versions):
    """Verify collected metadata only, with a soft owner-thread work budget."""
    deadline = time.monotonic() + 0.020
    for path, stamp in versions.items():
        if time.monotonic() >= deadline:
            return "budget"
        try:
            current = _proof_stamp(path)
        except OSError:
            current = ()  # Neither a fingerprint nor positively observed absence.
        if time.monotonic() >= deadline:
            return "budget"  # A single metadata call can exhaust the soft budget.
        if current != stamp:
            return "changed"
    return "budget" if time.monotonic() >= deadline else "current"


class LegacyIdentities(QObject):
    """One proof scan for saved rules and History, independent of presentation."""
    changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self._rules = self._history = frozenset()
        self._source = GAMELISTS
        self._context = ()
        self._aliases = {}
        self._versions = {}
        self._pending = False
        request = self._request = [(self._rules, self._history, self._source)]
        self._poller = Poller(self, lambda: _read_proofs(request[0]), self._ready, 0, now=False)
        self._poller.failed.connect(self._failed)
        self._refresh_timer = QTimer(self, singleShot=True)
        self._refresh_timer.timeout.connect(self._refresh)

    def watch_rules(self, keys):
        wanted = frozenset(key for key in keys if legacy_key_parts(key))
        if wanted != self._rules:
            previous = self._request[0]
            self._rules = wanted
            self._watch_changed(previous)

    def watch_history(self, keys, gamelists):
        wanted = frozenset(key for key in keys if legacy_key_parts(key))
        if (wanted, gamelists) != (self._history, self._source):
            previous = self._request[0]
            self._history, self._source = wanted, gamelists
            self._watch_changed(previous)

    def _watch_changed(self, previous):
        rules, history, source = previous
        if (not self._pending
                and self._rules <= rules and self._history <= history and source == self._source
                and self._context == _proof_context(previous)):
            # Keep dependency evidence until the last positive alias is consumed.
            # Removing migrated keys does not invalidate proof for retained keys.
            self._request[0] = (self._rules, self._history, self._source)
            wanted = {GAMELISTS: set(self._rules)}
            wanted.setdefault(self._source, set()).update(self._history)
            self._aliases = {path: {key: value for key, value in aliases.items() if key in wanted.get(path, ())}
                             for path, aliases in self._aliases.items()}
            self._context = _proof_context(self._request[0])
            if not any(self._aliases.values()):
                self._versions = {}
            if not (self._rules or self._history):
                self._refresh_timer.stop()
            QTimer.singleShot(0, self, self.changed.emit)
        else:
            self._schedule()

    def _schedule(self):
        self._aliases, self._versions = {}, {}
        self._pending = bool(self._rules or self._history)
        self._request[0] = (self._rules, self._history, self._source)
        self._refresh_timer.start(0)

    def _refresh(self):
        if not (self._rules or self._history):
            self._context = ()
            self.changed.emit()
            return
        self._pending = True
        self._aliases, self._versions = {}, {}
        self._context = _proof_context(self._request[0])
        self._poller.refresh()
        self.changed.emit()

    def _current(self):
        if self._pending:
            return False
        if self._context != _proof_context(self._request[0]):
            self._schedule()
            return False
        if any(self._aliases.values()):
            status = _dependency_status(self._versions)
            if status == "changed":
                self._schedule()
                return False
            if status == "budget":
                self._reject_for_now()
        return not self._pending

    def identity(self, key):
        """Canonical rule target; None while pending, empty when not proved."""
        if key not in self._rules:
            return ""
        if not self._current():
            return None
        return self._aliases.get(GAMELISTS, {}).get(key, ("", ""))[0]

    def history_pending(self):
        return bool(self._history) and not self._current()

    def history_aliases(self):
        """One validated migration batch, or None while fresh proof is pending."""
        return self._aliases.get(self._source, {}) if self._current() else None

    def rule_aliases(self):
        """Validate once for the entire pending rule migration batch."""
        return self._aliases.get(GAMELISTS, {}) if self._current() else {}

    def describe(self, state):
        legacy = _legacy_key_for_game(state.get("key", ""))
        proof = self.identity(legacy)
        state["legacyPending"] = proof is None
        state["legacyKey"] = legacy if proof == state.get("key") and proof else ""

    def _ready(self, result):
        request, context, aliases, versions = result
        if request != self._request[0]:
            return
        if context is None or context != _proof_context(request):
            self._schedule()
            return
        status = _dependency_status(versions) if any(aliases.values()) else "current"
        if status == "changed":
            self._schedule()
            return
        self._context = context
        if status == "budget":
            self._reject_for_now()
            return
        self._aliases, self._versions, self._pending = aliases, versions, False
        # Also retry a prior failed migration even when the proof is unchanged.
        self.changed.emit()
        if self._rules and not self._pending:
            self._refresh_timer.start(15000)

    def _reject_for_now(self):
        # A slow metadata check must not create an immediate retry loop.
        self._aliases, self._versions, self._pending = {}, {}, False
        self.changed.emit()
        if self._rules:
            self._refresh_timer.start(15000)

    def _failed(self, error):
        if self._pending and self._refresh_timer.isActive():
            return  # A newer request is queued for refresh.
        self._reject_for_now()


class NowPlaying(QObject):
    changed = Signal()
    selection = Signal()

    def __init__(self, gamescope, steamui):
        super().__init__()
        self._event, self._steam, self._proc = {}, None, None
        self._ui_focus, self._ui_app, self._hold, self._esde = False, (0, ""), 0.0, False
        self._pinned = False
        self.processes_known = False  # Initial absence of a snapshot is not observed idle.
        self.running = False     # a game is really running (a process), even while Steam's UI is in front
        self._sgame = None       # the non-Steam shortcut in front, if it is a game of its own
        self._shortcut = False   # a non-Steam shortcut (ES-DE itself, or an emulator it started) is in front
        self._steamui = steamui
        steamui.selected.connect(self._ui_selected)
        self._state, self._mtime, self._key, self._path = describe({}), 0, "", ""
        self._base_state, self._metadata_fields = self._state, None
        metadata = self._metadata_request = [None]
        focus = self._focus_request = [None]
        self.identities = LegacyIdentities(self)
        self.identities.changed.connect(self._publish_state)
        self._metadata = Poller(self, lambda: _read_metadata(metadata[0]), self._metadata_ready, 2000, now=False)
        self._focus_lookup = Poller(self, lambda: _focused_game(focus[0]), self._focus_ready, 2000, now=False)
        self.history = None      # set by the app: the one playtime record (history.py)
        gamescope.focusChanged.connect(self._focus)
        self._focus(gamescope.state["focus"])  # the first reading happened before we were listening
        timer = QTimer(self)
        timer.timeout.connect(self._poll)
        timer.start(400)
        self._procs = Poller(self, lambda: (running_game(), esde_running(), steam_running()), self._scan, 2000)   # /proc walk: a worker thread
        self._poll()

    @Property("QVariant", notify=changed)
    def state(self):
        return self._state

    @Slot(result="QVariant")
    def cheats(self):
        return read_cheats(self._path)

    def _poll(self):
        try:
            mtime = os.stat(STATE).st_mtime_ns
            if mtime == self._mtime:
                return
            self._mtime = mtime
            event = json.load(open(STATE))
        except (OSError, ValueError):
            return
        self._event = event
        self._update()

    def _scan(self, found):
        first = not self.processes_known
        self.processes_known = True
        game, esde, steam = found
        moved = game != self._proc or esde != self._esde
        self._proc, self._esde = game, esde
        running = bool(game or steam)
        moved = moved or running != self.running
        self.running = running
        if moved or first:
            self._update()
            if first and self._pinned:
                self.changed.emit()  # Observation is ready even with pinned presentation.

    def _ui_selected(self, appid, name):
        self._ui_app = (appid, name)
        self._update()

    def _focus(self, app):
        if self._pinned:
            return
        was = self._ui_focus
        self._ui_focus = app == "769"  # 769 is Steam's own interface in the foreground
        appid = int(app) if app.isdigit() else 0
        self._shortcut = appid >= 2**31
        if was and not self._ui_focus:
            # A game just started from Steam's interface needs time to take focus.
            self._hold = time.monotonic() + 25
            QTimer.singleShot(26000, self, self._update)
        request = app if appid and not self._ui_focus else None
        if request != self._focus_request[0]:
            self._steam = self._sgame = None
            self._focus_request[0] = request
            self._focus_lookup.refresh()
            if request is not None:
                # A focused window can have an id and still have no Steam game.
                # Wait for classification before treating this focus change as a start.
                return
        self._update()

    def _focus_ready(self, result):
        appid, title = result
        if self._pinned or appid != self._focus_request[0]:
            return
        game = _steam_selection(appid, title, playing=True) if title is not None else None
        steam = game if not self._shortcut else None
        shortcut = game if self._shortcut else None
        if (steam, shortcut) != (self._steam, self._sgame):
            self._steam, self._sgame = steam, shortcut
            self._update()
        elif game is None:
            # The first lookup of an unknown window lets the normal fallback take focus.
            self._focus_request[0] = None
            self._update()

    def pin(self, event=None, ui=None):
        """Hold one complete snapshot fixed, whatever the system does afterwards."""
        self._pinned = False
        self._proc, self._steam, self._sgame, self._esde = None, None, None, True
        self._focus_request[0] = None
        self._focus_lookup.refresh()
        self._ui_focus, self._ui_app = bool(ui), tuple(ui) if ui else (0, "")
        self._event = event or {}
        self._update(synchronous=True)
        self._pinned = True

    def _selected_ui(self):
        appid, name = self._ui_app
        if not appid:
            return _esde_selection({})
        if isinstance(appid, str):
            return _ludo_selection(appid, name), ("ludo", appid, getattr(self._steamui, "screenshot", ""))
        return _steam_selection(appid, name), ("steam", str(appid), True)

    def _update(self, synchronous=False):
        """Follow the foreground selection without waiting for title or artwork."""
        if self._pinned:
            return
        if self._ui_focus:  # Browsing Steam wins over anything running behind it.
            self._path, key = "", None
            state, request = self._selected_ui()
        elif self._steam:
            self._path, state, key = "", self._steam, self._steam["key"].partition("/")[2]
            request = ("steam", key, False)
        elif self._sgame and not self._proc:
            self._path, state, key = "", self._sgame, "sgame|" + self._sgame["key"]
            request = ("steam", self._sgame["key"].partition("/")[2], True)
        elif self._ui_app[0] and time.monotonic() < self._hold and not self._shortcut:
            self._path, key = "", None
            state, request = self._selected_ui()
        else:
            # An ES-DE browsing event wins over an emulator left running behind it.
            browsing = self._esde and self._event.get("event") in ("game-select", "system-select")
            shown = (self._event if browsing else None) or self._proc or (self._event if self._esde else {})
            self._path = shown.get("path", "")
            state, request = _esde_selection(shown)
            key = ("run|" + shown["path"]) if shown is self._proc else (shown.get("event", "") + "|" + (shown.get("path") or shown.get("system") or ""))
        self._base_state = state
        if synchronous or request != self._metadata_request[0]:
            self._metadata_request[0] = None if synchronous else request
            self._metadata_fields = _read_metadata(request)[1] if synchronous and request is not None else None
            self._metadata.refresh()
        self._publish_state()
        if key is not None and key != self._key:  # A Steam highlight must not switch pages.
            self._key = key
            self.selection.emit()

    def _publish_state(self):
        self._state = dict(self._base_state, **(self._metadata_fields or {}))
        self._state["metadataPending"] = self._metadata_request[0] is not None and self._metadata_fields is None
        self.identities.describe(self._state)
        self._state["playtime"] = self.history.total_for(self._state["key"]) if self.history else 0
        self.changed.emit()

    def _metadata_ready(self, result):
        request, fields = result
        if request == self._metadata_request[0] and not self._pinned and self._metadata_fields != fields:
            self._metadata_fields = fields
            self._publish_state()
