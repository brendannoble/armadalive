import json, os, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

from tests.common import app, delete_qobject
from tests.test_runtime import wait_for
from PySide6.QtCore import QObject, QTimer, Signal
from armadalive.nowplaying import LegacyIdentities
from armadalive import gamerules, system, emusettings

GAME = dict(kind="game", key="snes/Super Game (EU)", system="snes", playing=True)


class FakeNow(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.identities = LegacyIdentities(self)
        self.processes_known = True
        self.state = dict(GAME)


class FakeGs(QObject):
    fpsLimitFinished = Signal(object, bool)
    fpsLimitRequested = Signal(object)
    def __init__(self):
        super().__init__()
        self.state, self.set = {"limit": 60}, []

    def setFpsLimit(self, fps):
        self.set.append(fps)
        request = len(self.set)
        self.fpsLimitRequested.emit(request)
        QTimer.singleShot(0, self, lambda: self.fpsLimitFinished.emit(request, True))
        return request


class FakeSys(QObject):
    profileFinished = Signal(object, bool)
    profileRequested = Signal(object)
    stats = {"profile": "Eco"}

    def __init__(self):
        super().__init__()
        self.requests = []

    def setProfile(self, profile):
        self.requests.append(profile)
        request = len(self.requests)
        self.profileRequested.emit(request)
        QTimer.singleShot(0, self, lambda: self.profileFinished.emit(request, True))
        return request


class Rules(unittest.TestCase):
    def setUp(self):
        gamerules.CONFIG = os.path.join(tempfile.mkdtemp(), "rules.json")
        self.now, self.gs = FakeNow(), FakeGs()
        self.sys = FakeSys()
        self.rules = gamerules.GameRules(self.now, self.gs, self.sys)

    def test_a_rule_shows_the_name_of_its_game(self):
        class H:
            def title_of(self, key):
                return "Hades" if key == "steam/1145360" else ""
        self.rules.history = H()
        self.rules._rules = {"game:steam/1145360": dict(profile="Eco", fps=30), "game:snes/X": dict(profile="Eco", fps=0, title="Kept Title"), "system:snes": dict(profile="Eco", fps=0)}
        names = {r["key"]: (r["kind"], r["name"]) for r in self.rules.state["rules"]}
        self.assertEqual(names, {"game:steam/1145360": ("game", "Hades"), "game:snes/X": ("game", "Kept Title"), "system:snes": ("system", "snes")})

    def test_save_apply_and_forget(self):
        self.rules.toggle("game")
        self.assertEqual(self.rules.state["rules"], [dict(key="game:snes/Super Game (EU)", profile="Eco", fps=60, kind="game", name="snes/Super Game (EU)")])
        self.now.state = dict(GAME, playing=False); self.now.changed.emit()   # a game exits...
        self.now.state = dict(GAME); self.now.changed.emit()                  # ...and starts again
        self.assertEqual(self.sys.requests, ["Eco"])
        self.assertEqual(self.gs.set, [60])
        self.rules.toggle("game")
        self.assertEqual(self.rules.state["rules"], [])

    def test_a_system_rule_applies_to_every_game_of_that_system(self):
        self.rules.toggle("system")
        self.now.state = dict(GAME, key="snes/Another Game"); self.now.changed.emit()
        self.assertEqual(self.sys.requests, ["Eco"])

    def test_nothing_is_applied_while_browsing_or_when_switched_off(self):
        self.rules.toggle("system")
        self.now.state = dict(GAME, playing=False); self.now.changed.emit()
        self.rules.setEnabled(False)
        self.now.state = dict(GAME, key="snes/Other"); self.now.changed.emit()
        self.assertEqual(self.sys.requests, [])

    def test_rules_survive_a_restart(self):
        self.rules.toggle("game")
        again = gamerules.GameRules(self.now, self.gs, FakeSys())
        self.assertEqual(len(again.state["rules"]), 1)

    def test_base_rule_covers_games_without_a_rule_and_own_rules_win(self):
        FakeSys.stats = {"profile": "Eco"}
        self.rules.toggle("base")
        self.now.state = dict(GAME, key="snes/Unknown"); self.now.changed.emit()
        self.assertEqual(self.sys.requests, ["Eco"])
        FakeSys.stats = {"profile": "Performance"}
        self.now.state = dict(GAME, key="snes/Mine"); self.rules.toggle("game")   # own rule: Performance
        FakeSys.stats = {"profile": "Eco"}
        self.now.state = dict(GAME, key="snes/Other"); self.now.changed.emit()
        self.now.state = dict(GAME, key="snes/Mine"); self.now.changed.emit()
        self.assertEqual(self.sys.requests, ["Eco", "Eco", "Performance"])

    def test_idle_rule_applies_when_the_last_game_is_gone_not_while_steams_ui_is_in_front(self):
        FakeSys.stats = {"profile": "Eco"}
        self.rules.toggle("idle")
        self.now.state = dict(GAME); self.now.changed.emit()                       # a game runs (no rule for it)
        self.now.running = True
        self.now.state = dict(GAME, playing=False); self.now.changed.emit()       # Steam's UI in front, the game still runs
        self.assertEqual(self.sys.requests, [])
        self.now.running = False
        self.now.changed.emit()                                                   # now it is gone
        self.assertEqual(self.sys.requests, ["Eco"])
        app.processEvents()
        self.assertTrue(self.rules.state["loaded"].startswith("idle"))

    def test_saving_removing_and_loading_are_announced(self):
        seen = []
        self.rules.notice.connect(lambda code, detail: seen.append((code, detail)))
        self.rules.toggle("idle")
        self.rules.toggle("idle")
        self.rules.toggle("base")
        self.now.state = dict(GAME, key="snes/New"); self.now.changed.emit()
        app.processEvents()
        self.assertEqual([c for c, _ in seen], ["saved", "removed", "saved", "loaded"])
        self.assertIn("Eco", seen[2][1])


class ProfileRules(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(gamerules, "CONFIG", Path(directory, "rules.json")))
        self.enterContext(mock.patch.object(system, "Poller"))
        self.blocked = ""
        self.failed = ""
        self.entered, self.release = threading.Event(), threading.Event()
        self.completed, self.notices = [], []
        self.enterContext(mock.patch.object(system, "run", side_effect=self.run_command, create=True))
        self.enterContext(mock.patch("subprocess.Popen", side_effect=lambda args, **_: self.completed.append(args[-1])))
        self.now = FakeNow()
        self.monitor = system.SystemMonitor()
        self.gs = FakeGs()
        self.rules = gamerules.GameRules(self.now, self.gs, self.monitor)
        self.rules.notice.connect(lambda code, detail: self.notices.append((code, detail)))
        self.rules._rules = {
            "game:snes/First": {"profile": "Performance"},
            "game:snes/Next": {"profile": "Balanced"},
            gamerules.IDLE: {"profile": "Eco"},
        }

    def tearDown(self):
        self.release.set()
        self.assertTrue(wait_for(lambda: not self.monitor._commands._running))

    def run_command(self, *args):
        name = args[-1]
        if name == self.blocked:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("test did not release the profile command")
        if name == self.failed:
            raise OSError("profile unavailable")
        self.completed.append(name)

    def play(self, name):
        self.now.state = dict(GAME, key=f"snes/{name}", title=name)
        self.now.changed.emit()

    def test_older_blocked_manual_request_finishes_before_newer_rule(self):
        self.blocked = "Performance"
        self.monitor.setProfile("Performance")
        self.assertTrue(self.entered.wait(1))
        self.play("Next")
        self.assertEqual(self.completed, [])
        self.assertEqual(self.rules.state["loaded"], "")
        self.release.set()
        self.assertTrue(wait_for(lambda: not self.monitor._commands._running))
        self.assertEqual(self.completed, ["Performance", "Balanced"])
        self.assertEqual(self.notices, [("loaded", "Next · Balanced")])

    def test_idle_supersedes_a_running_rule_and_replaces_pending_rule(self):
        self.blocked = "Performance"
        self.play("First")
        self.assertTrue(self.entered.wait(1))
        self.play("Next")
        self.now.state = {"kind": "idle", "playing": False}
        self.now.changed.emit()
        self.release.set()
        self.assertTrue(wait_for(lambda: not self.monitor._commands._running))
        self.assertEqual(self.completed, ["Performance", "Eco"])
        self.assertEqual(self.notices, [("loaded", "idle · Eco")])
        self.assertEqual(self.rules.state["loaded"], "idle · Eco")

    def test_newer_manual_request_suppresses_stale_rule_notice(self):
        self.blocked = "Performance"
        self.play("First")
        self.assertTrue(self.entered.wait(1))
        self.monitor.setProfile("Eco")
        self.release.set()
        self.assertTrue(wait_for(lambda: not self.monitor._commands._running))
        self.assertEqual(self.completed, ["Performance", "Eco"])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.rules.state["loaded"], "")

    def test_failed_current_rule_does_not_publish_a_loaded_profile(self):
        self.play("First")
        self.assertTrue(wait_for(lambda: not self.monitor._commands._running))
        self.assertEqual(self.rules.state["loaded"], "First · Performance")
        self.failed = "Balanced"
        self.play("Next")
        self.assertTrue(wait_for(lambda: not self.monitor._commands._running))
        self.assertEqual(self.completed, ["Performance"])
        self.assertEqual(self.notices[-1], ("failed", "Next · Balanced"))
        self.assertEqual(self.rules.state["loaded"], "")


