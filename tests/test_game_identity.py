"""Canonical identities cross metadata, saved history and acknowledged game rules."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QObject, Signal

from tests.common import app, dispose_qt, touch, wait_for
from armadalive import esdelib, gameinfo, gamerules, history, nowplaying as n, persistence
from tests.test_gamerules import FakeGs, FakeSys


class CurrentGame(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.state = dict(kind="idle", key="", playing=False)
        self.identities = n.LegacyIdentities(self)
        self.processes_known = True

    def select(self, path, *, playing=False):
        self.state, _ = n._esde_selection(dict(event="game-start" if playing else "game-select",
                                              system="snes", path=path, name="Event title"))
        self.state["metadataPending"] = True
        self.changed.emit()


class IdentityIntegration(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix="armadalive-identity-"))
        self.root = Path(self.directory)
        self.roms = self.root / "roms" / "snes"
        self.roms.mkdir(parents=True)
        self.gamelists = str(self.root / "gamelists")
        self.enterContext(mock.patch.object(n, "ES", self.directory))
        self.enterContext(mock.patch.object(n, "GAMELISTS", self.gamelists))
        self.enterContext(mock.patch.object(n, "MEDIA", str(self.root / "media")))
        self.enterContext(mock.patch.object(esdelib, "CACHE", str(self.root / "no-bundled-cache")))
        self.enterContext(mock.patch.object(gamerules, "CONFIG", str(self.root / "rules.json")))
        touch(str(self.root / "settings" / "es_settings.xml"),
              f'<string name="ROMDirectory" value="{self.roms.parent}" />')
        self.now = CurrentGame()
        self.addCleanup(dispose_qt, self.now)
        # Resolution writes belong to the next task and never reach host files.
        self.enterContext(mock.patch.object(gamerules.emusettings, "read", return_value="1"))
        self.enterContext(mock.patch.object(gamerules.emusettings, "write"))

    def rom(self, relative):
        path = self.roms / relative
        touch(str(path))
        return str(path)

    def tracker(self, games, *, imported=True, sessions=()):
        self.path = self.root / "history.json"
        self.path.write_text(json.dumps(dict(imported=imported, games=games, sessions=list(sessions))))
        tracker = history.History(self.now, str(self.path), gamelists=self.gamelists)
        self.addCleanup(self.close_tracker, tracker)
        return tracker

    def close_tracker(self, tracker):
        tracker.flush()
        dispose_qt(tracker)

    def rules(self, records):
        (self.root / "rules.json").write_text(json.dumps(dict(rules=records)))
        self.gs, self.system = FakeGs(), FakeSys()
        rules = gamerules.GameRules(self.now, self.gs, self.system)
        self.addCleanup(dispose_qt, rules)
        return rules

    def test_queued_proof_tracks_a_dangling_directory_target_appearing(self):
        self.rom("Game.sfc")
        outside = self.root / "outside"
        (self.roms / "linked").symlink_to(outside, target_is_directory=True)
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        proof._refresh_timer.stop()
        captured = n._read_proofs(proof._request[0])
        touch(str(outside / "Game.sfc"))
        proof._ready(captured)
        self.assertIsNone(proof.identity("snes/Game"))
        wait_for(lambda: proof.identity("snes/Game") == "")

    def test_queued_proof_tracks_a_dangling_file_target_appearing(self):
        self.rom("Game.sfc")
        target = self.root / "outside.sfc"
        nested = self.roms / "nested"
        nested.mkdir()
        (nested / "Game.sfc").symlink_to(target)
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        proof._refresh_timer.stop()
        captured = n._read_proofs(proof._request[0])
        touch(str(target))
        proof._ready(captured)
        self.assertIsNone(proof.identity("snes/Game"))
        wait_for(lambda: proof.identity("snes/Game") == "")

    def test_deferred_history_proof_tracks_a_previously_missing_link_target(self):
        self.rom("Game.sfc")
        outside = self.root / "outside"
        (self.roms / "linked").symlink_to(outside, target_is_directory=True)
        tracker = self.tracker({"snes/Game": dict(total=11, favorite=True)})
        tracker.pauseWrites(restoring=True)
        tracker.flush()
        wait_for(lambda: not self.now.identities.history_pending())
        touch(str(outside / "Game.sfc"))
        tracker.resumeWrites()
        self.assertIn("snes/Game", tracker.games)
        self.assertNotIn("esde:snes/Game.sfc", tracker.games)
        wait_for(lambda: not tracker._history_proof_pending)

    def test_unreadable_link_target_cannot_match_previously_observed_absence(self):
        self.rom("Game.sfc")
        linked = self.roms / "linked"
        linked.symlink_to(self.root / "outside", target_is_directory=True)
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        wait_for(lambda: proof.identity("snes/Game") == "esde:snes/Game.sfc")
        stamp = n._file_stamp

        def unreadable(path):
            if path == str(linked):
                raise PermissionError("target cannot be inspected")
            return stamp(path)
        with mock.patch.object(n, "_file_stamp", side_effect=unreadable):
            self.assertIsNone(proof.identity("snes/Game"))

    def test_symlink_target_changing_from_file_to_directory_invalidates_proof(self):
        self.rom("Game.sfc")
        target = self.root / "outside"
        touch(str(target))
        (self.roms / "linked").symlink_to(target)
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        proof._refresh_timer.stop()
        captured = n._read_proofs(proof._request[0])
        target.unlink()
        touch(str(target / "Game.sfc"))
        proof._ready(captured)
        self.assertIsNone(proof.identity("snes/Game"))
        wait_for(lambda: proof.identity("snes/Game") == "")

    def test_queued_proof_is_rejected_when_an_existing_subdirectory_gains_a_sibling(self):
        self.rom("a/Game.sfc")
        (self.roms / "b").mkdir()
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        proof._refresh_timer.stop()
        captured = n._read_proofs(proof._request[0])
        self.rom("b/Game.sfc")
        proof._ready(captured)
        self.assertIsNone(proof.identity("snes/Game"))
        wait_for(lambda: proof.identity("snes/Game") == "")

    def test_rule_proof_is_revalidated_after_restore_freeze_before_migration(self):
        self.rom("a/Game.sfc")
        (self.roms / "b").mkdir()
        rules = self.rules({"game:snes/Game": dict(fps=30)})
        rules.pauseWrites(restoring=True)
        wait_for(lambda: self.now.identities.identity("snes/Game") == "esde:snes/a/Game.sfc")
        self.rom("b/Game.sfc")
        rules.resumeWrites()
        self.assertIn("game:snes/Game", rules._rules)
        self.assertNotIn("game:esde:snes/a/Game.sfc", rules._rules)
        wait_for(lambda: self.now.identities.identity("snes/Game") == "")

    def test_history_proof_is_revalidated_after_freeze_before_migration(self):
        self.rom("a/Game.sfc")
        (self.roms / "b").mkdir()
        tracker = self.tracker({"snes/Game": dict(total=9, favorite=True)})
        tracker.pauseWrites(restoring=True)
        tracker.flush()
        wait_for(lambda: not self.now.identities.history_pending())
        self.rom("b/Game.sfc")
        tracker.resumeWrites()
        self.assertIn("snes/Game", tracker.games)
        self.assertNotIn("esde:snes/a/Game.sfc", tracker.games)
        wait_for(lambda: not tracker._history_proof_pending)

    def test_failed_rule_write_revalidates_nested_dependencies_before_retry(self):
        self.rom("a/Game.sfc")
        (self.roms / "b").mkdir()
        rules = self.rules({"game:snes/Game": dict(fps=30)})
        with mock.patch.object(persistence, "write_json", side_effect=OSError("controlled failure")):
            wait_for(lambda: self.now.identities.identity("snes/Game") == "esde:snes/a/Game.sfc")
        self.rom("b/Game.sfc")
        self.now.identities.changed.emit()
        self.assertIn("game:snes/Game", rules._rules)
        self.assertNotIn("game:esde:snes/a/Game.sfc", rules._rules)
        wait_for(lambda: self.now.identities.identity("snes/Game") == "")

    def test_proof_includes_presentation_excluded_and_linked_directories(self):
        self.rom("Game.sfc")
        for folder in ("media", "images", "videos", "manuals", ".hidden"):
            with self.subTest(folder=folder):
                sibling = self.roms / folder / "Game.sfc"
                touch(str(sibling))
                self.assertEqual(n.resolve_legacy_keys({"snes/Game"}, self.gamelists), {})
                sibling.unlink()
        outside = self.root / "outside"
        touch(str(outside / "Game.sfc"))
        (self.roms / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual(n.resolve_legacy_keys({"snes/Game"}, self.gamelists), {})
        # Presentation filtering stays distinct from exhaustive legacy proof.
        definitions = {"snes": dict(path=str(self.roms), extensions={".sfc"})}
        self.assertEqual([row["id"] for row in esdelib.games(definitions)["snes"]], ["esde:snes/Game.sfc"])
        self.assertEqual(n.rom_relative("snes", str(self.roms / "linked" / "Game.sfc")), "linked/Game.sfc")

    def test_directory_cycles_and_duplicate_targets_fail_closed_without_hiding_canonical_paths(self):
        self.rom("Game.sfc")
        (self.roms / "cycle").symlink_to(self.roms, target_is_directory=True)
        with mock.patch.object(os, "scandir", wraps=os.scandir) as scan:
            self.assertEqual(n.resolve_legacy_keys({"snes/Game"}, self.gamelists), {})
            self.assertLessEqual(scan.call_count, 2)
        self.assertEqual(n.rom_relative("snes", str(self.roms / "cycle" / "Game.sfc")), "cycle/Game.sfc")
        (self.roms / "cycle").unlink()
        outside = self.root / "outside"
        touch(str(outside / "Another.sfc"))
        for name in ("first", "second"):
            (self.roms / name).symlink_to(outside, target_is_directory=True)
        self.assertEqual(n.resolve_legacy_keys({"snes/Another"}, self.gamelists), {})

    def test_shared_custom_roots_infer_only_a_unique_compatible_system(self):
        path = self.rom("Game.gba")
        definitions = {
            "gb": dict(path=str(self.roms), extensions={".gb"}),
            "gba": dict(path=str(self.roms), extensions={".gba"}),
        }
        for ordered in (definitions, dict(reversed(list(definitions.items())))):
            self.assertEqual(n._rom_path(path, str(self.roms.parent), n._custom_folders(str(self.roms.parent), ordered)),
                             ("gba", "Game.gba"))
        self.assertEqual([game["id"] for games in esdelib.games(definitions).values() for game in games], ["esde:gba/Game.gba"])
        definitions["gb"]["extensions"] = {".gba"}
        self.assertEqual(n._rom_path(path, str(self.roms.parent), n._custom_folders(str(self.roms.parent), definitions)), ("", ""))
        self.assertEqual(n.rom_relative("gb", path, str(self.roms)), "Game.gba")

    def test_equivalent_physical_custom_roots_do_not_choose_by_alias_spelling(self):
        path = self.rom("Game.gba")
        alias = self.root / "a-much-longer-alias"
        alias.symlink_to(self.roms, target_is_directory=True)
        definitions = {
            "gb": dict(path=str(alias), extensions={".gb"}),
            "gba": dict(path=str(self.roms), extensions={".gba"}),
        }
        roots = n._custom_folders(str(self.roms.parent), definitions)
        for spelling in (path, str(alias / "Game.gba")):
            self.assertEqual(n._rom_path(spelling, str(self.roms.parent), roots), ("gba", "Game.gba"))
        definitions["gb"]["extensions"] = {".gba"}
        roots = n._custom_folders(str(self.roms.parent), definitions)
        for spelling in (path, str(alias / "Game.gba")):
            self.assertEqual(n._rom_path(spelling, str(self.roms.parent), roots), ("", ""))

    def test_nested_custom_root_wins_but_a_nested_alias_of_the_same_root_is_ambiguous(self):
        path = self.rom("nested/Game.sfc")
        definitions = {
            "snes": dict(path=str(self.roms), extensions={".sfc"}),
            "other": dict(path=str(self.roms / "nested"), extensions={".sfc"}),
        }
        root = str(self.roms.parent)
        self.assertEqual(n._rom_path(path, root, n._custom_folders(root, definitions)), ("other", "Game.sfc"))
        self.rom("Game.sfc")
        alias = self.roms / "alias"
        alias.symlink_to(self.roms, target_is_directory=True)
        definitions["other"]["path"] = str(alias)
        self.assertEqual(n._rom_path(str(alias / "Game.sfc"), root, n._custom_folders(root, definitions)), ("", ""))

    def test_slow_changed_stat_uses_budget_retry_instead_of_immediate_refresh(self):
        self.rom("nested/Game.sfc")
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        proof._refresh_timer.stop()
        captured = n._read_proofs(proof._request[0])
        stamp, clock = n._proof_stamp, [0.0]

        def slow_changed(path):
            if path == str(self.roms / "nested"):
                clock[0] = 0.025
                return (0, 0, 0, 0)
            return stamp(path)
        with mock.patch.object(n, "_proof_stamp", side_effect=slow_changed), \
                mock.patch.object(n.time, "monotonic", side_effect=lambda: clock[0]):
            proof._ready(captured)
        self.assertEqual(proof.identity("snes/Game"), "")
        self.assertEqual(proof._refresh_timer.interval(), 15000)

    def test_dependency_budget_rejects_positive_proof_without_immediate_retry(self):
        self.rom("Game.sfc")
        proof = self.now.identities
        proof.watch_rules({"snes/Game"})
        proof._refresh_timer.stop()
        captured = n._read_proofs(proof._request[0])
        clock = iter(index * 0.021 for index in range(100))
        with mock.patch.object(n.time, "monotonic", side_effect=lambda: next(clock)):
            proof._ready(captured)
        self.assertEqual(proof.identity("snes/Game"), "")
        self.assertTrue(proof._refresh_timer.isActive())
        self.assertEqual(proof._refresh_timer.interval(), 15000)
        with mock.patch.object(n, "_read_proofs") as read:
            app.processEvents()
            read.assert_not_called()

    def test_removing_the_last_pending_rule_notifies_without_an_empty_worker(self):
        self.rom("Game.sfc")
        entered, release = threading.Event(), threading.Event()
        resolve, observed = n.resolve_legacy_keys, []
        self.now.identities.changed.connect(lambda: observed.append(self.now.identities.identity("snes/Game")))

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return resolve(*args, **kwargs)
        with mock.patch.object(n, "resolve_legacy_keys", side_effect=delayed) as reads:
            self.now.identities.watch_rules({"snes/Game"})
            try:
                wait_for(entered.is_set)
                self.now.identities.watch_rules(())
                wait_for(lambda: "" in observed)
                self.assertEqual(reads.call_count, 1)
                self.assertFalse(self.now.identities._refresh_timer.isActive())
            finally:
                release.set()
                wait_for(lambda: not self.now.identities._poller._busy)

    def test_no_proof_worker_is_scheduled_without_consumers_or_after_migration(self):
        with mock.patch.object(n, "_read_proofs", wraps=n._read_proofs) as reads:
            app.processEvents()
            self.assertEqual(reads.call_count, 0)
            self.assertFalse(self.now.identities._poller._timer.isActive())
            self.assertFalse(self.now.identities._refresh_timer.isActive())
            self.rom("Game.sfc")
            tracker = self.tracker({"snes/Game": dict(total=9)})
            rules = self.rules({"game:snes/Game": dict(fps=30)})
            wait_for(lambda: "game:esde:snes/Game.sfc" in rules._rules and not tracker._history_proof_pending)
            app.processEvents()
            self.assertEqual(reads.call_count, 1)
            self.assertFalse(self.now.identities._refresh_timer.isActive())
            self.assertFalse(self.now.identities._poller._timer.isActive())
            self.assertFalse(self.now.identities._poller._busy)

    def test_nested_directory_change_during_proof_invalidates_its_result(self):
        self.rom("a/Game.sfc")
        (self.roms / "b").mkdir()
        entered, release = threading.Event(), threading.Event()
        resolve = n.resolve_legacy_keys

        def delayed(*args, **kwargs):
            result = resolve(*args, **kwargs)
            entered.set()
            release.wait(3)
            return result
        with mock.patch.object(n, "resolve_legacy_keys", side_effect=delayed):
            self.now.identities.watch_rules({"snes/Game"})
            try:
                wait_for(entered.is_set)
                self.rom("b/Game.sfc")
            finally:
                release.set()
            wait_for(lambda: self.now.identities.identity("snes/Game") == "")

    def test_proved_rule_applies_while_presentation_is_blocked(self):
        from tests.common import Fake
        path = self.rom("Game.sfc")
        entered, release = threading.Event(), threading.Event()
        read = n._read_metadata

        def delayed(request):
            if request is not None and request[0] == "game":
                entered.set()
                release.wait(3)
            return read(request)
        with (mock.patch.object(n, "_read_metadata", side_effect=delayed),
              mock.patch.object(n, "running_game", return_value=None),
              mock.patch.object(n, "esde_running", return_value=True),
              mock.patch.object(n, "steam_running", return_value=False),
              mock.patch.object(n, "STATE", str(self.root / "no-events"))):
            backend = n.NowPlaying(Fake(), Fake())
            (self.root / "rules.json").write_text('{"rules":{"game:snes/Game":{"fps":30}}}')
            display = FakeGs()
            rules = gamerules.GameRules(backend, display, FakeSys())
            try:
                backend._esde = True
                backend._event = dict(event="game-start", system="snes", path=path, name="Event title")
                backend._update()
                wait_for(lambda: entered.is_set() and display.set == [30])
                self.assertTrue(backend.state["playing"])
                self.assertTrue(backend.state["metadataPending"])
                self.assertIn("game:esde:snes/Game.sfc", rules._rules)
            finally:
                release.set()
                dispose_qt(rules)
                dispose_qt(backend)

    def test_late_presentation_cannot_overwrite_replaced_history(self):
        self.now.select(self.rom("Game.sfc"), playing=True)
        tracker = self.tracker({})
        tracker.flush()
        replacement = '{"imported":true,"games":{"steam/99":{"total":99}},"sessions":[]}'
        self.path.write_text(replacement)
        self.now.state.update(metadataPending=False, title="Scraped name", fanart="file:///new.jpg")
        self.now.changed.emit()
        tracker.flush()
        self.assertEqual(self.path.read_text(), replacement)
        self.assertEqual(tracker.games["esde:snes/Game.sfc"]["title"], "Event title")

    def test_late_presentation_waits_for_its_own_pending_write(self):
        entered, release = threading.Event(), threading.Event()
        write = persistence.write_json
        tracker = self.tracker({})

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return write(*args, **kwargs)
        with mock.patch.object(persistence, "write_json", side_effect=delayed):
            try:
                self.now.select(self.rom("Game.sfc"), playing=True)
                wait_for(entered.is_set)
                self.now.state.update(metadataPending=False, title="Scraped name", fanart="file:///new.jpg")
                self.now.changed.emit()
                self.assertEqual(tracker.games["esde:snes/Game.sfc"]["title"], "Event title")
            finally:
                release.set()
            wait_for(lambda: tracker.games["esde:snes/Game.sfc"]["title"] == "Scraped name")
        tracker.flush()
        self.assertEqual(tracker.games["esde:snes/Game.sfc"]["plays"], 1)

    def test_rule_migration_retries_after_replacement_succeeds_but_directory_sync_fails(self):
        path = self.rom("Game.sfc")
        rules = self.rules({"game:snes/Game": dict(fps=30)})
        rules_path = str(self.root / "rules.json")
        original, sync = n._stamp(rules_path), persistence._sync_directory

        def fail_commit(directory):
            if directory == str(self.root) and n._stamp(rules_path) != original:
                raise OSError("controlled directory sync failure")
            return sync(directory)
        self.now.select(path, playing=True)
        with mock.patch.object(persistence, "_sync_directory", side_effect=fail_commit):
            wait_for(lambda: self.gs.set == [30])
            self.assertIn("game:snes/Game", rules._rules)
        self.now.identities.changed.emit()
        self.assertIn("game:esde:snes/Game.sfc", rules._rules)
        self.assertEqual(self.gs.set, [30])

    def test_rule_and_history_proof_share_one_scan_and_metadata_does_not_scan(self):
        path = self.rom("nested/Shared.sfc")
        self.rom("Other.sfc")
        with mock.patch.object(n, "resolve_legacy_keys", wraps=n.resolve_legacy_keys) as resolve:
            tracker = self.tracker({"snes/Shared": dict(total=9, favorite=True)})
            rules = self.rules({"game:snes/Shared": dict(fps=30), "game:snes/Other": dict(fps=40)})
            wait_for(lambda: "esde:snes/nested/Shared.sfc" in tracker.games and "game:esde:snes/Other.sfc" in rules._rules)
            app.processEvents()
            n.describe(dict(event="game-select", system="snes", path=path))
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(resolve.call_args.args, ({"snes/Shared", "snes/Other"}, self.gamelists))

    def test_dotted_stems_hidden_unlisted_and_unreadable_siblings_are_not_claimed(self):
        for relative in ("a/Shared.v1.sfc", "b/Shared.v1.zip", "Hidden.sfc", "else/Hidden.sfc", "Unique.sfc"):
            self.rom(relative)
        touch(f"{self.gamelists}/snes/gamelist.xml", '<gameList><game><path>./else/Hidden.sfc</path><hidden>true</hidden></game></gameList>')
        keys = {"snes/Shared.v1", "snes/Hidden", "snes/Unique"}
        self.assertEqual(set(n.resolve_legacy_keys(keys, self.gamelists)), {"snes/Unique"})

        def unreadable(*args, **kwargs):
            yield "Unique.sfc"
            raise PermissionError("unreadable sibling directory")
        with mock.patch.object(esdelib, "rom_files", side_effect=unreadable):
            self.assertEqual(n.resolve_legacy_keys(keys, self.gamelists), {})
        touch(f"{self.gamelists}/snes/gamelist.xml", '<broken')
        self.assertEqual(n.resolve_legacy_keys(keys, self.gamelists), {})

    def test_retarget_while_scan_is_blocked_never_publishes_the_old_proof(self):
        old = self.root / "old"
        new = self.root / "new"
        touch(str(old / "Game.sfc"))
        touch(str(new / "Game.sfc"))
        touch(str(new / "another" / "Game.sfc"))
        link = self.root / "linked"
        link.symlink_to(old, target_is_directory=True)
        touch(str(self.root / "custom_systems" / "es_systems.xml"),
              f'<systemList><system><name>snes</name><path>{link}</path><extension>.sfc</extension></system></systemList>')
        entered, release = threading.Event(), threading.Event()
        resolve = n.resolve_legacy_keys
        results = []
        self.now.identities.changed.connect(lambda: results.append(self.now.identities.identity("snes/Game")))

        def delayed(*args, **kwargs):
            result = resolve(*args, **kwargs)
            entered.set()
            release.wait(3)
            return result
        with mock.patch.object(n, "resolve_legacy_keys", side_effect=delayed):
            self.now.identities.watch_rules({"snes/Game"})
            try:
                wait_for(entered.is_set)
                link.unlink()
                link.symlink_to(new, target_is_directory=True)
                self.assertIsNone(self.now.identities.identity("snes/Game"))
            finally:
                release.set()
            wait_for(lambda: self.now.identities.identity("snes/Game") == "")
        self.assertNotIn("esde:snes/Game.sfc", results)

    def test_history_merges_once_without_restarting_the_open_session_or_reversing_favorite(self):
        path = self.rom("Region/Game.sfc")
        key = "esde:snes/Region/Game.sfc"
        self.now.select(path, playing=True)
        tracker = self.tracker({"snes/Game": dict(total=100, plays=4, favorite=True),
                                key: dict(total=20, plays=2, favorite=False)},
                               sessions=[["snes/Game", int(time.time()), 100]])
        opened = tracker._open
        wait_for(lambda: "snes/Game" not in tracker.games)
        self.assertIs(tracker._open, opened)
        self.assertEqual(tracker.games[key]["total"], 120)
        self.assertEqual(tracker.games[key]["plays"], 7)
        self.assertFalse(tracker.games[key]["favorite"])
        self.assertEqual([row[0] for row in tracker._sessions], [key, key])
        self.now.identities.changed.emit()
        self.assertEqual(tracker.games[key]["total"], 120)
        tracker.flush()
        self.assertEqual(json.loads(self.path.read_text())["games"][key]["plays"], 7)

    def test_ambiguous_favorite_keeps_its_data_but_cannot_launch_the_last_sibling(self):
        first = self.rom("a/Same.sfc")
        self.rom("b/Same.sfc")
        tracker = self.tracker({"snes/Same": dict(total=14, path=first, favorite=True)})
        wait_for(lambda: not tracker._history_proof_pending)
        self.assertEqual(tracker.games["snes/Same"]["path"], first)
        self.assertEqual(tracker.favorites()[0][1]["path"], "")
        self.assertEqual(tracker.games["snes/Same"]["total"], 14)

    def test_legacy_history_requires_equivalent_existing_absolute_location(self):
        path = self.rom("Game.sfc")
        other = self.root / "other" / "roms" / "snes" / "Game.sfc"
        touch(str(other))
        tracker = self.tracker({"snes/Game": dict(total=8, path=str(other), favorite=True)})
        wait_for(lambda: not tracker._history_proof_pending)
        self.assertIn("snes/Game", tracker.games)
        self.assertNotIn("esde:snes/Game.sfc", tracker.games)
        self.assertEqual(n.rom_relative("snes", path), "Game.sfc")
        alias = self.root / "same"
        alias.symlink_to(self.roms, target_is_directory=True)
        self.assertEqual(n.rom_relative("snes", str(alias / "Game.sfc")), "Game.sfc")

    def test_pending_proof_survives_an_ordinary_background_save(self):
        self.rom("Game.sfc")
        entered, release = threading.Event(), threading.Event()
        resolve = n.resolve_legacy_keys

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return resolve(*args, **kwargs)
        with mock.patch.object(n, "resolve_legacy_keys", side_effect=delayed):
            tracker = self.tracker({"snes/Game": dict(total=8, favorite=True)})
            try:
                wait_for(entered.is_set)
                tracker.toggleFavorite("steam/42", "Another game", "steam", "")
                tracker.flush()
            finally:
                release.set()
            wait_for(lambda: "esde:snes/Game.sfc" in tracker.games)
        tracker.flush()
        saved = json.loads(self.path.read_text())["games"]
        self.assertEqual(saved["esde:snes/Game.sfc"]["total"], 8)
        self.assertTrue(saved["steam/42"]["favorite"])

    def test_write_completion_cannot_bless_an_external_replacement_for_pending_migration(self):
        self.rom("Game.sfc")
        proof_entered, proof_release = threading.Event(), threading.Event()
        sync_entered, sync_release = threading.Event(), threading.Event()
        resolve, sync = n.resolve_legacy_keys, persistence._sync_directory
        replacement = dict(imported=True, games={"steam/99": dict(total=99)}, sessions=[])

        def delayed_proof(*args, **kwargs):
            proof_entered.set()
            proof_release.wait(3)
            return resolve(*args, **kwargs)
        with mock.patch.object(n, "resolve_legacy_keys", side_effect=delayed_proof):
            tracker = self.tracker({"snes/Game": dict(total=8)})
            original = n._stamp(str(self.path))

            def delayed_sync(directory):
                if directory == str(self.root) and n._stamp(str(self.path)) != original:
                    sync_entered.set()
                    sync_release.wait(3)
                return sync(directory)
            try:
                wait_for(proof_entered.is_set)
                with mock.patch.object(persistence, "_sync_directory", side_effect=delayed_sync):
                    tracker.toggleFavorite("steam/42", "Another game", "steam", "")
                    wait_for(sync_entered.is_set)
                    restored = self.root / "restored.json"
                    restored.write_text(json.dumps(replacement))
                    os.replace(restored, self.path)
                    sync_release.set()
                    tracker.flush()
                proof_release.set()
                wait_for(lambda: not tracker._history_proof_pending)
                self.assertNotEqual(tracker._file_version, n._stamp(str(self.path)))
                self.assertEqual(json.loads(self.path.read_text()), replacement)
                self.assertNotIn("esde:snes/Game.sfc", tracker.games)
            finally:
                sync_release.set()
                proof_release.set()

    def test_history_proof_is_frozen_through_restore_and_replayed_after_rollback(self):
        self.rom("Game.sfc")
        entered, release = threading.Event(), threading.Event()
        resolve = n.resolve_legacy_keys

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return resolve(*args, **kwargs)
        with mock.patch.object(n, "resolve_legacy_keys", side_effect=delayed):
            tracker = self.tracker({"snes/Game": dict(total=8, favorite=True)})
            try:
                wait_for(entered.is_set)
                tracker.pauseWrites(restoring=True)
                tracker.flush()
                rollback = self.path.read_bytes()
                replacement = '{"imported":true,"games":{"steam/99":{"total":99}},"sessions":[]}'
                self.path.write_text(replacement)
                release.set()
                wait_for(lambda: not self.now.identities.history_pending())
                self.assertEqual(self.path.read_text(), replacement)
                self.assertIn("snes/Game", tracker.games)
                self.path.write_bytes(rollback)
                tracker.resumeWrites()
                wait_for(lambda: "esde:snes/Game.sfc" in tracker.games)
            finally:
                release.set()

    def test_first_import_waits_for_migration_without_adding_the_same_playcount_twice(self):
        self.rom("Game.sfc")
        touch(f"{self.gamelists}/snes/gamelist.xml", '<gameList><game><path>./Game.sfc</path>'
              '<lastplayed>20261105T192400</lastplayed><playcount>6</playcount></game></gameList>')
        tracker = self.tracker({"snes/Game": dict(total=88, plays=6)}, imported=False)
        wait_for(lambda: tracker._imported)
        tracker.flush()
        self.assertNotIn("snes/Game", tracker.games)
        self.assertEqual((tracker.games["esde:snes/Game.sfc"]["plays"], tracker.games["esde:snes/Game.sfc"]["total"]), (6, 88))

    def test_canonical_rule_wins_and_delayed_proof_does_not_apply_it_twice(self):
        path = self.rom("Game.sfc")
        rules = self.rules({"game:snes/Game": dict(fps=20), "game:esde:snes/Game.sfc": dict(fps=40)})
        self.now.select(path, playing=True)
        self.assertEqual(self.gs.set, [40])
        wait_for(lambda: self.now.identities.identity("snes/Game") is not None)
        self.assertEqual(self.gs.set, [40])
        self.assertEqual(rules._rules["game:esde:snes/Game.sfc"]["fps"], 40)
        self.assertIn("game:snes/Game", rules._rules)

    def test_migration_failure_keeps_legacy_rule_usable_and_retries_without_reapplying(self):
        path = self.rom("Game.sfc")
        rules = self.rules({"game:snes/Game": dict(fps=30)})
        self.now.select(path, playing=True)
        self.assertEqual(self.gs.set, [])
        with mock.patch.object(persistence, "write_json", side_effect=OSError("controlled migration failure")):
            wait_for(lambda: self.now.identities.identity("snes/Game") == "esde:snes/Game.sfc")
            self.assertEqual(self.gs.set, [30])
            self.assertIn("game:snes/Game", rules._rules)
        self.now.identities.changed.emit()
        self.assertIn("game:esde:snes/Game.sfc", rules._rules)
        self.assertNotIn("game:snes/Game", rules._rules)
        self.assertEqual(self.gs.set, [30])

    def test_pending_legacy_rule_does_not_apply_system_fallback_to_ambiguous_sibling(self):
        path = self.rom("a/Game.sfc")
        self.rom("b/Game.sfc")
        rules = self.rules({"game:snes/Game": dict(fps=30), "system:snes": dict(fps=60)})
        self.now.select(path, playing=True)
        self.assertEqual(self.gs.set, [])
        wait_for(lambda: self.now.identities.identity("snes/Game") == "")
        self.assertEqual(self.gs.set, [60])
        self.assertIn("game:snes/Game", rules._rules)

    def test_rule_migration_waits_for_restore_resume_and_keeps_device_originals(self):
        self.rom("Game.sfc")
        rules = self.rules({"game:snes/Game": dict(fps=30)})
        rules._base = {"dolphin": "exact-original"}
        rules.pauseWrites(restoring=True)
        wait_for(lambda: self.now.identities.identity("snes/Game") == "esde:snes/Game.sfc")
        self.assertIn("game:snes/Game", rules._rules)
        rules.resumeWrites()
        self.assertIn("game:esde:snes/Game.sfc", rules._rules)
        self.assertEqual(json.loads((self.root / "rules.json").read_text())["base"], {"dolphin": "exact-original"})

    def test_configured_nested_gameinfo_and_duplicate_normalized_titles(self):
        path = self.rom("a/Same.v1.sfc")
        self.rom("b/Same.v1.sfc")
        touch(f"{self.gamelists}/snes/gamelist.xml", '<gameList>'
              '<game><path>./a/Same.v1.sfc</path><name>Game’s name</name><desc>First</desc></game>'
              '<game><path>./b/Same.v1.sfc</path><name>Other</name><desc>Second</desc></game></gameList>')
        touch(str(self.root / "media" / "snes" / "covers" / "a" / "Same.v1.png"))
        self.assertEqual(gameinfo.read("snes", path)["desc"], "First")
        self.assertTrue(gameinfo.read("snes", path)["cover"].endswith("/a/Same.v1.png"))
        self.assertEqual(gameinfo.find("Game's name"), ("snes", "a/Same.v1.sfc"))
        content = Path(f"{self.gamelists}/snes/gamelist.xml").read_text().replace("Other", "Game's name")
        touch(f"{self.gamelists}/snes/gamelist.xml", content)
        self.assertEqual(gameinfo.find("Game's name"), ("", ""))


if __name__ == "__main__":
    unittest.main()
