import json, os, builtins, tempfile, threading, time, unittest
from pathlib import Path
from contextlib import ExitStack
from unittest import mock

from PySide6.QtCore import QTimer

from tests.common import GAME, ROMS, Fake, dispose_qt, touch, wait_for
from armadalive import emusettings, gamerules, history, nowplaying as n, steamshortcuts


class Describe(unittest.TestCase):

    def test_history_and_rule_keys_consider_unlisted_and_hidden_siblings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = f"{directory}/collection"
            touch(f"{directory}/settings/es_settings.xml", f'<string name="ROMDirectory" value="{root}" />')
            touch(f"{directory}/custom_systems/es_systems.xml", """<systemList>
                <system><name>snes</name><path>%ROMPATH%/custom</path><extension>.sfc .zip</extension></system>
                </systemList>""")
            touch(f"{directory}/gamelists/snes/gamelist.xml", """<gameList>
                <game><path>./a/Same.sfc</path><hidden>true</hidden></game>
                <game><path>./a/Unique.sfc</path><name>Unique</name></game>
                </gameList>""")
            for rom in ("a/Same.sfc", "b/Same.zip", "a/Unique.sfc"):
                touch(f"{root}/custom/{rom}")
            with (mock.patch.object(n, "ES", directory), mock.patch.object(n, "GAMELISTS", directory + "/gamelists")):
                one = n.describe(dict(GAME, path=f"{root}/custom/a/Same.sfc"))
                two = n.describe(dict(GAME, path="b/Same.zip"))
                unique = n.describe(dict(GAME, path="a/Unique.sfc"))
                aliases = n.resolve_legacy_keys({"snes/Same", "snes/Unique"}, n.GAMELISTS)
            self.assertEqual((one["key"], two["key"], unique["key"]),
                             ("esde:snes/a/Same.sfc", "esde:snes/b/Same.zip", "esde:snes/a/Unique.sfc"))
            self.assertEqual(set(aliases), {"snes/Unique"})
            self.assertEqual(aliases["snes/Unique"][0], unique["key"])


    def test_unresolved_gamelist_path_cannot_make_a_legacy_rule_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = f"{directory}/current/roms/snes"
            other = f"{directory}/different/roms/snes/A.sfc"
            touch(f"{folder}/A.sfc")
            touch(other)
            touch(f"{directory}/custom_systems/es_systems.xml", f"""<systemList>
                <system><name>snes</name><path>{folder}</path><extension>.sfc</extension></system>
                </systemList>""")
            touch(f"{directory}/gamelists/snes/gamelist.xml", f"<gameList><game><path>{other}</path></game></gameList>")
            with (mock.patch.object(n, "ES", directory), mock.patch.object(n, "GAMELISTS", f"{directory}/gamelists")):
                self.assertEqual(n.describe(dict(GAME, path=f"{folder}/A.sfc"))["legacyKey"], "")
                self.assertEqual(n.describe(dict(GAME, path=other))["key"], "")


    def test_retargeted_system_root_invalidates_cached_path_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            original, current = f"{directory}/original", f"{directory}/current"
            linked = f"{directory}/linked"
            for folder in (original, current):
                touch(f"{folder}/Unique.sfc")
            os.symlink(original, linked)
            touch(f"{directory}/custom_systems/es_systems.xml", f"""<systemList>
                <system><name>snes</name><path>{linked}</path><extension>.sfc</extension></system>
                </systemList>""")
            gamelists = f"{directory}/gamelists"
            touch(f"{gamelists}/snes/gamelist.xml", f"<gameList><game><path>{original}/Unique.sfc</path></game></gameList>")
            with mock.patch.object(n, "ES", directory):
                self.assertIn("snes/Unique", n.resolve_legacy_keys({"snes/Unique"}, gamelists))
                os.unlink(linked)
                os.symlink(current, linked)
                self.assertNotIn("snes/Unique", n.resolve_legacy_keys({"snes/Unique"}, gamelists))


    def test_configured_rom_root_takes_precedence_over_system_named_ancestors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = f"{directory}/snes/collection"
            touch(f"{directory}/settings/es_settings.xml", f'<string name="ROMDirectory" value="{root}" />')
            touch(f"{directory}/gamelists/snes/gamelist.xml", """<gameList>
                <game><path>./Region/Unique.sfc</path><name>Correct title</name></game>
                </gameList>""")
            touch(f"{directory}/media/snes/fanart/Region/Unique.jpg")
            with (mock.patch.object(n, "ES", directory),
                  mock.patch.object(n, "GAMELISTS", directory + "/gamelists"),
                  mock.patch.object(n, "MEDIA", directory + "/media")):
                game = n.describe(dict(GAME, path=f"{root}/snes/Region/Unique.sfc"))
            self.assertEqual(game["title"], "Correct title")
            self.assertTrue(game["fanart"].endswith("/Region/Unique.jpg"))


    def test_symlink_system_root_gives_physical_and_configured_paths_one_playing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            physical, linked = f"{directory}/physical", f"{directory}/linked"
            relative = "nested/Unique.sfc"
            touch(f"{physical}/{relative}")
            touch(f"{physical}-other/{relative}")
            os.symlink(physical, linked)
            touch(f"{directory}/custom_systems/es_systems.xml", f"""<systemList>
                <system><name>snes</name><path>{linked}</path><extension>.sfc</extension></system>
                </systemList>""")
            with mock.patch.object(n, "ES", directory):
                for form in (relative, f"{linked}/{relative}", f"{physical}/{relative}"):
                    with self.subTest(form=form):
                        state = n.describe(dict(GAME, event="game-start", path=form))
                        self.assertEqual((state["key"], state["romPath"], state["playing"]),
                                         (f"esde:snes/{relative}", f"{linked}/{relative}", True))
                unrelated = n.describe(dict(GAME, event="game-start", path=f"{physical}-other/{relative}"))
                self.assertEqual(unrelated["key"], "")


    def test_nested_rom_title_and_media_follow_the_relative_path(self):
        with tempfile.TemporaryDirectory() as directory:
            touch(f"{directory}/snes/gamelist.xml", """<gameList>
                <game><path>./a/Same.sfc</path><name>First</name></game>
                <game><path>./b/Same.sfc</path><name>Second</name></game>
                </gameList>""")
            touch(f"{directory}/snes/fanart/a/Same.jpg")
            with mock.patch.object(n, "GAMELISTS", directory), mock.patch.object(n, "MEDIA", directory):
                game = n.describe(dict(GAME, path=f"{ROMS}/snes/a/Same.sfc"))
            self.assertEqual(game["title"], "First")
            self.assertTrue(game["fanart"].endswith("/a/Same.jpg"))

    def test_game_has_art_and_key(self):
        d = n.describe(GAME)
        self.assertEqual((d["kind"], d["title"], d["key"], d["system"]), ("game", "Super Game", "esde:snes/Super Game (EU).zip", "snes"))
        self.assertTrue(d["logo"].endswith("Super%20Game%20%28EU%29.png") or d["logo"].endswith("Super Game (EU).png"))
        self.assertTrue(d["fanart"] and d["manual"])
        self.assertFalse(d["playing"])
        self.assertTrue(n.describe(dict(GAME, event="game-start"))["playing"])

    def test_idle_and_system(self):
        self.assertEqual(n.describe({})["kind"], "idle")
        self.assertEqual(n.describe({"event": "quit"})["kind"], "idle")
        s = n.describe({"event": "system-select", "system": "snes", "systemName": "Super Nintendo", "t": 1})
        self.assertEqual((s["kind"], s["title"], s["system"]), ("system", "Super Nintendo", "snes"))

    def test_system_name_cannot_escape_the_media_folder(self):
        self.assertEqual(n.describe({"event": "system-select", "system": "../../etc", "t": 1})["kind"], "idle")


