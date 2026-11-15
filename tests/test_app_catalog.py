import base64
import json
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

from tests.common import delete_qobject, wait_for
from armadalive import apps, steamshortcuts


class DesktopOverrides(unittest.TestCase):
    def test_hidden_and_no_display_overrides_mask_the_system_entry(self):
        with TemporaryDirectory() as tmp:
            user, system = (Path(tmp) / name for name in ("user", "system"))
            for directory in (user, system):
                directory.mkdir()
            for name, key in (("hidden", "Hidden"), ("no-display", "NoDisplay")):
                (user / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName=User\n{key}=true\n")
                (system / f"{name}.desktop").write_text("[Desktop Entry]\nType=Application\nName=System\n")
            (system / "visible.desktop").write_text("[Desktop Entry]\nType=Application\nName=Visible\n")
            with mock.patch.object(apps, "APP_DIRS", [user, system]):
                self.assertEqual(set(apps._catalog()), {"visible"})


class Catalog(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "apps.json"
        for module, name, value in (
            (apps, "CONFIG", str(self.config)),
            (apps, "_catalog", lambda: {}),
            (apps, "ICON_ROOTS", []),
            (apps.steamlib, "games", lambda: []),
            (apps.esdelib, "games", lambda **_: {}),
            (apps.esdelib, "systems", lambda: {}),
        ):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    @contextmanager
    def backend(self):
        backend = apps.Apps()
        try:
            yield backend
        finally:
            if hasattr(backend, "catalogLoading"):
                wait_for(lambda: not backend.catalogLoading and not backend._art_running and backend._shortcuts._preparing is None)
                delete_qobject(backend)

    def test_startup_and_picker_reads_do_not_wait_for_the_scan(self):
        owner = threading.get_ident()
        started, release = threading.Event(), threading.Event()

        def scan(**_):
            self.assertNotEqual(threading.get_ident(), owner)
            started.set()
            release.wait(3)
            return [dict(id="steam:10", name="A game", icon="")]

        with mock.patch.object(apps.steamlib, "games", scan), self.backend() as backend:
            self.assertTrue(started.wait(1))
            try:
                self.assertEqual(backend.catalog(), [])
                self.assertEqual(backend.slots, [{}])
                self.assertIn("apps", [g["id"] for g in backend.groups()])
            finally:
                release.set()
            wait_for(lambda: bool(backend.catalog()))
            self.assertEqual(backend.catalog()[0]["name"], "A game")

    def test_unavailable_pins_are_hidden_and_survive_an_edit_until_the_next_mount(self):
        self.config.write_text(json.dumps({"slots": ["steam:10", "folder:1"], "folders": {
            "1": {"name": "Games", "items": ["steam:10"]}}, "next": 2}))
        games = []
        with mock.patch.object(apps.steamlib, "games", lambda: games[:]), self.backend() as backend:
            if hasattr(backend, "catalogLoading"):
                wait_for(lambda: not backend.catalogLoading)
            backend.renameFolder("folder:1", "Favorites")
            saved = json.loads(self.config.read_text())
            self.assertEqual(saved["slots"], ["steam:10", "folder:1"])
            self.assertEqual(saved["folders"]["1"]["items"], ["steam:10"])
            self.assertEqual([row.get("id") for row in backend.slots], ["folder:1", None])
            self.assertEqual(backend.folderItems("folder:1"), [])
            self.assertEqual(backend.layoutSlot(0), 1)
            self.assertEqual(backend.layoutSlot(1), 2)
            with mock.patch.object(steamshortcuts, "eval_js") as run:
                backend.launch(0)
                backend.randomIn("folder:1")
                run.assert_not_called()
            games.append(dict(id="steam:10", name="A game", icon=""))
            backend.refreshCatalog()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual([row.get("name") for row in backend.slots], ["A game", "Favorites", None])
            self.assertEqual(backend.folderItems("folder:1")[0]["name"], "A game")
            games.clear()
            backend.refreshCatalog()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual([row.get("name") for row in backend.slots], ["Favorites", None])

    def test_an_edit_before_the_first_scan_is_not_replaced_by_defaults(self):
        started, release = threading.Event(), threading.Event()

        def desktop():
            started.set()
            release.wait(3)
            return {"org.mozilla.firefox": dict(name="Firefox", iconName="")}

        with mock.patch.object(apps, "_catalog", desktop), self.backend() as backend:
            try:
                self.assertTrue(started.wait(1))
                backend.assign(backend.layoutSlot(0), "folder:new")
            finally:
                release.set()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual([row.get("id") for row in backend.slots], ["folder:1", None])

    def test_catalog_changes_are_published_on_the_owner_thread(self):
        owner = threading.get_ident()
        started, release = threading.Event(), threading.Event()
        published = []

        def scan():
            started.set()
            release.wait(3)
            return [dict(id="steam:10", name="A game", icon="")]

        with mock.patch.object(apps.steamlib, "games", scan), self.backend() as backend:
            try:
                self.assertTrue(started.wait(1))
                backend.catalogChanged.connect(lambda: published.append((threading.get_ident(), backend.catalog())))
            finally:
                release.set()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual(published, [(owner, [dict(id="steam:10", name="A game", icon="",
                                                      cover=True, group="steam")])])

    def test_scan_errors_keep_last_catalog_and_next_refresh_recovers(self):
        steam = mock.Mock(side_effect=[
            [dict(id="steam:10", name="Original", icon="")],
            OSError("device unavailable"),
            [dict(id="steam:10", name="Renamed", icon="")],
        ])
        with mock.patch.object(apps.steamlib, "games", steam), self.assertLogs(apps.log, level="ERROR"), \
                self.backend() as backend:
            wait_for(lambda: not backend.catalogLoading)
            backend.refreshCatalog()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual(backend.catalog()[0]["name"], "Original")
            backend.refreshCatalog()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual(backend.catalog()[0]["name"], "Renamed")

    def test_overlapping_refreshes_discard_the_stale_scan_and_coalesce(self):
        first_started, release_first = threading.Event(), threading.Event()
        second_started, release_second = threading.Event(), threading.Event()
        calls = []

        def scan():
            calls.append(1)
            if len(calls) == 1:
                first_started.set()
                release_first.wait(3)
                return [dict(id="steam:10", name="Old", icon="")]
            second_started.set()
            release_second.wait(3)
            return [dict(id="steam:11", name="Latest", icon="")]

        with mock.patch.object(apps.steamlib, "games", scan), self.backend() as backend:
            try:
                self.assertTrue(first_started.wait(1))
                for _ in range(10):
                    backend.refreshCatalog()
                self.assertEqual(len(calls), 1)
                release_first.set()
                wait_for(second_started.is_set)
                self.assertEqual(backend.catalog(), [])
            finally:
                release_first.set()
                release_second.set()
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual(len(calls), 2)
            self.assertEqual([row["name"] for row in backend.catalog()], ["Latest"])

    def test_rom_cover_rendering_runs_off_thread_only_for_pinned_tiles(self):
        cover = str(self.config.with_name("A.png"))
        games = {"snes": [dict(id="esde:snes/A.sfc", name="A", cover=cover, rom="/roms/A.sfc")]}
        owner = threading.get_ident()
        calls = []

        def render(path):
            calls.append((threading.get_ident(), path))
            return "file:///rounded.png"

        with mock.patch.object(apps.esdelib, "games", lambda **_: games), \
                mock.patch.object(apps.esdelib, "systems", lambda: {"snes": {"fullname": "Super Nintendo"}}), \
                mock.patch.object(apps.steamlib, "games", lambda: []), \
                mock.patch.object(apps.steamlib, "_url", render), \
                self.backend() as backend:
            wait_for(lambda: not backend.catalogLoading)
            self.assertEqual(calls, [])
            self.assertTrue(backend.catalog()[0]["icon"].endswith("A.png"))
            backend.assign(0, "esde:snes/A.sfc")
            wait_for(lambda: backend.slots[0]["icon"] == "file:///rounded.png")
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0][0], owner)
            self.assertEqual(calls[0][1], cover)

    def test_refresh_retains_pin_and_preview_while_replacement_art_is_pending(self):
        rom = dict(id="esde:snes/A.sfc", name="A", cover="/covers/A.png", rom="/roms/A.sfc")
        self.config.write_text(json.dumps({"slots": [rom["id"], "folder:1"], "folders": {
            "1": {"name": "Games", "items": [rom["id"]]}}}))
        started, release = threading.Event(), threading.Event()
        rounds = []

        def render(path):
            rounds.append(path)
            if len(rounds) == 1:
                return "file:///initial.png"
            started.set()
            release.wait(3)
            return "file:///replacement.png"

        with mock.patch.object(apps.esdelib, "games", lambda **_: {"snes": [rom]}), \
                mock.patch.object(apps.esdelib, "systems", lambda: {"snes": {"fullname": "SNES"}}), \
                mock.patch.object(apps.steamlib, "games", return_value=[]), \
                mock.patch.object(apps.steamlib, "_url", render), \
                self.backend() as backend:
            wait_for(lambda: backend.slots[0]["icon"] == "file:///initial.png")
            backend.refreshCatalog()
            try:
                wait_for(started.is_set)
                self.assertEqual(backend.slots[0]["icon"], "file:///initial.png")
                self.assertEqual(backend.slots[1]["preview"], ["file:///initial.png"])
            finally:
                release.set()
            wait_for(lambda: backend.slots[0]["icon"] == "file:///replacement.png")

    def test_a_late_preview_is_discarded_if_its_pin_was_removed(self):
        rom = dict(id="esde:snes/A.sfc", name="A", cover="/covers/A.png", rom="/roms/A.sfc")
        started, release = threading.Event(), threading.Event()
        rounds = []

        def render(path):
            rounds.append(path)
            if len(rounds) == 1:
                started.set()
                release.wait(3)
                return "file:///obsolete.png"
            return "file:///current.png"

        self.config.write_text(json.dumps({"slots": [rom["id"]]}))
        with mock.patch.object(apps.esdelib, "games", lambda **_: {"snes": [rom]}), \
                mock.patch.object(apps.esdelib, "systems", lambda: {"snes": {"fullname": "SNES"}}), \
                mock.patch.object(apps.steamlib, "_url", render), self.backend() as backend:
            try:
                wait_for(started.is_set)
                backend.assign(backend.layoutSlot(0), "")
            finally:
                release.set()
            wait_for(lambda: not backend._art_running)
            self.assertEqual(backend.slots, [{}])
            backend.assign(backend.layoutSlot(0), rom["id"])
            self.assertEqual(backend.slots[0]["icon"], "")
            wait_for(lambda: not backend._art_running)
            self.assertEqual(backend.slots[0]["icon"], "file:///current.png")
            self.assertEqual(rounds, [rom["cover"], rom["cover"]])

    def test_first_rom_launch_prepares_real_art_off_thread_and_drops_duplicate_taps(self):
        name = "Big game"
        rom = dict(id="esde:snes/Big game.sfc", name=name, cover="", rom="/roms/Big game.sfc")
        media = self.config.parent / "snes" / "fanart"
        media.mkdir(parents=True)
        fanart = QImage(2560, 1440, QImage.Format.Format_RGB32)
        fanart.fill(0xFF336699)
        self.assertTrue(fanart.save(str(media / "Big game.jpg")))

        owner = threading.get_ident()
        started, release = threading.Event(), threading.Event()
        artwork = steamshortcuts.artwork
        prepared_on, steam_calls, heartbeats, notices = [], [], [], []

        def gated_artwork(system, path):
            prepared_on.append(threading.get_ident())
            started.set()
            release.wait(1.5)
            return artwork(system, path)

        def dispatch(expression, done, **_):
            steam_calls.append((threading.get_ident(), expression, done))

        with mock.patch.object(apps.esdelib, "games", return_value={"snes": [rom]}), \
                mock.patch.object(apps.esdelib, "systems", return_value={"snes": {"fullname": "SNES"}}), \
                mock.patch.object(apps.esdelib, "command", return_value=["/usr/bin/emu", rom["rom"]]), \
                mock.patch.object(apps.steamlib, "games", return_value=[]), \
                mock.patch.object(steamshortcuts, "SHORTCUTS", str(self.config.parent / "missing.vdf")), \
                mock.patch.object(steamshortcuts, "MEDIA", str(self.config.parent)), \
                mock.patch.object(steamshortcuts, "artwork", gated_artwork), \
                mock.patch.object(steamshortcuts, "eval_js", dispatch), \
                self.backend() as backend:
            shortcuts = backend._shortcuts
            shortcuts.notice.connect(lambda code, title: notices.append((code, title)))
            wait_for(lambda: not backend.catalogLoading)
            QTimer.singleShot(0, lambda: heartbeats.append(True))
            try:
                backend.launchId(rom["id"])
                wait_for(started.is_set)
                self.assertTrue(heartbeats)
                self.assertFalse(steam_calls)
                backend.launchId(rom["id"])
            finally:
                release.set()
            wait_for(lambda: len(steam_calls) == 1)
            thread, expression, added = steam_calls[0]
            self.assertEqual(thread, owner)
            self.assertEqual(len(prepared_on), 1)
            self.assertNotEqual(prepared_on[0], owner)
            payload = json.loads(expression[expression.rfind(")({") + 2:-1])
            images = {kind: QImage.fromData(base64.b64decode(data)) for kind, data, _ in payload["art"]}
            self.assertEqual(set(images), {1, 3})
            self.assertEqual((images[1].width(), images[1].height()), (1920, 1080))
            self.assertEqual((images[3].width(), images[3].height()), (764, 430))
            added(123)
            wait_for(lambda: len(steam_calls) == 2)
            self.assertEqual(steam_calls[1][0], owner)
            self.assertIn("SteamClient.Apps.RunGame", steam_calls[1][1])
            steam_calls[1][2](True)
            self.assertFalse(shortcuts.state["busy"])
            self.assertEqual(notices, [("launchRequested", name)])

    def test_duplicate_random_tap_cannot_dispatch_both_steam_and_emulated_games(self):
        rom = dict(id="esde:snes/Book.sfc", name="Book", cover="", rom="/roms/Book.sfc")
        native = dict(id="steam:10", name="Native", icon="")
        for first in (rom, native):
            with self.subTest(first=first["id"]):
                dispatches = []
                remaining = native if first is rom else rom
                with mock.patch.object(apps.esdelib, "games", return_value={"snes": [rom]}), \
                        mock.patch.object(apps.esdelib, "systems", return_value={"snes": {"fullname": "SNES"}}), \
                        mock.patch.object(apps.esdelib, "command", return_value=["/usr/bin/emu", rom["rom"]]), \
                        mock.patch.object(apps.steamlib, "games", return_value=[native]), \
                        mock.patch.object(apps.random, "choice", side_effect=[first["id"], remaining["id"]]) as choose, \
                        mock.patch.object(steamshortcuts, "known_appid", return_value=123), \
                        mock.patch.object(steamshortcuts, "eval_js", side_effect=lambda js, done, **_: dispatches.append((js, done))), \
                        mock.patch("armadalive.steamui.eval_js", side_effect=lambda js, done, **_: dispatches.append((js, done))), \
                        self.backend() as backend:
                    wait_for(lambda: not backend.catalogLoading)
                    backend.launchId(apps.RANDOM)
                    backend.launchId(apps.RANDOM)
                    expected_gameid = (123 << 32) | 0x02000000 if first is rom else 10
                    wait_for(lambda: any(f'"{expected_gameid}"' in js for js, _ in dispatches))
                    self.assertEqual(len(dispatches), 1)
                    self.assertEqual(choose.call_count, 1)
                    dispatches[0][1](True)