class RuleAcknowledgments(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(gamerules, "CONFIG", Path(directory, "rules.json")))
        self.enterContext(mock.patch.object(QTimer, "singleShot"))
        self.enterContext(mock.patch("subprocess.Popen"))  # also safe against the old direct writer
        self.now, self.gs, self.sys = FakeNow(), FakeGs(), FakeSys()
        self.rules = gamerules.GameRules(self.now, self.gs, self.sys)
        self.notices = []
        self.rules.notice.connect(lambda *r: self.notices.append(r))

    def test_frame_rate_only_rule_waits_for_its_own_real_command_acknowledgment(self):
        self.rules._apply({"fps": 60}, "game")
        self.assertEqual(self.notices, [])
        self.assertEqual(self.rules.state["loaded"], "")
        self.gs.fpsLimitFinished.emit(99, True)
        self.assertEqual(self.notices, [])
        self.gs.fpsLimitFinished.emit(1, True)
        self.assertEqual(self.notices, [("loaded", "game · –, 60 FPS")])

    def test_frame_rate_failure_cannot_be_hidden_by_profile_success(self):
        self.rules._apply({"profile": "Eco", "fps": 60}, "game")
        self.sys.profileFinished.emit(1, True)
        self.assertEqual(self.notices, [])
        self.gs.fpsLimitFinished.emit(1, False)
        self.assertEqual(self.notices, [("failed", "game · Eco, 60 FPS")])
        self.assertEqual(self.rules.state["loaded"], "")

    def test_both_commands_must_finish_even_when_display_finishes_first(self):
        self.rules._apply({"profile": "Eco", "fps": 60}, "game")
        self.gs.fpsLimitFinished.emit(1, True)
        self.assertEqual(self.notices, [])
        self.sys.profileFinished.emit(1, True)
        self.assertEqual(self.notices, [("loaded", "game · Eco, 60 FPS")])

    def test_new_manual_profile_invalidates_rule_after_its_profile_part_completed(self):
        self.rules._apply({"profile": "Eco", "fps": 60}, "game")
        self.sys.profileFinished.emit(1, True)
        self.sys.setProfile("Performance")
        self.gs.fpsLimitFinished.emit(1, True)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.rules.state["loaded"], "")

    def test_new_manual_display_invalidates_rule_after_its_display_part_completed(self):
        self.rules._apply({"profile": "Eco", "fps": 60}, "game")
        self.gs.fpsLimitFinished.emit(1, True)
        self.gs.setFpsLimit(120)
        self.sys.profileFinished.emit(1, True)
        self.assertEqual(self.notices, [])

    def test_disabling_rules_or_game_exit_suppresses_pending_success(self):
        self.rules._applied = GAME["key"]
        self.rules._apply({"fps": 60}, "game")
        self.now.state = {"playing": False}
        self.now.changed.emit()
        self.gs.fpsLimitFinished.emit(1, True)
        self.assertEqual(self.notices, [])
        self.rules._apply({"fps": 60}, "game")
        self.rules.setEnabled(False)
        self.gs.fpsLimitFinished.emit(2, True)
        self.assertEqual(self.notices, [])





