"""Check for a release and report its installer result across app restarts."""
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import uuid
import zlib

from PySide6.QtCore import Property, QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from . import REPO, __version__, persistence

REPO_ID = os.environ.get("ARMADALIVE_REPO", REPO)
SHARE = os.path.expanduser("~/.local/share/armadalive")
CACHE = os.path.expanduser("~/.cache/armadalive/update")
ASSET, CHECKSUM = "armadalive.tar.gz", "armadalive.tar.gz.sha256"
INSTALL_TIMEOUT = 30 * 60
LAUNCH_TIMEOUT = 60

# The transient unit survives the app's restart. The launcher holds the same
# lock until systemd-run returns. A pending record covers the handoff to flock.
RUN_INSTALLER = r'''work=$1
root=$2
lock=$3
finish() {
    status=$?
    if [ -d "$work" ]; then
        printf '%s\n' "$status" > "$work.result.part" &&
            mv -f -- "$work.result.part" "$work.result"
        rm -rf -- "$work"
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
exec 9>>"$lock"
flock 9 || exit 1
(cd "$root" && ./install.sh)
'''


def _version(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag)[:3])


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _get(url, timeout=15):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": f"ArmadaLive/{__version__}"}), timeout=timeout)


def _valid_record(record):
    if not isinstance(record, dict):
        return False
    attempt = record.get("attempt")
    if not (isinstance(attempt, str) and re.fullmatch(r"install-[0-9a-f]{32}", attempt)
            and isinstance(record.get("latest"), str)):
        return False
    phase = record.get("phase")
    if phase == "downloading":
        return True
    if phase == "failed":
        return isinstance(record.get("message"), str) and bool(record["message"])
    deadline = record.get("deadline")
    return phase == "started" and type(deadline) in (int, float) and 0 < deadline < float("inf")


