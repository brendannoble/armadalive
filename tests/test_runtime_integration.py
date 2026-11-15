"""Real-process integration checks for the asynchronous runtime boundaries.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_runtime_integration -v
Only external commands and filesystem locations are replaced. Production parsing,
timeouts, Qt worker scheduling, queues, readback and publication run unchanged.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer

from tests.common import app
from armadalive import admin, controls, media, privileged, system
from armadalive.poller import Poller


class RuntimeProcessIntegration(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="armadalive-runtime-")))
        bindir = self.root / "bin"
        bindir.mkdir()
        source = Path(__file__).with_name("runtime_command_shim.py").read_text()
        for command in ("wpctl", "nmcli", "rfkill", "iw", "tailscale", "busctl", "sudo"):
            executable = bindir / command
            executable.write_text("#!" + sys.executable + "\n" + source)
            executable.chmod(0o700)
        self.config = {}
        self.configure()
        (self.root / "volume").write_text("0.10")
        # No fallback PATH: an unanticipated command cannot reach a real service.
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "PATH": str(bindir), "ARMADALIVE_RUNTIME_FIXTURE": str(self.root)}))
        self.objects = []
        self.pollers = []
        self.ticks = []
        self.started = time.monotonic()
        self.heartbeat = QTimer()
        self.heartbeat.timeout.connect(lambda: self.ticks.append((time.monotonic(), threading.get_ident())))
        self.heartbeat.start(10)

    def tearDown(self):
        # Release blocked fixtures even after an assertion, drain workers while
        # their PATH remains installed, then delete their owner-thread objects.
        for name in ("old-read", "first-write"):
            (self.root / (name + ".release")).touch()
        for poller in self.pollers:
            poller._timer.stop()
        try:
            self.wait(lambda: all(not poller._busy for poller in self.pollers)
                      and all(not getattr(obj, "_commands", None)
                              or not obj._commands._running for obj in self.objects), timeout=7)
        finally:
            self.heartbeat.stop()
            for obj in self.objects:
                obj.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.stack.close()

    def configure(self, **updates):
        self.config.update(updates)
        temporary = self.root / "config.next"
        temporary.write_text(json.dumps(self.config))
        temporary.replace(self.root / "config.json")

    def own(self, obj):
        self.objects.append(obj)
        for poller in obj.findChildren(Poller):
            # Tests drive polling explicitly; reads themselves remain real workers.
            poller._timer.stop()
            self.pollers.append(poller)
        return obj

    def events(self, command=None, event=None):
        path = self.root / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if (command is None or row["command"] == command)
                and (event is None or row["event"] == event)]

    def wait(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if condition():
                return
            time.sleep(0.002)
        self.fail("Asynchronous condition did not complete within %.1f seconds" % timeout)

    def assert_responsive(self, minimum_ticks=10):
        self.assertGreaterEqual(len(self.ticks), minimum_ticks)
        self.assertEqual({thread for _, thread in self.ticks}, {threading.get_ident()})
        times = [self.started, *(tick for tick, _ in self.ticks), time.monotonic()]
        # This loose ceiling tolerates shared-runner noise but catches the real
        # one-, two- and four-second blocking service calls used below.
        self.assertLess(max(b - a for a, b in zip(times, times[1:])), 0.5)

    def assert_serial(self, rows):
        active = set()
        for row in rows:
            if row["event"] == "start":
                self.assertFalse(active, "overlapping subprocesses: " + repr(rows))
                active.add(row["pid"])
            elif row["event"] == "exit":
                active.remove(row["pid"])
        self.assertFalse(active)

    def assert_reaped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_controls_real_commands_coalesce_discard_stale_read_and_recover(self):
        obj = self.own(controls.Controls())
        self.wait(lambda: obj.state["volume"] == 0.1 and not obj._poller._busy
                  and not obj._vpn_poller._busy)
        published = []
        obj.changed.connect(lambda: published.append((obj.state["volume"], threading.get_ident())))

        # Hold a real wpctl read after it captured 0.10, then finish two writes.
        self.configure(volume_read_gate="old-read", volume_write_gate="first-write")
        obj._poller.refresh()
        self.wait(lambda: any(row.get("gate") == "old-read" for row in self.events("wpctl", "waiting")))
        obj.setVolume(0.2)
        self.wait(lambda: any(row.get("gate") == "first-write" for row in self.events("wpctl", "waiting")))
        for value in (0.3, 0.4, 0.6, 0.9):
            obj.setVolume(value)
        self.configure(volume_write_gate="")
        (self.root / "first-write.release").touch()
        self.wait(lambda: not obj._commands._running)
        self.assertTrue(obj._poller._busy)
        self.assertEqual((self.root / "volume").read_text(), "0.90")
        self.configure(volume_read_gate="")
        (self.root / "old-read.release").touch()
        self.wait(lambda: obj.state["volume"] == 0.9 and not obj._poller._busy)
        self.assertEqual(published, [(0.9, threading.get_ident())])

        writes = [row for row in self.events("wpctl") if row["args"][0] == "set-volume"]
        self.assertEqual([row["args"][-1] for row in writes if row["event"] == "start"], ["0.20", "0.90"])
        self.assert_serial(writes)
        reads = [row for row in self.events("wpctl") if row["args"][0] == "get-volume"]
        self.assert_serial(reads)

        completed = []
        obj._commands.completed.connect(lambda *args: completed.append(args))
        self.configure(volume_write_mode="fail")
        obj.setVolume(0.7)
        self.wait(lambda: bool(completed) and not obj._poller._busy)
        self.assertFalse(completed[-1][2])
        self.assertEqual(obj.state["volume"], 0.9)
        self.configure(volume_write_mode="healthy")
        obj.setVolume(0.65)
        self.wait(lambda: obj.state["volume"] == 0.65 and not obj._poller._busy)
        self.assertTrue(completed[-1][2])
        self.assert_responsive()

    def test_power_real_timeout_nonoverlap_malformed_failure_and_recovery(self):
        obj = self.own(system.SystemMonitor())
        self.wait(lambda: obj.stats["temp"] == 51 and all(not p._busy for p in self.pollers))
        self.assertEqual(len(self.events("busctl", "start")), 1)
        self.configure(power_mode="hang")
        obj._power_poller.refresh()
        self.wait(lambda: len(self.events("busctl", "start")) == 2)
        hung = self.events("busctl", "start")[-1]
        for _ in range(50):
            obj._power_poller.kick()
        self.wait(lambda: not obj._power_poller._busy, timeout=2)
        self.assertEqual(len(self.events("busctl", "start")), 2)
        self.assertEqual(obj.stats["temp"], 51)
        self.assert_reaped(hung["pid"])
        self.assertLess(time.monotonic() - hung["time"], 1.8)
        for mode in ("malformed", "fail"):
            self.configure(power_mode=mode)
            obj._power_poller.refresh()
            self.wait(lambda: not obj._power_poller._busy)
            self.assertEqual(obj.stats["temp"], 51)
        self.configure(power_mode="healthy", temperature=67)
        obj._power_poller.refresh()
        self.wait(lambda: obj.stats["temp"] == 67 and not obj._power_poller._busy)
        self.assertEqual(obj.stats["profiles"], ["Balanced", "Performance"])
        self.assertEqual(len(self.events("busctl", "start")), 5)
        self.assert_responsive(minimum_ticks=50)

    def test_media_real_candidate_failure_shared_deadline_and_recovery(self):
        self.configure(title="First track")
        obj = self.own(media.Media())
        self.wait(lambda: obj.state.get("title") == "First track" and not obj._poller._busy)
        self.assertEqual(obj.state["name"], media.PREFIX + "healthy")
        initial = self.events("busctl", "start")
        self.assertEqual(len(initial), 4)  # list, broken status, healthy status, healthy metadata
        for mode in ("malformed", "fail"):
            self.configure(media_mode=mode)
            obj._poller.refresh()
            self.wait(lambda: not obj._poller._busy)
            self.assertEqual(obj.state["title"], "First track")
        self.configure(media_mode="hang")
        before = len(self.events("busctl", "start"))
        started = time.monotonic()
        obj._poller.refresh()
        self.wait(lambda: len(self.events("busctl", "start")) >= before + 2)
        for _ in range(30):
            obj._poller.kick()
        self.wait(lambda: not obj._poller._busy, timeout=5)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 3.5)
        self.assertLess(elapsed, 5)
        attempted = self.events("busctl", "start")[before:]
        self.assertEqual(len(attempted), 3)  # shared deadline permits two status calls, not three
        self.assertEqual([row["args"][3] for row in attempted[1:]],
                         [media.PREFIX + "broken", media.PREFIX + "healthy"])
        for row in attempted[1:]:
            self.assert_reaped(row["pid"])
        self.assertEqual(obj.state["title"], "First track")
        self.configure(media_mode="healthy", title="Recovered track")
        obj._poller.refresh()
        self.wait(lambda: obj.state.get("title") == "Recovered track" and not obj._poller._busy)
        self.assert_responsive(minimum_ticks=250)

    def test_shared_helper_real_timeout_coalesces_and_admin_recovers(self):
        helper = self.root / "installed-helper"
        helper.write_text("fixture helper\n")
        scripts = self.root / "share" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "armadalive-helper").write_text(helper.read_text())
        self.stack.enter_context(mock.patch.object(privileged, "HELPER", str(helper)))
        self.stack.enter_context(mock.patch.object(admin, "SHARE", str(scripts.parent)))
        self.stack.enter_context(mock.patch.object(admin, "AYN_MAP", str(self.root / "absent-map")))
        self.stack.enter_context(mock.patch.object(privileged, "_seen", (float("-inf"), False)))
        self.stack.enter_context(mock.patch.object(privileged, "_probe_generation", 0))
        self.configure(helper_mode="hang")
        obj = self.own(admin.Admin())
        # A second consumer enters the same real probe lock while sudo hangs.
        owner = QObject()
        results = []
        second = Poller(owner, lambda: privileged.available(refresh=True), results.append, 60_000, now=False)
        self.own(owner)
        self.wait(lambda: bool(self.events("sudo", "start")))
        second.kick()
        self.wait(lambda: bool(results) and not obj._poller._busy, timeout=3)
        self.assertEqual(results, [False])
        self.assertEqual(obj.state["helper"], "missing")
        calls = self.events("sudo", "start")
        self.assertEqual(len(calls), 1)
        self.assert_reaped(calls[0]["pid"])
        self.configure(helper_mode="healthy")
        privileged.invalidate()
        obj.refresh()
        self.wait(lambda: obj.state["helper"] == "ok" and not obj._poller._busy)
        self.assertEqual(len(self.events("sudo", "start")), 2)
        self.assert_responsive(minimum_ticks=100)


if __name__ == "__main__":
    unittest.main()
