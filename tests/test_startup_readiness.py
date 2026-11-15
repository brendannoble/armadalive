import contextlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from armadalive import app as gui


BACKENDS = (
    "Activity", "Admin", "Apps", "AynButton", "Backup", "Battery", "Cheats", "Closer",
    "Controls", "ControlsLayout", "EmulatorControl", "GameInfo", "GameRules", "Gamescope",
    "Health", "History", "LudoSync", "Manuals", "Media", "NowPlaying", "Onboarding", "Pad",
    "Palette", "Settings", "Shell", "SteamShortcuts", "SteamUi", "SystemMonitor", "Tasks",
    "Thermal", "Tiles", "Trackpad", "UiSettings", "Updater", "VectorArtwork",
)
LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/run.sh"


class AppReadiness(unittest.TestCase):
    def setUp(self):
        constructors = {name: mock.DEFAULT for name in BACKENDS}
        constructors.update(QGuiApplication=mock.DEFAULT, QQmlApplicationEngine=mock.DEFAULT,
                            QTimer=mock.DEFAULT)
        self.enterContext(mock.patch.multiple(gui, **constructors))
        self.enterContext(mock.patch.object(gui.signal, "signal"))
        self.socket = self.enterContext(mock.patch.object(socket, "socket"))
        self.datagram = self.socket.return_value.__enter__.return_value
        self.window = mock.Mock(spec=gui.QQuickWindow)
        self.window.presentationActiveChanged = mock.Mock()
        self.engine = gui.QQmlApplicationEngine.return_value
        self.engine.rootObjects.return_value = [self.window]
        self.application = gui.QGuiApplication.return_value
        self.events = []
        self.callbacks = []
        self.engine.load.side_effect = lambda *_: self.events.append("load window")
        self.engine.rootObjects.side_effect = lambda: (
            self.events.append("root objects") or self.engine.rootObjects.return_value
        )
        gui.QTimer.singleShot.side_effect = lambda _delay, callback: self.callbacks.append(callback)
        self.application.exec.side_effect = self.enter_event_loop
        self.datagram.sendto.side_effect = lambda *_: self.events.append("send")

    def enter_event_loop(self):
        self.events.append("enter event loop")
        for callback in self.callbacks:
            callback()
        if self.application.exit.called:
            return self.application.exit.call_args.args[0]
        return 0

    def start_app(self, address, *arguments):
        with mock.patch.dict(os.environ), mock.patch.object(sys, "argv", ["armadalive", *arguments]):
            if address is None:
                os.environ.pop("NOTIFY_SOCKET", None)
            else:
                os.environ["NOTIFY_SOCKET"] = address
            return gui.main()

    def test_filesystem_notification_is_sent_once_after_entering_the_event_loop(self):
        self.assertEqual(self.start_app("/run/user/1000/notify"), 0)
        self.assertEqual(self.events, ["load window", "root objects", "enter event loop", "send"])
        self.socket.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.datagram.sendto.assert_called_once_with(b"READY=1", "/run/user/1000/notify")

    def test_abstract_notification_address_uses_its_null_prefix(self):
        self.assertEqual(self.start_app("@armadalive-notify"), 0)
        self.datagram.sendto.assert_called_once_with(b"READY=1", "\0armadalive-notify")

    def test_standalone_start_runs_the_event_loop_without_opening_a_socket(self):
        self.assertEqual(self.start_app(None, "--windowed"), 0)
        self.assertIn("enter event loop", self.events)
        self.socket.assert_not_called()

    def test_snapshot_captures_the_window_without_notifying(self):
        with mock.patch.object(gui.QQuickWindow, "grabWindow") as grab, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.start_app("@notify", "--snapshot", "/tmp/mock.png", "--delay", "0"), 0)
        grab.assert_called_once_with(self.window)
        grab.return_value.save.assert_called_once_with("/tmp/mock.png")
        self.application.quit.assert_called_once()
        self.socket.assert_not_called()

    def test_missing_root_exits_before_notifying(self):
        self.engine.rootObjects.return_value = []
        self.assertEqual(self.start_app("@notify"), 1)
        self.assertNotIn("enter event loop", self.events)
        self.socket.assert_not_called()

    def test_non_window_root_is_not_a_successful_start(self):
        self.engine.rootObjects.return_value = [mock.Mock(spec=gui.QObject)]
        self.assertEqual(self.start_app("@notify"), 1)
        self.assertNotIn("enter event loop", self.events)
        self.socket.assert_not_called()

    def test_notification_transport_failure_exits_unsuccessfully(self):
        self.datagram.sendto.side_effect = OSError("notification socket unavailable")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.start_app("@notify"), 1)
        self.assertIn("notification socket unavailable", stderr.getvalue())


class LauncherReadinessContext(unittest.TestCase):
    def test_notification_address_reaches_exec_and_bottom_display_child(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as runtime:
                fake_python = Path(runtime, "python")
                fake_python.write_text(
                    '#!/bin/sh\n'
                    '[ "$1" = -c ] && exit 0\n'
                    'printf "%s\\n" "$$" "$PPID" "$NOTIFY_SOCKET" "$QT_QPA_PLATFORM" "$DISPLAY" "$*"\n'
                )
                fake_python.chmod(0o755)
                if fallback:
                    Path(runtime, "armada-bottom-env").write_text("DISPLAY=:2\n")
                env = dict(
                    os.environ, XDG_RUNTIME_DIR=runtime, NOTIFY_SOCKET="@notify",
                    DISPLAY=":1", ARMADALIVE_PYTHON=str(fake_python), ARMADALIVE_X11_AFTER="0",
                    ARMADALIVE_WAIT="0", ARMADALIVE_WATCH="0.01", ARMADALIVE_DRY_RUN="",
                )
                with subprocess.Popen(["sh", LAUNCHER], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as launcher:
                    output, errors = launcher.communicate(timeout=5)
                self.assertEqual(launcher.returncode, 0, errors)
                pid, parent, address, platform, display, arguments = output.splitlines()
                self.assertEqual(int(parent if fallback else pid), launcher.pid)
                self.assertEqual(address, "@notify")
                self.assertEqual(platform, "xcb")
                self.assertEqual(display, ":2" if fallback else ":1")
                self.assertEqual(arguments, "-m armadalive" if fallback else "-m armadalive --windowed")