class Updater(QObject):
    changed = Signal()
    _check_requested = Signal()
    _install_requested = Signal()
    _worker_changed = Signal(dict)
    _installation_changed = Signal()

    def __init__(self):
        super().__init__()
        self._s = dict(version=__version__, repo=REPO_ID, repoUrl=f"https://github.com/{REPO_ID}" if REPO_ID else "",
                       status="norepo" if not REPO_ID else "idle", latest="", message="", tarball="", sha="")
        self._check_requested.connect(self._start_check)
        self._install_requested.connect(self._start_install)
        self._worker_changed.connect(self._apply_worker_state, Qt.ConnectionType.QueuedConnection)
        self._installation_changed.connect(self._poll_completion, Qt.ConnectionType.QueuedConnection)
        self._cache = Path(CACHE).absolute()
        self._record_path = self._cache / "installation.json"
        self._lock_path = self._cache / "attempt.lock"
        self._completion = QTimer(self)
        self._completion.setInterval(1000)
        self._completion.timeout.connect(self._poll_completion)
        self._poll_completion()

    @Property("QVariant", notify=changed)
    def state(self):
        return dict(self._s, scripts=f"{SHARE}/scripts")

    def _set(self, **kw):
        self._s.update(kw)
        self.changed.emit()

    @Slot(dict)
    def _apply_worker_state(self, state):
        self._set(**state)

    def _read_record(self):
        return persistence.read_json(self._record_path, _valid_record, {})

    def _result_path(self, record):
        return self._cache / (record["attempt"] + ".result")

    def _wait(self, status, latest):
        if not self._completion.isActive():
            self._completion.start()
        if (self._s["status"], self._s["latest"]) != (status, latest):
            self._set(status=status, latest=latest, message="")

    @Slot()
    def _poll_completion(self):
        record = self._read_record()
        if record.get("phase") == "downloading":
            try:
                with self._lock_path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    record = self._read_record()
            except BlockingIOError:
                self._wait("installing", record["latest"])
                return
            except OSError as error:
                record = dict(record, phase="failed", message=str(error))

        status, message = "error", ""
        if not record:
            if self._s["status"] not in ("installing", "started"):
                return
            message = "Could not read the update attempt."
        elif record["phase"] == "downloading":
            message = "The update download was interrupted."
        elif record["phase"] == "failed":
            message = record["message"]
        else:
            try:
                with self._result_path(record).open(encoding="ascii") as result:
                    raw = result.read(5)
                if not re.fullmatch(r"[0-9]{1,3}\n", raw) or int(raw) > 255:
                    raise ValueError("invalid installer exit status")
            except FileNotFoundError:
                if time.time() < record["deadline"]:
                    self._wait("started", record["latest"])
                    return
                message = "The installer did not report a result. Check the armadalive-update journal."
            except (OSError, ValueError):
                message = "Could not read the installer result. Check the armadalive-update journal."
            else:
                code = int(raw)
                if code == 0:
                    status = "installed"
                else:
                    message = f"Installation failed (exit status {code}). Check the armadalive-update journal."

        self._completion.stop()
        self._set(status=status, latest=record.get("latest", self._s["latest"]), message=message)

    @Slot()
    def openRepo(self):
        if self._s["repoUrl"]:
            QDesktopServices.openUrl(QUrl(self._s["repoUrl"]))

    @Slot()
    def check(self):
        self._check_requested.emit()

    @Slot()
    def _start_check(self):
        if not REPO_ID or self._s["status"] in ("checking", "installing", "started"):
            return
        self._set(status="checking", message="")
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            with _get(f"https://api.github.com/repos/{REPO_ID}/releases/latest") as response:
                release = json.load(response)
            tag = release["tag_name"]
            assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
            newer = _version(tag) > _version(__version__)
            if newer and ASSET in assets and CHECKSUM in assets:
                if not all(isinstance(assets[name], str) and assets[name] for name in (ASSET, CHECKSUM)):
                    raise ValueError("Invalid update download location")
        except (OSError, ValueError, KeyError, TypeError, HTTPException, RecursionError) as error:
            self._worker_changed.emit(dict(status="error", message=str(error)))
            return
        if not newer:
            self._worker_changed.emit(dict(status="uptodate", latest=tag, message=""))
        elif ASSET in assets and CHECKSUM in assets:
            self._worker_changed.emit(dict(status="available", latest=tag, tarball=assets[ASSET], sha=assets[CHECKSUM], message=""))
        else:
            self._worker_changed.emit(dict(status="error", latest=tag,
                                           message="Release has no checksum file; update by hand from GitHub"))

    @Slot()
    def install(self):
        self._install_requested.emit()

    @Slot()
    def _start_install(self):
        if self._s["status"] != "available":
            return
        release = (self._s["latest"], self._s["tarball"], self._s["sha"])
        self._set(status="installing", message="")
        threading.Thread(target=self._install, args=release, daemon=True).start()

    def _install(self, latest, tarball, checksum):
        try:
            self._cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    self._install_locked(latest, tarball, checksum)
        except OSError as error:
            self._worker_changed.emit(dict(status="error", message=str(error)))
        else:
            self._installation_changed.emit()

    def _install_locked(self, latest, tarball, checksum):
        previous = self._read_record()
        if previous:
            old_work = self._cache / previous["attempt"]
            old_result = self._result_path(previous)
            if previous["phase"] == "started" and time.time() < previous["deadline"] and not old_result.exists():
                return
            # Disable an expired attempt before its delayed wrapper can acquire the lock.
            try:
                shutil.rmtree(old_work)
            except FileNotFoundError:
                pass

        attempt = f"install-{uuid.uuid4().hex}"
        work = self._cache / attempt
        record = dict(attempt=attempt, latest=latest, phase="downloading")
        persistence.write_json(self._record_path, record)
        try:
            if previous:
                old_result.unlink(missing_ok=True)
                old_result.with_suffix(".result.part").unlink(missing_ok=True)
            work.mkdir(mode=0o700)
            root = self._download(work, tarball, checksum)
            # Keep shell syntax in a file so systemd cannot expand its variables.
            runner = work / "run-installer.sh"
            runner.write_text(RUN_INSTALLER, encoding="utf-8")
            record.update(phase="started", deadline=time.time() + INSTALL_TIMEOUT + LAUNCH_TIMEOUT)
            persistence.write_json(self._record_path, record)
            paths = [str(path).replace("$", "$$") for path in (runner, work, root, self._lock_path)]
            subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--unit=armadalive-update",
                            "--property=Type=exec", f"--property=RuntimeMaxSec={INSTALL_TIMEOUT}s",
                            "sh", *paths], check=True, timeout=LAUNCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            # The unit may already have been handed to systemd. Wait for its result.
            return
        except (OSError, ValueError, RuntimeError, EOFError, zlib.error, HTTPException,
                tarfile.TarError, subprocess.CalledProcessError) as error:
            shutil.rmtree(work, ignore_errors=True)
            persistence.write_json(self._record_path, dict(record, phase="failed", message=str(error) or type(error).__name__))

    def _download(self, work, tarball, checksum):
        archive = work / "release.tar.gz"
        with _get(tarball, 60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with _get(checksum) as response:
            tokens = response.read().decode().split(maxsplit=1)
        if not tokens or not re.fullmatch(r"[0-9a-fA-F]{64}", tokens[0]):
            raise ValueError("Release checksum is missing or malformed")
        if sha256(archive) != tokens[0].lower():
            raise ValueError("checksum mismatch, download discarded")
        dest = work / "release"
        with tarfile.open(archive) as package:
            package.extractall(dest, filter="data")  # refuses absolute paths, links out of the tree and device files
        (root,) = [entry for entry in dest.iterdir() if entry.is_dir()]
        if not (root / "install.sh").is_file():
            raise RuntimeError("release has no install.sh")
        return root
