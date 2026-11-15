"""Manage and activate cheats for RetroArch games."""
import os
from PySide6.QtCore import Property, QObject, Signal, Slot

CONFIG = os.path.expanduser("~/.config/retroarch")


class Cheats(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._active = {}  # game_key -> set of cheat indices that are active

    @Slot(str, int)
    def toggle_cheat(self, game_key, cheat_index):
        """Toggle a cheat on/off and write to RetroArch config."""
        if game_key not in self._active:
            self._active[game_key] = set()
        if cheat_index in self._active[game_key]:
            self._active[game_key].remove(cheat_index)
        else:
            self._active[game_key].add(cheat_index)
        self._write_config(game_key)
        self.changed.emit()

    @Property("QVariantMap", notify=changed)
    def active(self):
        return {key: sorted(indices) for key, indices in self._active.items() if indices}

    @Slot(str, int, result=bool)
    def is_active(self, game_key, cheat_index):
        """Check if a cheat is active."""
        return cheat_index in self._active.get(game_key, set())

    def _write_config(self, game_key):
        """Write active cheats to RetroArch's .opt config file for a game."""
        # The app's canonical key retains the ROM's relative path and extension.
        # RetroArch core options still use only the ROM's filename stem.
        system, separator, relative = game_key.removeprefix("esde:").partition("/")
        if not separator or system not in ("snes", "nes", "genesis", "n64"):
            return
        rom_name = os.path.basename(relative)
        if game_key.startswith("esde:"):
            rom_name = os.path.splitext(rom_name)[0]
        if not rom_name or rom_name in (".", ".."):
            return

        # assume RetroArch core names match system (snes -> Snes9x, etc)
        # this is a simplification; real mapping would be more complex
        core_name = {"snes": "Snes9x", "nes": "Nestopia", "genesis": "Genesis-Plus-GX"}.get(system, system)

        opt_file = os.path.join(CONFIG, "config", core_name, rom_name + ".opt")
        os.makedirs(os.path.dirname(opt_file), exist_ok=True)

        # read existing opt file if it exists
        opts = {}
        if os.path.exists(opt_file):
            with open(opt_file) as config:
                for line in config:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        opts[k.strip()] = v.strip()

        # set cheat enables
        opts["cheats_enabled"] = '"true"'
        active = self._active.get(game_key, set())
        for i in range(16):  # RetroArch supports up to 16 cheats
            opts[f"cheat{i}_enable"] = '"true"' if i in active else '"false"'

        # write back
        with open(opt_file, "w") as f:
            for k, v in opts.items():
                f.write(f"{k} = {v}\n")
