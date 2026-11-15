import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt

from tests.common import app
from armadalive import shell, updater


run = subprocess.run


class Version(unittest.TestCase):
    def test_comparison(self):
        self.assertGreater(updater._version("v0.10.0"), updater._version("0.9.9"))
        self.assertEqual(updater._version("v1.2.3"), updater._version("1.2.3"))

    def test_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content"
            path.write_bytes(b"abc")
            self.assertEqual(updater.sha256(path), hashlib.sha256(b"abc").hexdigest())


class Update(unittest.TestCase):
    tarball_url = "https://release.example/armadalive.tar.gz"
    checksum_url = "https://release.example/armadalive.tar.gz.sha256"

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.cache = Path(directory) / "${literal-cache}" / "update"
        self.enterContext(patch.object(updater, "CACHE", str(self.cache)))
        self.enterContext(patch.object(updater, "REPO_ID", "o/r"))
        self.enterContext(patch.object(updater, "_get", self.get))
        launcher = SimpleNamespace(
            run=self.launch,
            CalledProcessError=subprocess.CalledProcessError,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        self.enterContext(patch.object(updater, "subprocess", launcher))
        self.release = {"tag_name": "v99.0.0", "assets": [
            {"name": updater.ASSET, "browser_download_url": self.tarball_url},
            {"name": updater.CHECKSUM, "browser_download_url": self.checksum_url},
        ]}
        self.archive = self.package()
        self.checksum = hashlib.sha256(self.archive).hexdigest().upper().encode() + b"  armadalive.tar.gz\n"
        self.downloads = 0
        self.launches = []
        self.download_error = None
        self.launch_error = None
        self.downloading = threading.Event()
        self.release_download = threading.Event()
        self.release_download.set()

    def package(self, *, exit_code=0, filename="ArmadaLive/install.sh"):
        content = f"#!/bin/sh\nexit {exit_code}\n".encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            installer = tarfile.TarInfo(filename)
            installer.mode = 0o755
            installer.size = len(content)
            archive.addfile(installer, io.BytesIO(content))
        return stream.getvalue()

    def get(self, url, timeout=15):
        if url == "https://api.github.com/repos/o/r/releases/latest":
            return io.BytesIO(json.dumps(self.release).encode())
        if url == self.tarball_url:
            self.downloads += 1
            self.downloading.set()
            if not self.release_download.wait(3):
                raise OSError("test download gate timed out")
            if self.download_error:
                raise self.download_error
            return io.BytesIO(self.archive)
        if url == self.checksum_url:
            return io.BytesIO(self.checksum)
        raise ValueError(f"unexpected URL: {url}")

    def launch(self, command, **kwargs):
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--unit=armadalive-update", command)
        self.launches.append([str(arg).replace("$$", "$") for arg in command[command.index("sh"):]])
        if self.launch_error:
            raise self.launch_error
        return subprocess.CompletedProcess(command, 0)

    def wait_for(self, backend, *statuses):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            app.processEvents()
            if backend.state["status"] in statuses:
                return backend.state
            time.sleep(0.005)
        self.fail(f"Expected {statuses}, got {backend.state}")

    def offer(self, backend):
        backend.check()
        self.assertEqual(self.wait_for(backend, "available", "error")["status"], "available")

    def restart(self, backend):
        backend.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return updater.Updater()

    def test_a_newer_release_with_a_checksum_is_offered(self):
        backend = updater.Updater()
        self.offer(backend)
        self.assertEqual(backend.state["latest"], "v99.0.0")

    def test_a_newer_release_without_a_checksum_is_refused(self):
        self.release["assets"].pop()
        backend = updater.Updater()
        backend.check()
        state = self.wait_for(backend, "error")
        self.assertIn("checksum", state["message"])
        backend.install()
        self.assertEqual(self.downloads, 0)

    def test_the_same_version_is_up_to_date(self):
        self.release = {"tag_name": updater.__version__, "assets": []}
        backend = updater.Updater()
        backend.check()
        self.wait_for(backend, "uptodate")

    def test_malformed_release_ends_check_and_allows_retry(self):
        backend = updater.Updater()
        release = self.release
        self.release = {"tag_name": "v99.0.0", "assets": [{"name": updater.ASSET}]}
        backend.check()
        self.wait_for(backend, "error")
        self.release = release
        self.offer(backend)

    def test_launcher_success_is_pending_until_installer_exits_after_restart(self):
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        backend = self.restart(backend)
        self.assertEqual(backend.state["status"], "started")
        backend.check()
        backend.install()
        self.assertEqual(backend.state["status"], "started")
        self.assertEqual(self.downloads, 1)
        result = run(self.launches[0], check=True, timeout=5, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.wait_for(backend, "installed")
        backend = self.restart(backend)
        self.assertEqual((backend.state["status"], backend.state["latest"]), ("installed", "v99.0.0"))

    def test_failed_installer_exit_survives_restart(self):
        self.archive = self.package(exit_code=7)
        self.checksum = hashlib.sha256(self.archive).hexdigest().encode()
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        self.assertEqual(run(self.launches[0], timeout=5, capture_output=True).returncode, 7)
        result = run([sys.executable, "-c", """
import json
import sys
from PySide6.QtCore import QCoreApplication
from armadalive import updater
app = QCoreApplication([])
updater.CACHE = sys.argv[1]
print(json.dumps(updater.Updater().state))
""", str(self.cache)], capture_output=True, check=True, timeout=5)
        state = json.loads(result.stdout)
        self.assertEqual(state["status"], "error")
        self.assertIn("exit status 7", state["message"])
        backend = self.restart(backend)
        self.offer(backend)

    def test_other_attempt_results_cannot_complete_the_pending_attempt(self):
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        run(self.launches[0], check=True, timeout=5, capture_output=True)
        self.wait_for(backend, "installed")
        previous_result = next(self.cache.glob("*.result"))
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        previous_result.write_text("0\n", encoding="ascii")
        backend = self.restart(backend)
        self.assertEqual(backend.state["status"], "started")
        run(self.launches[1], check=True, timeout=5, capture_output=True)
        self.wait_for(backend, "installed")

    def test_invalid_installer_result_is_terminal_and_recoverable(self):
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        record = json.loads((self.cache / "installation.json").read_text())
        (self.cache / (record["attempt"] + ".result")).write_text("0\ntruncated payload", encoding="ascii")
        backend = self.restart(backend)
        self.assertEqual(backend.state["status"], "error")
        self.assertIn("installer result", backend.state["message"])
        self.offer(backend)

    def test_missing_result_expires_after_restart_and_can_retry(self):
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        record = json.loads((self.cache / "installation.json").read_text())
        clock = SimpleNamespace(time=lambda: record["deadline"] - 1)
        with patch.object(updater, "time", clock, create=True):
            backend = self.restart(backend)
            self.assertEqual(backend.state["status"], "started")
            clock.time = lambda: record["deadline"]
            self.wait_for(backend, "error")
            self.assertIn("did not report", backend.state["message"])
        self.offer(backend)
        self.enterContext(patch.object(updater, "time", clock))
        backend.install()
        self.wait_for(backend, "started")
        self.assertEqual(len(self.launches), 2)
        self.assertNotEqual(run(self.launches[0], timeout=5, capture_output=True).returncode, 0)
        self.assertEqual(self.restart(backend).state["status"], "started")

    def test_missing_invalid_and_mismatched_checksums_are_terminal(self):
        backend = updater.Updater()
        for checksum in (b"", b"not a checksum", b"0" * 64):
            with self.subTest(checksum=checksum):
                self.checksum = checksum
                self.offer(backend)
                backend.install()
                state = self.wait_for(backend, "error", "installed")
                self.assertEqual(state["status"], "error")
                self.assertIn("checksum", state["message"])
                backend = self.restart(backend)
                self.assertEqual(backend.state["status"], "error")
        self.assertEqual(self.launches, [])

    def test_archive_filter_rejects_paths_outside_the_release(self):
        self.archive = self.package(filename="../escape")
        self.checksum = hashlib.sha256(self.archive).hexdigest().encode()
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "error")
        self.assertEqual(self.launches, [])

    def test_truncated_archive_ends_attempt_and_allows_retry(self):
        self.archive = self.package()[:30]
        self.checksum = hashlib.sha256(self.archive).hexdigest().encode()
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "error")
        self.assertEqual(self.launches, [])
        backend = self.restart(backend)
        self.assertEqual(backend.state["status"], "error")
        self.offer(backend)

    def test_launcher_timeout_still_accepts_an_eventual_result(self):
        self.launch_error = subprocess.TimeoutExpired("systemd-run", 60)
        backend = updater.Updater()
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")
        backend = self.restart(backend)
        run(self.launches[0], check=True, timeout=5, capture_output=True)
        self.wait_for(backend, "installed")

    def test_download_and_launcher_failures_survive_restart_and_allow_retry(self):
        backend = updater.Updater()
        for failure in ("network unavailable", "launcher unavailable"):
            with self.subTest(failure=failure):
                self.download_error = OSError(failure) if failure.startswith("network") else None
                self.launch_error = OSError(failure) if failure.startswith("launcher") else None
                self.offer(backend)
                backend.install()
                self.wait_for(backend, "error")
                backend = self.restart(backend)
                self.assertEqual(backend.state["status"], "error")
                self.assertIn(failure, backend.state["message"])
        self.download_error = self.launch_error = None
        self.offer(backend)
        backend.install()
        self.wait_for(backend, "started")

    def test_competing_updaters_cannot_download_or_install_in_parallel(self):
        primary, second, third = updater.Updater(), updater.Updater(), updater.Updater()
        for backend in (primary, second, third):
            self.offer(backend)
        self.release_download.clear()
        self.addCleanup(self.release_download.set)
        primary.install()
        self.assertTrue(self.downloading.wait(2))
        second.install()
        deadline = time.monotonic() + 2
        while not second._completion.isActive() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        self.assertTrue(second._completion.isActive())
        self.assertEqual(self.downloads, 1)
        self.release_download.set()
        self.wait_for(primary, "started")
        self.wait_for(second, "started")
        # The service has been launched but has not opened the attempt lock.
        third.install()
        self.wait_for(third, "started")
        self.assertEqual(self.downloads, 1)
        self.assertEqual(len(self.launches), 1)

    def test_shell_subprocess_can_run_during_an_update(self):
        backend = updater.Updater()
        self.offer(backend)
        self.release_download.clear()
        backend.install()
        try:
            self.assertTrue(self.downloading.wait(2))
            result = shell.subprocess.run(
                [sys.executable, "-c", "print('shell probe')"],
                check=True, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.stdout, "shell probe\n")
        finally:
            self.release_download.set()
            self.wait_for(backend, "started")

    def test_worker_state_changes_run_on_the_owner_thread(self):
        backend = updater.Updater()
        owner = threading.get_ident()
        observed = []
        backend.changed.connect(
            lambda: observed.append((backend.state["status"], threading.get_ident())),
            Qt.ConnectionType.DirectConnection,
        )
        request = threading.Thread(target=backend.check)
        request.start()
        request.join(2)
        self.wait_for(backend, "available")
        self.launch_error = OSError("launcher unavailable")
        request = threading.Thread(target=backend.install)
        request.start()
        request.join(2)
        self.wait_for(backend, "error")
        self.assertEqual(observed, [(status, owner) for status in ("checking", "available", "installing", "error")])


if __name__ == "__main__":
    unittest.main()
