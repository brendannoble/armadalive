"""Exercise backup coordination with both real asynchronous persistence workers."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests.common import app, wait_for
from PySide6.QtCore import QEvent, QObject, QTimer, Signal

from armadalive import apps, backup, history, persistence


class IdleNow(QObject):
    changed = Signal()
    state = dict(kind="idle", key="", playing=False)


class BackupIntegration(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.layout_path = self.directory / "apps.json"
        self.history_path = self.directory / "history.json"
        self.archive = self.directory / "backup.json"
        self.layout_path.write_text(json.dumps(dict(
            slots=["folder:1"], folders={"1": dict(name="Initial", items=[])}, next=2)))
        self.history_path.write_text(json.dumps(dict(imported=True, games={}, sessions=[])))
        # Keep real catalog readers isolated from the user's installed programs.
        empty = str(self.directory / "empty")
        for module, name, value in (
            (apps, "CONFIG", str(self.layout_path)),
            (apps, "APP_DIRS", [empty]),
            (apps, "ICON_ROOTS", [empty]),
            (apps.steamlib, "STEAM", empty),
            (apps.esdelib, "ES", empty),
            (apps.esdelib, "APPIMAGE", empty),
            (apps.esdelib, "CACHE", empty),
            (backup, "CONFIG_DIR", str(self.directory)),
            (backup, "FILE", str(self.archive)),
        ):
            self.enterContext(mock.patch.object(module, name, value))
        self.now = IdleNow()
        self.history = history.History(self.now, str(self.history_path))
        self.apps = apps.Apps(self.history)
        self.coordinator = backup.Backup(writers=(self.apps, self.history))
        self.addCleanup(self.close_backends)
        wait_for(lambda: not self.apps.catalogLoading)

    def close_backends(self):
        self.coordinator._drain.stop()
        self.apps.flush()
        self.history._shutdown()
        for obj in (self.coordinator, self.apps, self.history, self.now):
            obj.deleteLater()
            app.sendPostedEvents(obj, QEvent.Type.DeferredDelete)

    def block_first_replacements(self):
        """Hold each real writer immediately before its first atomic commit."""
        entered = {path: threading.Event() for path in (self.layout_path, self.history_path)}
        release = {path: threading.Event() for path in entered}
        owner = threading.get_ident()
        original = os.replace

        def replace(source, destination):
            path = Path(destination)
            if path in entered and not entered[path].is_set():
                self.assertNotEqual(threading.get_ident(), owner)
                entered[path].set()
                if not release[path].wait(5):
                    raise OSError("test did not release the persistence worker")
            return original(source, destination)

        self.enterContext(mock.patch.object(persistence.os, "replace", replace))
        # Release before backend cleanup even if an assertion fails.
        self.addCleanup(lambda: [event.set() for event in release.values()])
        return entered, release

    def edit_both(self, name, key):
        self.apps.renameFolder("folder:1", name)
        self.history.toggleFavorite(key, name, "steam", "")

    def read_layout(self):
        return json.loads(self.layout_path.read_text())

    def read_history(self):
        return json.loads(self.history_path.read_text())

    def test_export_waits_for_both_writers_and_resumes_edits_after_snapshot(self):
        entered, release = self.block_first_replacements()
        self.edit_both("First accepted", "steam/41")
        for event in entered.values():
            self.assertTrue(event.wait(1))
        self.edit_both("Latest accepted", "steam/42")
        self.coordinator.save()
        self.edit_both("During export", "steam/43")

        fired = []
        QTimer.singleShot(0, lambda: fired.append(True))
        app.processEvents()
        self.assertTrue(fired)
        self.assertTrue(self.coordinator.state["busy"])
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.apps.slots[0]["name"], "During export")
        self.assertTrue(self.history.info("steam/43")["favorite"])

        release[self.layout_path].set()
        wait_for(lambda: not self.apps.writesPending())
        self.assertEqual(self.read_layout()["folders"]["1"]["name"], "Latest accepted")
        self.assertTrue(self.coordinator.state["busy"])
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.read_history()["games"], {})

        release[self.history_path].set()
        wait_for(lambda: self.coordinator.state["message"] == "saved")
        exported = json.loads(self.archive.read_text())["files"]
        self.assertEqual(exported["apps.json"]["folders"]["1"]["name"], "Latest accepted")
        self.assertEqual(set(exported["history.json"]["games"]), {"steam/41", "steam/42"})
        self.apps.flush()
        self.history.flush()
        self.assertFalse(self.coordinator.state["busy"])
        self.assertEqual(self.read_layout()["folders"]["1"]["name"], "During export")
        self.assertEqual(set(self.read_history()["games"]), {"steam/41", "steam/42", "steam/43"})

    def test_restore_holds_both_writers_and_failed_restart_resumes_both(self):
        restored_layout = dict(slots=["folder:9"], folders={"9": dict(name="Restored", items=[])}, next=10)
        restored_history = dict(imported=True, games={"steam/99": dict(total=900, plays=2)}, sessions=[])
        self.archive.write_text(json.dumps(dict(files={
            "apps.json": restored_layout, "history.json": restored_history})))
        entered, release = self.block_first_replacements()
        self.edit_both("Old active write", "steam/41")
        for event in entered.values():
            self.assertTrue(event.wait(1))
        self.edit_both("Old queued write", "steam/42")

        with mock.patch.object(backup.QTimer, "singleShot") as restart:
            self.coordinator.restore()
            self.edit_both("During restore", "steam/43")
            release[self.history_path].set()
            wait_for(lambda: not self.history.writesPending())
            self.assertEqual(set(self.read_history()["games"]), {"steam/41", "steam/42"})
            self.assertEqual(self.read_layout()["folders"]["1"]["name"], "Initial")
            self.assertTrue(self.coordinator.state["busy"])
            restart.assert_not_called()

            release[self.layout_path].set()
            wait_for(lambda: self.coordinator.state["message"] == "restored")
            restart.assert_called_once()
            self.assertEqual(restart.call_args.args[0], 800)

        # Flush and keep editing during the restart window: neither old worker
        # may overwrite the restored primary files.
        self.edit_both("During restart", "steam/44")
        self.apps.flush()
        self.history.flush()
        self.assertTrue(self.coordinator.state["busy"])
        self.assertEqual(self.read_layout(), restored_layout)
        self.assertEqual(self.read_history(), restored_history)

        with mock.patch.object(backup.os, "execv", side_effect=OSError("restart unavailable")) as execute:
            restart.call_args.args[1]()
            execute.assert_called_once_with(backup.sys.executable, backup.sys.orig_argv)
        self.apps.flush()
        self.history.flush()
        self.assertFalse(self.coordinator.state["busy"])
        self.assertEqual(self.coordinator.state["message"], "restart unavailable")
        self.assertEqual(self.read_layout()["folders"]["1"]["name"], "During restart")
        self.assertEqual(set(self.read_history()["games"]), {"steam/41", "steam/42", "steam/43", "steam/44"})


if __name__ == "__main__":
    unittest.main()