class EntryPoint(unittest.TestCase):
    def test_normal_main_shares_the_coordinator_between_grid_and_add_action(self):
        from armadalive import app as entry
        from contextlib import ExitStack
        # Stop before loading a window. Every service constructor is replaced so
        # this exercises main's real wiring without devices, services or user files.
        with ExitStack() as stack:
            names = ('QGuiApplication QQmlApplicationEngine VectorArtwork Gamescope SteamUi NowPlaying '
                     'SystemMonitor ControlsLayout EmulatorControl History SteamShortcuts Apps AynButton '
                     'Settings GameInfo Shell Controls UiSettings Updater Admin Health GameRules Tiles '
                     'Battery Media Backup Palette Tasks Onboarding Activity Manuals LudoSync Trackpad '
                     'Cheats Pad Closer Thermal QTimer').split()
            constructors = {name: stack.enter_context(mock.patch.object(entry, name)) for name in names}
            stack.enter_context(mock.patch.object(entry.signal, 'signal'))
            constructors['QQmlApplicationEngine'].return_value.rootObjects.return_value = []
            self.assertEqual(entry.main(), 1)
            coordinator = constructors['SteamShortcuts'].return_value
            constructors['SteamShortcuts'].assert_called_once_with(constructors['NowPlaying'].return_value)
            constructors['Apps'].assert_called_once_with(constructors['History'].return_value, coordinator)
            context = constructors['QQmlApplicationEngine'].return_value.rootContext.return_value
            context.setContextProperty.assert_any_call('steamShortcuts', coordinator)
            context.setContextProperty.assert_any_call('apps', constructors['Apps'].return_value)

if __name__ == "__main__":
    unittest.main()