class RulePersistence(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "game_rules.json"
        self.enterContext(mock.patch.object(gamerules, "CONFIG", self.path))

    def make(self):
        return gamerules.GameRules(FakeNow(), FakeGs(), FakeSys())

    def test_malformed_rules_recover_valid_backup_and_optional_local_values(self):
        import json
        saved = {"rules": {"game:legacy/name": {"profile": "Eco", "fps": 45.5, "res": 7, "extra": True}},
                 "base": {"psp": "2", "gc": None}, "future": {"value": 4}}
        self.path.write_text('{"rules":{"game:bad":null}}')
        self.path.with_suffix(".json.bak").write_text(json.dumps(saved))
        rules = self.make()
        self.assertEqual(rules.state["rules"][0]["key"], "game:legacy/name")
        rules.setEnabled(False)
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted["rules"], saved["rules"])
        self.assertEqual(persisted["base"], saved["base"])
        self.assertEqual(persisted["future"], saved["future"])

    def test_invalid_nested_rules_default_safely_without_a_backup(self):
        for content in ('{"rules":null}', '{"rules":{"game:a":[]}}', '{"rules":{"game:a":{"fps":"oops"}}}', '{"base":[]}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertEqual(self.make().state["rules"], [])

    def test_atomic_save_preserves_symlink_mode_and_last_valid_configuration(self):
        import json
        from armadalive import persistence
        target = self.root / "actual"
        original = b'{ "rules":{}, "base":{"psp":"2"} }'
        target.write_bytes(original)
        target.chmod(0o640)
        self.path.symlink_to(target.name)
        rules = self.make()
        notifications = []
        rules.changed.connect(lambda: notifications.append(True))
        replace = os.replace

        def fail_primary(source, dest):
            if Path(dest) == target:
                raise OSError("primary replace failed")
            return replace(source, dest)

        with mock.patch.object(persistence.os, "replace", side_effect=fail_primary):
            with self.assertRaisesRegex(OSError, "primary replace failed"):
                rules.toggle("game")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(notifications, [])
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        rules.setEnabled(False)
        self.assertFalse(json.loads(target.read_text())["enabled"])
        self.assertEqual(json.loads(target.read_text())["base"], {"psp": "2"})
        self.assertEqual(Path(str(target) + ".bak").read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_freeze_blocks_all_edits_and_defers_rule_checks_until_resume(self):
        rules = self.make()
        rules.toggle("game")
        previous = self.path.read_bytes()
        rules.pauseWrites(restoring=True)
        rules.toggle("idle")
        rules.cycleRes()
        rules.remove(rules.state["gameKey"])
        rules.setEnabled(False)
        with mock.patch.object(rules, "_emulator") as emulator:
            rules._check()
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertTrue(rules.state["writesPaused"])
            self.assertTrue(rules.state["enabled"])
            self.assertFalse(rules.writesPending())
            emulator.assert_not_called()
            rules.resumeWrites()
            emulator.assert_called_once()
        self.assertFalse(rules.state["writesPaused"])
        rules.setEnabled(False)
        self.assertNotEqual(self.path.read_bytes(), previous)

    def test_pause_persists_live_device_originals_before_backup_cutoff(self):
        import json
        rules = self.make()
        rules._base = {"psp": "device original"}
        rules.pauseWrites(restoring=True)
        self.assertEqual(json.loads(self.path.read_text())["base"], rules._base)
        rules.resumeWrites()
        # A second export can read an unchanged configuration without rewriting it.
        with mock.patch.object(gamerules.persistence, "write_json", side_effect=AssertionError("unexpected rewrite")):
            rules.pauseWrites()
        rules.resumeWrites()

    def test_export_uses_valid_recovered_rules(self):
        import json
        from armadalive import backup
        self.path.write_text('{broken')
        self.path.with_suffix(".json.bak").write_text('{"rules":{},"enabled":false,"base":{"psp":"2"}}')
        rules = self.make()
        archive = self.root / "archive.json"
        with mock.patch.object(backup, "CONFIG_DIR", str(self.root)), mock.patch.object(backup, "FILE", str(archive)):
            coordinator = backup.Backup((rules,))
            coordinator.save()
            self.assertEqual(coordinator.state["message"], "saved")
        saved = json.loads(archive.read_text())["files"]["game_rules.json"]
        self.assertEqual(saved, {"rules": {}, "enabled": False})
        self.assertEqual(json.loads(self.path.read_text())["base"], {"psp": "2"})

    def test_invalid_rules_keep_independently_valid_primary_originals_on_ordinary_save(self):
        import json
        self.path.write_text('{"rules":null,"base":{"psp":"device-original"}}')
        self.path.with_suffix(".json.bak").write_text('{"rules":{},"base":{"psp":"older-original"}}')
        rules = self.make()
        self.assertEqual(rules.state["rules"], [])
        rules.setEnabled(False)
        self.assertEqual(json.loads(self.path.read_text())["base"], {"psp": "device-original"})

    def test_unusable_originals_block_save_and_backup_without_erasing_evidence(self):
        from armadalive import backup
        original = b'{"rules":null,"base":{"psp":["damaged-original"]}}'
        self.path.write_bytes(original)
        rules = self.make()
        with self.assertRaisesRegex(ValueError, "original"):
            rules.setEnabled(False)
        self.assertEqual(self.path.read_bytes(), original)
        archive = self.root / "archive.json"
        archive.write_text('{"files":{"game_rules.json":{"rules":{}}}}')
        with mock.patch.object(backup, "CONFIG_DIR", str(self.root)), mock.patch.object(backup, "FILE", str(archive)):
            coordinator = backup.Backup((rules,))
            coordinator.restore()
            self.assertIn("original", coordinator.state["message"])
            self.assertFalse(coordinator.state["busy"])
        self.assertEqual(self.path.read_bytes(), original)


class EmulatorRecovery(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "game_rules.json"
        self.actual = self.root / "GFX.ini"
        # Exercise the already-merged Dolphin dialect and preserve exact syntax.
        self.original = b'[sEtTiNgS]\r\nInternalResolution = "02"  \r\nOther = unchanged\r\n'
        self.actual.write_bytes(self.original)
        self.actual.chmod(0o640)
        self.link = self.root / "linked-GFX.ini"
        self.link.symlink_to(self.actual.name)
        self.enterContext(mock.patch.object(gamerules, "CONFIG", self.config))
        self.enterContext(mock.patch.dict(emusettings.SPECS, {
            "gc": (self.link, "Settings", "InternalResolution", "{}"),
            "wii": (self.actual, "Settings", "InternalResolution", "{}"),
        }))
        self.config.write_text(json.dumps({"rules": {
            "system:gc": {"res": 4}, "system:wii": {"res": 5},
        }}))
        self.objects = []
        self.addCleanup(self.dispose_objects)
        self.rules, self.now = self.new_rules()

    def dispose_objects(self):
        for obj in reversed(self.objects):
            delete_qobject(obj)

    def new_rules(self):
        now, gs, system = FakeNow(), FakeGs(), FakeSys()
        now.state = {"kind": "", "playing": False}
        self.objects.extend((now, gs, system))
        rules = gamerules.GameRules(now, gs, system)
        self.objects.append(rules)
        return rules, now

    def select(self, system="gc", now=None):
        now = now or self.now
        # A system-scoped rule requires no legacy proof or new identity wrapper.
        now.state = dict(kind="game", key=f"{system}/Fixture", system=system,
                         title="Fixture", playing=False)
        now.changed.emit()

    def assert_original(self):
        self.assertEqual(self.actual.read_bytes(), self.original)
        self.assertEqual(self.actual.stat().st_mode & 0o777, 0o640)
        self.assertTrue(self.link.is_symlink())

    def fail_emulator_replace(self, source, target):
        if Path(target) == self.actual:
            raise OSError("temporary emulator replacement failure")
        return self.real_replace(source, target)

    def test_shared_dolphin_target_keeps_first_original_across_systems(self):
        self.select("gc")
        self.assertEqual(emusettings.read("gc"), "4")
        self.select("wii")
        self.assertEqual(emusettings.read("wii"), "5")
        self.rules.remove("system:wii")
        self.select("wii")
        self.assert_original()

    def test_restart_recovers_original_without_a_later_rule_edit(self):
        self.select()
        self.assertEqual(emusettings.read("gc"), "4")
        self.new_rules()
        self.assert_original()

    def test_failed_original_persistence_prevents_emulator_mutation(self):
        # Reject all JSON persistence, regardless of journal layout/name. The
        # existing emulator writer uses write_bytes and would otherwise succeed.
        with mock.patch.object(gamerules.persistence, "write_json",
                               side_effect=OSError("cannot make original durable")):
            self.select()
        self.assert_original()

    def test_failed_restore_can_retry_without_forgetting_original(self):
        self.select()
        self.rules.remove("system:gc")
        self.real_replace = os.replace
        with mock.patch("os.replace", side_effect=self.fail_emulator_replace):
            self.select()
        self.assertEqual(emusettings.read("gc"), "4")
        self.select()
        self.assert_original()

    def test_failed_restore_survives_a_rule_save_and_restart(self):
        self.select()
        self.rules.remove("system:gc")
        self.real_replace = os.replace
        with mock.patch("os.replace", side_effect=self.fail_emulator_replace):
            self.select()
            # Public settings write must not erase failed-restore evidence.
            self.rules.setEnabled(True)
        self.new_rules()
        self.assert_original()

    def test_legacy_handoff_restores_known_target_and_retains_unknown_and_null(self):
        legacy = {"gc": "02", "unavailable-emulator": "retained exact text", "ps2": None}
        self.config.write_text(json.dumps({"rules": {}, "base": legacy}))
        self.assertTrue(emusettings.write("gc", 4))
        self.new_rules()
        # Independent subtests also expose compatibility already present on base.
        with self.subTest("known original recovered"):
            self.assert_original()
        saved_base = json.loads(self.config.read_text()).get("base", {})
        with self.subTest("unsupported original retained"):
            self.assertEqual(saved_base.get("unavailable-emulator"), legacy["unavailable-emulator"])
        with self.subTest("null original retained"):
            self.assertIn("ps2", saved_base)
            self.assertIsNone(saved_base["ps2"])
        with self.subTest("known original handed off once"):
            self.assertNotIn("gc", saved_base)

    def test_ordinary_rule_save_preserves_unknown_and_null_legacy_originals(self):
        legacy = {"unavailable-emulator": "retained exact text", "ps2": None}
        self.config.write_text(json.dumps({"rules": {}, "base": legacy}))
        rules, _ = self.new_rules()
        rules.setEnabled(False)
        self.assertEqual(json.loads(self.config.read_text())["base"], legacy)
        self.assert_original()

    def test_failed_legacy_handoff_preserves_all_local_originals(self):
        legacy = {"gc": "02", "unavailable-emulator": "retained exact text", "ps2": None}
        self.config.write_text(json.dumps({"rules": {}, "base": legacy}))
        self.assertTrue(emusettings.write("gc", 4))
        before = self.actual.read_bytes()
        with mock.patch.object(gamerules.persistence, "write_json",
                               side_effect=OSError("cannot hand off originals")):
            self.new_rules()
        self.assertEqual(self.actual.read_bytes(), before)
        self.assertEqual(json.loads(self.config.read_text())["base"], legacy)


    def test_disable_during_play_defers_restore_until_exit_and_retries_failure(self):
        self.select()
        notices = []
        self.rules.notice.connect(lambda code, detail: notices.append(code))
        self.now.state['playing'] = True
        self.now.changed.emit()
        self.rules.setEnabled(False)
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertIn('resolutionRestorePending', notices)
        self.real_replace = os.replace
        with mock.patch('os.replace', side_effect=self.fail_emulator_replace):
            self.select()
        self.assertEqual(emusettings.read('gc'), '4')
        self.new_rules()
        self.assert_original()

    def test_disable_while_idle_restores_immediately(self):
        self.select()
        self.rules.setEnabled(False)
        self.assert_original()

    def test_failed_journal_unlink_is_repaired_before_another_override(self):
        self.select()
        remove = gamerules.persistence.remove
        def fail_after_unlink(path):
            remove(path)
            raise OSError('directory sync failed after unlink')
        with mock.patch.object(gamerules.persistence, 'remove', side_effect=fail_after_unlink):
            self.rules.setEnabled(False)
        self.assert_original()
        self.rules.setEnabled(True)
        with mock.patch.object(gamerules.persistence, 'write_json', side_effect=OSError('cannot recommit')):
            self.select()
        self.assert_original()
        self.select()
        self.assertEqual(emusettings.read('gc'), '4')
        self.new_rules()
        self.assert_original()

    def test_failed_journal_durability_does_not_mutate_emulator(self):
        write = gamerules.persistence.write_json
        def fail_after_commit(path, data, **kwargs):
            result = write(path, data, **kwargs)
            if Path(path).name == 'emulator_undo.json':
                raise OSError('directory sync failed after replacement')
            return result
        with mock.patch.object(gamerules.persistence, 'write_json', side_effect=fail_after_commit):
            self.select()
        self.assert_original()
        self.new_rules()
        self.assert_original()

    def test_failed_legacy_rules_save_retains_legacy_and_durable_journal_for_retry(self):
        self.config.write_text(json.dumps({'rules': {}, 'base': {'gc': '02', 'other': 'keep'}}))
        emusettings.write('gc', 4)
        write = gamerules.persistence.write_json
        def fail_rules(path, data, **kwargs):
            if Path(path) == self.config:
                raise OSError('cannot finish handoff')
            return write(path, data, **kwargs)
        with mock.patch.object(gamerules.persistence, 'write_json', side_effect=fail_rules):
            self.new_rules()
        self.assertEqual(json.loads(self.config.read_text())['base']['gc'], '02')
        self.assertEqual(emusettings.read('gc'), '4')
        self.new_rules()
        self.assert_original()
        self.assertEqual(json.loads(self.config.read_text())['base'], {'other': 'keep'})

    def test_conflicting_legacy_shared_originals_preserve_evidence(self):
        legacy = {'gc': '02', 'wii': '3'}
        self.config.write_text(json.dumps({'base': legacy}))
        emusettings.write('gc', 4)
        self.new_rules()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertEqual(json.loads(self.config.read_text())['base'], legacy)

    def test_malformed_journal_is_not_replaced_or_used_to_override(self):
        journal = self.root / 'emulator_undo.json'
        journal.write_text('not a journal')
        rules, now = self.new_rules()
        self.select(now=now)
        self.assert_original()
        self.assertEqual(journal.read_text(), 'not a journal')

    def test_missing_or_ambiguous_original_is_not_created_or_replaced(self):
        for content in ('[Settings]\nOther=1\n', '[Settings]\nInternalResolution=2\nInternalResolution=3\n'):
            with self.subTest(content=content):
                self.actual.write_text(content)
                self.select()
                self.assertEqual(self.actual.read_text(), content)
                self.assertFalse((self.root / 'emulator_undo.json').exists())

    def test_invalid_compatible_saved_resolution_is_retained_but_not_applied(self):
        for resolution in (-1, 6, 2.5, True):
            with self.subTest(resolution=resolution):
                self.rules._rules['system:gc']['res'] = resolution
                self.rules._save()
                self.select()
                self.assert_original()
                self.assertEqual(json.loads(self.config.read_text())['rules']['system:gc']['res'], resolution)

    def test_retargeted_symlink_does_not_restore_old_original_into_new_file(self):
        self.select()
        alternate = self.root / 'alternate.ini'
        alternate.write_text('[Settings]\nInternalResolution=3\n')
        self.link.unlink()
        self.link.symlink_to(alternate.name)
        # Wii still proves the original physical target, so use only retargeted gc.
        with mock.patch.dict(emusettings.SPECS, {'wii': emusettings.SPECS['gc']}):
            self.rules.setEnabled(False)
        self.assertEqual(alternate.read_text(), '[Settings]\nInternalResolution=3\n')
        self.assertTrue((self.root / 'emulator_undo.json').exists())

    def launch_state(self, path='Fixture.iso'):
        from armadalive import nowplaying
        self.enterContext(mock.patch.object(nowplaying, '_rom_configuration', return_value=(str(self.root), {})))
        return str(self.root / 'gc' / path)

    def test_launch_hold_is_not_extended_and_timeout_recovers(self):
        path = self.launch_state()
        self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
        deadline = self.rules._launch_deadline
        self.now.state = {'kind': 'idle', 'playing': False, 'title': 'Metadata update'}
        self.now.changed.emit()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
        self.assertEqual(self.rules._launch_deadline, deadline)
        self.rules._launch_hold.timeout.emit()
        self.assert_original()

    def test_backup_pause_defers_expired_hold_recovery_until_resume(self):
        path = self.launch_state()
        self.rules.prepare_esde_launch('gc', path, 'Fixture')
        self.rules.pauseWrites(restoring=True)
        self.rules._launch_hold.timeout.emit()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertTrue(self.rules.prepare_esde_launch('gc', path, 'Fixture'))
        self.rules.resumeWrites()
        self.assert_original()

    def test_pending_proof_waits_only_when_resolution_can_change(self):
        path = self.launch_state()
        self.rules._rules = {'game:gc/Fixture': {'profile': 'Eco'}}
        self.rules._save()
        with mock.patch.object(self.now.identities, 'identity', return_value=None):
            self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
            self.rules._rules['system:gc'] = {'res': 3}
            self.assertIsNone(self.rules.prepare_esde_launch('gc', path, 'Fixture'))
        self.assert_original()

    def test_unproved_identity_uses_system_resolution_without_waiting(self):
        path = self.launch_state()
        self.rules._rules['game:gc/Fixture'] = {'res': 5}
        self.rules._save()
        with mock.patch.object(self.now.identities, 'identity', return_value=''):
            self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
        self.assertEqual(emusettings.read('gc'), '4')

    def test_late_proof_notifies_once_without_mutating_running_emulator(self):
        self.rules._rules = {'game:gc/Fixture': {'res': 5}}
        self.rules._save()
        seen = []
        self.rules.notice.connect(lambda code, detail: seen.append(code))
        self.now.state = dict(kind='game', key='esde:gc/Fixture.iso', system='gc', title='Fixture', playing=True)
        with mock.patch.object(self.now.identities, 'identity', return_value=None):
            self.now.changed.emit()
        with mock.patch.object(self.now.identities, 'identity', return_value='esde:gc/Fixture.iso'):
            self.now.identities.changed.emit()
            self.now.changed.emit()
        self.assert_original()
        self.assertEqual(seen.count('resolutionNextLaunch'), 1)

    def test_consumed_legacy_original_cannot_return_from_rules_backup(self):
        self.config.write_text(json.dumps({'rules': {}, 'base': {'gc': '02'}}))
        emusettings.write('gc', 4)
        self.new_rules()
        self.assert_original()
        emusettings.write('gc', 3)
        self.config.write_text('damaged primary')
        self.new_rules()
        self.assertEqual(emusettings.read('gc'), '3')

    def test_expired_launch_does_not_reapply_on_unrelated_selected_metadata(self):
        path = self.launch_state()
        from armadalive import nowplaying
        self.now.state, _ = nowplaying._esde_selection(dict(event='game-select', system='gc', path=path))
        self.rules.prepare_esde_launch('gc', path, 'Fixture')
        self.rules._launch_hold.timeout.emit()
        self.assert_original()
        self.now.state['title'] = 'Metadata arrived'
        self.now.changed.emit()
        self.assert_original()

    def test_handoff_preserves_each_rules_copy_and_retries_partial_backup_update(self):
        primary = {'rules': {'system:gc': {'title': 'current'}}, 'base': {'gc': '02', 'unknown': 'current-local'}}
        previous = {'rules': {'system:gc': {'title': 'previous'}}, 'base': {'gc': '02', 'unknown': 'previous-local'}, 'optional': 7}
        self.config.write_text(json.dumps(primary))
        backup_path = Path(str(self.config) + '.bak')
        backup_path.write_text(json.dumps(previous))
        backup_path.chmod(0o640)
        emusettings.write('gc', 4)
        write = gamerules.persistence.write_json
        def fail_backup(path, data, **kwargs):
            if Path(path) == backup_path:
                raise OSError('backup copy unavailable')
            return write(path, data, **kwargs)
        with mock.patch.object(gamerules.persistence, 'write_json', side_effect=fail_backup):
            self.new_rules()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertTrue((self.root / 'emulator_undo.json').exists())
        self.new_rules()
        self.assert_original()
        self.assertEqual(json.loads(self.config.read_text())['rules'], primary['rules'])
        previous['base'].pop('gc')
        self.assertEqual(json.loads(backup_path.read_text()), previous)
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o640)

    def test_restore_visible_before_failed_directory_sync_requires_durable_retry(self):
        self.select()
        self.rules.remove('system:gc')
        write = gamerules.persistence.write_bytes
        def fail_after_emulator_replacement(path, data, **kwargs):
            result = write(path, data, **kwargs)
            if Path(path) == self.actual:
                raise OSError('directory sync failed after replacement')
            return result
        with mock.patch.object(gamerules.persistence, 'write_bytes', side_effect=fail_after_emulator_replacement):
            self.select()
        self.assert_original()
        self.assertTrue((self.root / 'emulator_undo.json').exists())
        # The option already equals its original. Fail the explicit sync after
        # that no-write update, not an earlier handoff durability check.
        sync, update, attempted = gamerules.persistence._sync_directory, emusettings.update_setting, []
        def unchanged_update(*args):
            result = update(*args)
            attempted.append(True)
            return result
        def fail_restore_sync(path):
            if attempted:
                raise OSError('restore retry sync failed')
            return sync(path)
        with mock.patch.object(emusettings, 'update_setting', side_effect=unchanged_update), \
                mock.patch.object(gamerules.persistence, '_sync_directory', side_effect=fail_restore_sync):
            self.select()
        self.assertTrue(attempted)
        self.assertTrue((self.root / 'emulator_undo.json').exists())
        self.select()
        self.assertFalse((self.root / 'emulator_undo.json').exists())

    def test_pending_proof_with_same_resolution_does_not_delay_launch(self):
        path = self.launch_state()
        self.rules._rules['game:gc/Fixture'] = {'res': 4}
        self.rules._save()
        with mock.patch.object(self.now.identities, 'identity', return_value=None):
            self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
        self.assertEqual(emusettings.read('gc'), '4')

    def test_legacy_handoff_preserves_primary_symlink_and_mode(self):
        target = self.root / 'actual-rules.json'
        self.config.rename(target)
        self.config.symlink_to(target.name)
        target.write_text(json.dumps({'rules': {}, 'base': {'gc': '02'}}))
        target.chmod(0o640)
        emusettings.write('gc', 4)
        self.new_rules()
        self.assert_original()
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(target.read_text())['base'], {})

    def test_failed_journal_shrink_preserves_all_originals_through_retry(self):
        alternate = self.root / 'other.ini'
        alternate.write_text('[Settings]\nInternalResolution=5\n')
        emusettings.write('gc', 4)
        journal = self.root / 'emulator_undo.json'
        journal.write_text(json.dumps([
            [str(self.actual), 'Settings', 'InternalResolution', '02'],
            [str(alternate), 'Settings', 'InternalResolution', '3'],
        ]))
        write, self.real_replace = gamerules.persistence.write_json, os.replace
        def fail_after_shrink(path, data, **kwargs):
            result = write(path, data, **kwargs)
            if Path(path) == journal and len(data) == 1:
                raise OSError('directory sync failed after shrink')
            return result
        with mock.patch.dict(emusettings.SPECS, {'psp': (alternate, 'Settings', 'InternalResolution', '{}')}):
            with mock.patch('os.replace', side_effect=self.fail_emulator_replace), \
                    mock.patch.object(gamerules.persistence, 'write_json', side_effect=fail_after_shrink):
                rules, now = self.new_rules()
            self.assertEqual(len(json.loads(journal.read_text())), 1)
            self.select('wii', now=now)
            self.assertEqual(emusettings.read('gc'), '5')
            self.new_rules()
            self.assert_original()
            self.assertEqual(emusettings.read('psp'), '3')


    def test_handoff_retires_lexical_and_real_backups_for_a_symlink_primary(self):
        target = self.root / 'actual-rules.json'
        self.config.rename(target)
        self.config.symlink_to(target.name)
        target.write_text(json.dumps({'rules': {}, 'base': {'gc': '02'}}))
        lexical = Path(str(self.config) + '.bak')
        real = Path(str(target) + '.bak')
        copies = [(lexical, {'rules': {'system:gc': {'title': 'lexical'}},
                            'base': {'gc': '02', 'unknown': 'lexical-local', 'ps2': None}}),
                  (real, {'rules': {'system:gc': {'title': 'real'}},
                          'base': {'gc': '02', 'unknown': 'real-local', 'ps2': None}})]
        for path, saved in copies:
            path.write_text(json.dumps(saved))
            path.chmod(0o640)
        emusettings.write('gc', 4)
        self.new_rules()
        self.assert_original()
        for path, saved in copies:
            with self.subTest(path=path.name):
                saved['base'].pop('gc')
                self.assertEqual(json.loads(path.read_text()), saved)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        emusettings.write('gc', 3)
        target.write_text('damaged primary')
        self.new_rules()
        self.assertEqual(emusettings.read('gc'), '3')
        self.assertTrue(self.config.is_symlink())

    def test_unrelated_observed_play_keeps_the_prepared_identity_hold(self):
        path = self.launch_state()
        self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
        held, deadline = self.rules._launch_key, self.rules._launch_deadline
        self.now.state = dict(kind='game', key='esde:psp/Other.iso', system='psp', playing=True)
        self.now.running = True
        self.now.changed.emit()
        self.assertEqual(emusettings.read('gc'), '4')
        self.assertEqual(self.rules._launch_key, held)
        self.assertEqual(self.rules._launch_deadline, deadline)
        self.now.state = dict(kind='game', key=held, system='gc', playing=True)
        self.now.changed.emit()
        self.assertEqual(self.rules._launch_key, '')
        self.assertEqual(emusettings.read('gc'), '4')

    def test_unrelated_observed_play_still_allows_hold_timeout_recovery(self):
        path = self.launch_state()
        self.rules.prepare_esde_launch('gc', path, 'Fixture')
        self.now.state = dict(kind='game', key='esde:psp/Other.iso', system='psp', playing=True)
        self.now.running = True
        self.now.changed.emit()
        self.assertEqual(emusettings.read('gc'), '4')
        self.rules._launch_hold.timeout.emit()
        self.assert_original()

    def test_running_game_does_not_block_noop_launch_with_disabled_or_absent_rules(self):
        path = self.launch_state()
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.rules._rules = {}
                self.rules.setEnabled(enabled)
                self.now.state = dict(kind='game', key='esde:gc/Other.iso', system='gc', playing=True)
                self.now.running = True
                self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
                self.assert_original()

    def test_running_game_still_blocks_required_override_or_restoration(self):
        path = self.launch_state()
        self.now.state = dict(kind='game', key='esde:gc/Other.iso', system='gc', playing=True)
        self.now.running = True
        self.assertTrue(self.rules.prepare_esde_launch('gc', path, 'Fixture'))
        self.assert_original()
        self.now.running = False
        self.select()
        self.now.state['playing'] = True
        self.now.running = True
        self.rules.setEnabled(False)
        self.assertTrue(self.rules.prepare_esde_launch('gc', path, 'Fixture'))
        self.assertEqual(emusettings.read('gc'), '4')

    def assert_late_removal_notifies(self, rule):
        self.select()  # Prepare system resolution 4 and retain original 02.
        self.rules._rules['game:gc/Fixture'] = rule
        self.rules._save()
        seen = []
        self.rules.notice.connect(lambda code, detail: seen.append(code))
        self.now.state = dict(kind='game', key='esde:gc/Fixture.iso', system='gc', title='Fixture', playing=True)
        self.now.running = True
        with mock.patch.object(self.now.identities, 'identity', return_value=None):
            self.now.changed.emit()
        with mock.patch.object(self.now.identities, 'identity', return_value='esde:gc/Fixture.iso'):
            self.now.identities.changed.emit()
            self.now.changed.emit()
            self.assertEqual(emusettings.read('gc'), '4')
            self.assertEqual(seen.count('resolutionNextLaunch'), 1)
            self.now.running = False
            self.now.state['playing'] = False
            self.now.changed.emit()
        self.assert_original()

    def test_profile_only_late_proof_announces_removal_without_running_mutation(self):
        self.assert_late_removal_notifies({'profile': 'Eco'})

    def test_zero_resolution_late_proof_announces_removal_without_running_mutation(self):
        self.assert_late_removal_notifies({'res': 0})


    def test_launch_can_prepare_idle_target_without_touching_running_target(self):
        path = self.launch_state()
        other = self.root / 'other.ini'
        other.write_text('[Settings]\nInternalResolution=3\n')
        with mock.patch.dict(emusettings.SPECS, {'psp': (other, 'Settings', 'InternalResolution', '{}')}):
            self.rules._rules['system:psp'] = {'res': 5}
            self.rules._save()
            self.select('psp')
            self.now.state['playing'] = True
            self.now.running = True
            self.assertEqual(self.rules.prepare_esde_launch('gc', path, 'Fixture'), '')
            self.assertEqual(other.read_text(), '[Settings]\nInternalResolution=5\n')
            self.assertEqual(emusettings.read('gc'), '4')
            self.now.running = False
            self.now.state = {'kind': 'idle', 'playing': False}
            self.rules._launch_hold.timeout.emit()
            self.assert_original()
            self.assertEqual(other.read_text(), '[Settings]\nInternalResolution=3\n')


if __name__ == "__main__":
    unittest.main()