class Precedence(unittest.TestCase):
    """What is in front decides: Steam UI, a Steam game, then ES-DE's world."""

    def setUp(self):
        with mock.patch.object(n.NowPlaying, "_scan"):
            self.np = n.NowPlaying(Fake(), Fake())
        self.addCleanup(dispose_qt, self.np)
        self.psx = dict(event="game-start", system="psx", name="Digimon", path=f"{ROMS}/psx/Digimon.chd")

    def title(self):
        self.np._update()
        wait_for(lambda: not self.np.state.get("metadataPending"))
        return self.np._state["title"]

    def test_browsing_beats_a_lingering_emulator(self):
        self.np._esde, self.np._event, self.np._proc, self.np._shortcut = True, GAME, self.psx, True
        self.assertEqual(self.title(), "Super Game")

    def test_a_started_game_shows_the_running_emulator(self):
        self.np._esde, self.np._event, self.np._proc = True, dict(GAME, event="game-start"), self.psx
        self.assertEqual(self.title(), "Digimon")

    def test_steam_ui_selection_beats_everything_behind_it(self):
        self.np._ui_app, self.np._ui_focus, self.np._esde, self.np._event, self.np._proc = (1190170, "OlliOlli"), True, True, GAME, self.psx
        self.assertEqual(self.title(), "OlliOlli")

    def test_a_launching_steam_game_is_held_but_a_shortcut_in_front_ends_the_hold(self):
        self.np._ui_app, self.np._hold = (1190170, "OlliOlli"), time.monotonic() + 25
        self.assertEqual(self.title(), "OlliOlli")
        self.np._esde, self.np._event, self.np._shortcut = True, GAME, True
        self.assertEqual(self.title(), "Super Game")

    def test_a_stale_event_is_ignored_when_es_de_is_closed(self):
        self.np._event, self.np._esde = GAME, False
        self.assertEqual(self.np._state["kind"] if self.np._update() is None else "", "idle")

    def test_snapshot_pin_holds_still(self):
        self.np.pin(GAME)
        self.assertTrue(all(self.np.state[field] for field in ("logo", "fanart", "manual")))
        self.np._ui_focus, self.np._esde = True, False
        self.np._update()
        self.assertEqual(self.np._state["title"], "Super Game")

    def close_history(self, tracker):
        try:
            tracker.pauseWrites()
            tracker.flush()
        finally:
            tracker._np.history = None
            dispose_qt(tracker)

    def test_busy_metadata_does_not_block_or_drop_an_intervening_game_start(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            touch(f"{directory}/snes/gamelist.xml", """<gameList>
                <game><path>./A.sfc</path><name>Scraped A</name></game>
                <game><path>./B.sfc</path><name>Scraped B</name></game>
                </gameList>""")
            entered, release, returned = threading.Event(), threading.Event(), threading.Event()
            threads, published, ticks = [], [], []
            read_title = n._game_title

            def delayed(system, path):
                if path == "A.sfc":
                    threads.append(threading.get_ident())
                    entered.set()
                    release.wait(2)
                    returned.set()
                return read_title(system, path)

            with (mock.patch.object(n, "GAMELISTS", directory),
                  mock.patch.object(n, "STATE", directory + "/no-state.json"),
                  mock.patch.object(n, "running_game", return_value=None),
                  mock.patch.object(n, "esde_running", return_value=True),
                  mock.patch.object(n, "steam_running", return_value=False),
                  mock.patch.object(n, "_game_title", side_effect=delayed)):
                backend = n.NowPlaying(Fake(), Fake())
                path = f"{directory}/history.json"
                touch(path, '{"imported": true}')
                tracker = history.History(backend, path)
                resources.callback(dispose_qt, backend)
                resources.callback(self.close_history, tracker)
                backend.history = tracker
                rules_path = f"{directory}/rules.json"
                touch(rules_path, '{"rules": {"game:esde:snes/A.sfc": {"fps": 30}}}')
                display = mock.Mock()
                with mock.patch.object(gamerules, "CONFIG", rules_path):
                    rules = gamerules.GameRules(backend, display, mock.Mock())
                resources.callback(dispose_qt, rules)
                backend.changed.connect(lambda: published.append(dict(backend.state)))
                backend._esde = True
                backend._event = dict(GAME, path="A.sfc", name="Event A")
                try:
                    backend._update()
                    QTimer.singleShot(0, lambda: ticks.append(True))
                    wait_for(lambda: entered.is_set() and ticks)
                    self.assertNotEqual(threads, [threading.get_ident()])
                    self.assertFalse(returned.is_set())
                    self.assertEqual(backend.state["title"], "Event A")
                    backend._event = dict(backend._event, event="game-start")
                    backend._update()
                    self.assertTrue(backend.state["playing"])
                    self.assertEqual(backend.state["key"], "esde:snes/A.sfc")
                    display.setFpsLimit.assert_called_once_with(30)
                    tracker._open[3] -= 12
                    backend._event = dict(GAME, path="B.sfc", name="Event B")
                    backend._update()
                    self.assertFalse(backend.state["playing"])
                finally:
                    release.set()
                wait_for(lambda: backend.state["title"] == "Scraped B")
                self.assertNotIn("Scraped A", [state["title"] for state in published])
                self.assertIn(("esde:snes/A.sfc", True), [(state["key"], state["playing"]) for state in published])
                self.assertGreaterEqual(tracker.info("esde:snes/A.sfc")["total"], 12)
                self.assertEqual(tracker.info("esde:snes/A.sfc")["plays"], 1)

    def test_matching_metadata_enriches_the_open_history_session(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            path = f"{directory}/history.json"
            touch(path, '{"imported": true}')
            tracker = history.History(self.np, path)
            resources.callback(self.close_history, tracker)
            self.np.history = tracker
            self.np._event, self.np._esde = dict(GAME, event="game-start"), True
            self.np._update()
            key = "esde:snes/Super Game (EU).zip"
            self.assertEqual(tracker.games[key]["plays"], 1)
            self.assertEqual(tracker.games[key]["title"], "x")
            wait_for(lambda: bool(tracker.games[key].get("art")))
            self.assertEqual((tracker.games[key]["title"], tracker.games[key]["plays"]), ("Super Game", 1))

    def test_blocked_steam_manifest_does_not_block_focus_changes_or_publish_late(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = f"{directory}/steamapps/appmanifest_42.acf"
            touch(manifest, '"name" "Old Steam game"')
            entered, release = threading.Event(), threading.Event()
            reads, published = [], []
            read = builtins.open

            def blocked(path, *args, **kwargs):
                if path == manifest:
                    reads.append(threading.get_ident())
                    entered.set()
                    release.wait(1)
                return read(path, *args, **kwargs)

            self.np.changed.connect(lambda: published.append(self.np.state["key"]))
            with (mock.patch.object(n, "STEAM_LIBRARIES", [directory]),
                  mock.patch("builtins.open", side_effect=blocked)):
                try:
                    self.np._focus("42")
                    wait_for(entered.is_set)
                    self.np._ui_selected(10, "Current selection")
                    self.np._focus("769")
                    self.assertNotEqual(reads, [threading.get_ident()])
                    self.assertEqual((self.np.state["key"], self.np.state["playing"]), ("steam/10", False))
                finally:
                    release.set()
                wait_for(lambda: not self.np._focus_lookup._busy)
                self.assertNotIn("steam/42", published)

    def test_known_steam_start_precedes_its_blocked_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            touch(f"{directory}/steamapps/appmanifest_42.acf", '"name" "Steam game"')
            entered, release = threading.Event(), threading.Event()
            first = n._first

            def blocked(pattern):
                if "/42/" in pattern:
                    entered.set()
                    release.wait(1)
                return first(pattern)

            with (mock.patch.object(n, "STEAM_LIBRARIES", [directory]),
                  mock.patch.object(n, "_first", side_effect=blocked)):
                try:
                    self.np._focus("42")
                    wait_for(entered.is_set)
                    self.assertEqual((self.np.state["key"], self.np.state["title"], self.np.state["playing"]),
                                     ("steam/42", "Steam game", True))
                    self.assertTrue(self.np.state.get("metadataPending"))
                finally:
                    release.set()
                wait_for(lambda: not self.np.state.get("metadataPending"))


class RunningGame(unittest.TestCase):

    def test_a_nested_rom_keeps_every_path_component(self):
        rom = f"{ROMS}/gbc/Region/Nested/My Game.gbc"
        a, b, c = self.cmdline(rom)
        with a, b, c:
            game = n.running_game()
        self.assertEqual(game["path"], f"{ROMS}/gbc/Region/Nested/My Game.gbc")
        self.assertEqual(game["name"], "My Game")


    def test_detects_running_rom_under_a_custom_system_root(self):
        with tempfile.TemporaryDirectory() as directory:
            rom = f"{directory}/outside-roms/Region/Unique.sfc"
            touch(f"{directory}/custom_systems/es_systems.xml", f"""<systemList>
                <system><name>snes</name><path>{directory}/outside-roms</path><extension>.sfc</extension></system>
                </systemList>""")
            read_file = builtins.open
            read_proc = mock.mock_open(read_data=f"retroarch\0-L\0/cores/core.so\0{rom}\0".encode())
            with (mock.patch.object(n, "ES", directory),
                  mock.patch("os.listdir", return_value=["42"]),
                  mock.patch("builtins.open", side_effect=lambda path, *a, **kw:
                             read_proc(path, *a, **kw) if str(path).startswith("/proc/") else read_file(path, *a, **kw))):
                game = n.running_game()
                self.assertEqual((game["system"], game["path"], n.describe(game)["key"]),
                                 ("snes", rom, "esde:snes/Region/Unique.sfc"))


    def test_detects_physical_path_for_a_symlink_configured_system(self):
        with tempfile.TemporaryDirectory() as directory:
            physical, linked = f"{directory}/physical", f"{directory}/linked"
            rom = f"{physical}/Region/Unique.sfc"
            touch(rom)
            os.symlink(physical, linked)
            touch(f"{directory}/custom_systems/es_systems.xml", f"""<systemList>
                <system><name>snes</name><path>{linked}</path><extension>.sfc</extension></system>
                </systemList>""")
            read_file = builtins.open
            read_proc = mock.mock_open(read_data=f"retroarch\0{rom}\0".encode())
            with (mock.patch.object(n, "ES", directory), mock.patch("os.listdir", return_value=["42"]),
                  mock.patch("builtins.open", side_effect=lambda path, *a, **kw:
                             read_proc(path, *a, **kw) if str(path).startswith("/proc/") else read_file(path, *a, **kw))):
                game = n.running_game()
                self.assertIsNotNone(game)
                self.assertEqual((game["path"], n.describe(game)["key"]),
                                 (f"{linked}/Region/Unique.sfc", "esde:snes/Region/Unique.sfc"))

    def cmdline(self, rom):
        args = b"\0".join(a.encode() for a in ("retroarch", "-L", "/cores/x.so", rom, "--appendconfig", "/c.cfg")) + b"\0"
        return mock.patch("builtins.open", mock.mock_open(read_data=args)), mock.patch("os.listdir", return_value=["42"]), mock.patch.object(n, "_rom_dir", return_value=ROMS)

    def test_a_rom_under_another_mount_of_the_same_card_is_found(self):   # what Ludo starts: /run/media/EXT instead of /run/media/armada/EXT
        for rom in ("/run/media/EXT/Emulation/roms/gbc/Pokemon - Silver.gbc", ROMS + "/gbc/Pokemon - Silver.gbc"):
            a, b, c = self.cmdline(rom)
            with a, b, c, mock.patch.object(n, "_same_file", return_value=True):
                game = n.running_game()
            self.assertEqual((game["system"], game["name"]), ("gbc", "Pokemon - Silver"))


class InitialProcessRecovery(unittest.TestCase):
    def setUp(self):
        from tests.test_gamerules import FakeGs, FakeSys
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config, self.actual = self.root / 'game_rules.json', self.root / 'GFX.ini'
        self.journal = self.root / 'emulator_undo.json'
        self.config.write_text(json.dumps({'enabled': False, 'rules': {}}))
        self.actual.write_text('[Settings]\nInternalResolution=4\n')
        self.journal.write_text(json.dumps([[str(self.actual), 'Settings', 'InternalResolution', '02']]))
        self.enterContext(mock.patch.object(gamerules, 'CONFIG', self.config))
        self.enterContext(mock.patch.dict(emusettings.SPECS, {'gc': (self.actual, 'Settings', 'InternalResolution', '{}')}))
        self.enterContext(mock.patch.object(n, 'STATE', str(self.root / 'absent-state.json')))
        self.enterContext(mock.patch.object(n, '_rom_configuration', return_value=(str(self.root), {})))
        self.enterContext(mock.patch.object(n, 'esde_running', return_value=False))
        self.enterContext(mock.patch.object(n, 'steam_running', return_value=False))
        self.entered, self.release = threading.Event(), threading.Event()
        self.game, self.failed = None, False
        def reader():
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError('test did not release initial process observation')
            if self.failed:
                raise OSError('initial process observation unavailable')
            return self.game
        self.enterContext(mock.patch.object(n, 'running_game', side_effect=reader))
        self.now = n.NowPlaying(Fake(), Fake())
        self.gs, self.system = FakeGs(), FakeSys()
        self.objects = [self.now, self.gs, self.system]
        self.addCleanup(self.close_objects)
        self.assertTrue(self.entered.wait(1))

    def close_objects(self):
        self.release.set()
        for timer in self.now.findChildren(QTimer):
            timer.stop()
        for obj in reversed(self.objects):
            dispose_qt(obj)

    def rules(self):
        rules = gamerules.GameRules(self.now, self.gs, self.system)
        self.objects.append(rules)
        return rules

    def observe(self, game=None):
        self.game = game
        self.release.set()
        wait_for(lambda: not self.now._procs._busy)

    def test_disabled_app_restart_retains_active_override_before_and_after_first_scan(self):
        self.rules()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertTrue(self.journal.exists())
        game = dict(event='game-start', system='gc', path=str(self.root / 'gc/Fixture.iso'))
        self.observe(game)
        self.assertTrue(self.now.running)
        self.assertTrue(self.now.state['playing'])
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertTrue(self.journal.exists())
        self.game = None
        self.now._procs.kick()
        wait_for(lambda: not self.now.running)
        self.assertEqual(emusettings.read('gc'), '02')
        self.assertFalse(self.journal.exists())

    def test_initial_idle_observation_notifies_and_releases_deferred_recovery(self):
        self.rules()
        notices = []
        self.now.changed.connect(lambda: notices.append(self.now.running))
        self.assertEqual(emusettings.read('gc'), '4')
        self.observe()
        self.assertEqual(notices, [False])
        self.assertEqual(emusettings.read('gc'), '02')
        self.assertFalse(self.journal.exists())

    def test_failed_initial_scan_retains_recovery_until_successful_retry(self):
        self.rules()
        self.failed = True
        self.observe()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertTrue(self.journal.exists())
        self.failed = False
        self.now._procs.kick()
        wait_for(lambda: not self.now._procs._busy)
        self.assertEqual(emusettings.read('gc'), '02')
        self.assertFalse(self.journal.exists())

    def test_first_idle_observation_respects_backup_freeze(self):
        rules = self.rules()
        rules.pauseWrites(restoring=True)
        self.observe()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertTrue(self.journal.exists())
        rules.resumeWrites()
        self.assertEqual(emusettings.read('gc'), '02')
        self.assertFalse(self.journal.exists())

    def test_unknown_observation_does_not_delay_a_noop_launch(self):
        self.journal.unlink()
        rules = self.rules()
        path = str(self.root / 'gc/Fixture.iso')
        self.assertEqual(rules.prepare_esde_launch('gc', path, 'Fixture'), '')
        self.assertEqual(emusettings.read('gc'), '4')

    def launch_waiting_for_observation(self):
        from tests.test_steamshortcuts import close_shortcuts
        self.journal.unlink()
        self.actual.write_text('[Settings]\nInternalResolution=02\n')
        self.config.write_text(json.dumps({'rules': {'system:gc': {'res': 4}}}))
        rules = self.rules()
        coordinator = steamshortcuts.SteamShortcuts(prepare_esde_launch=rules.prepare_esde_launch)
        self.addCleanup(close_shortcuts, coordinator)
        self.now.changed.connect(coordinator.resume_esde_launch)
        rules.changed.connect(coordinator.resume_esde_launch)
        self.sent, self.notices = [], []
        coordinator.notice.connect(lambda code, detail: self.notices.append(code))
        def dispatch(script, done, **kwargs):
            if kwargs['valid']():
                self.sent.append(script)
                done(True)
            else:
                done(None)
        self.enterContext(mock.patch.object(steamshortcuts, 'known_appid', return_value=123))
        self.enterContext(mock.patch.object(steamshortcuts, 'eval_js', side_effect=dispatch))
        coordinator.launch('Fixture', 'gc', str(self.root / 'gc/Fixture.iso'),
                           lambda *_: ['/usr/bin/emu', 'fixture'], lambda: True)
        wait_for(lambda: coordinator._preparing is None)
        return coordinator

    def test_first_idle_observation_resumes_the_existing_launch_coordinator(self):
        coordinator = self.launch_waiting_for_observation()
        self.assertEqual(self.sent, [])
        self.assertEqual(emusettings.read('gc'), '02')
        self.assertTrue(coordinator.state['busy'])
        self.observe()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.notices, ['launchRequested'])
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertFalse(coordinator.state['busy'])

    def test_delayed_first_observation_cannot_renew_launch_deadline(self):
        from types import SimpleNamespace
        clock = [10.0]
        with mock.patch.object(steamshortcuts, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
            coordinator = self.launch_waiting_for_observation()
            self.assertEqual(self.sent, [])
            clock[0] = 15.0
            self.observe()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notices, ['prepareFailed'])
        self.assertEqual(emusettings.read('gc'), '02')
        self.assertFalse(coordinator.state['busy'])


if __name__ == "__main__":
    unittest.main()
