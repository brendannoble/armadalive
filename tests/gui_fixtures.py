"""Disposable files and explicit external-I/O shims for the real-app GUI probe."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
from unittest.mock import patch


class Fixtures:
    def __init__(self, root, output):
        self.root, self.output = Path(root).resolve(), Path(output).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.calls, self.denied = [], []
        self.network_calls = []
        self.patches = []
        self.lock = threading.Lock()
        self.boundaries = [
            "All Python ~ path expansion points into a disposable home; XDG config/cache/runtime paths and ES-DE paths are disposable.",
            "subprocess.run returns explicit read-only CLI fixtures; unknown or mutating commands fail. subprocess.Popen is denied.",
            "Steam browser discovery returns disconnected via a queued Qt callback; Steam store urllib responses are delayed fixture bytes.",
            "InputPlumber system-bus connection reports disconnected; actual device opens and Python socket creation are denied.",
            "Python file mutation audit rejects paths outside fixture/output roots; sysfs battery/GPU values and helper presence use fixture files.",
            "Gamescope stats pipe points to a missing disposable path; process inspection and CPU/RAM reads remain real read-only operations.",
        ]
        self.names = ("Aurora Quest", "Coral Voyage", "Final Horizon")

    def text(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def data(self, relative, value):
        return self.text(relative, json.dumps(value))

    def seed(self):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        import pymupdf
        self.es = self.home / "ES-DE"
        self.text("ES-DE/settings/es_settings.xml", '<string name="ROMDirectory" value="' + str(self.home / "roms") + '"/>\n<bool name="CustomEventScripts" value="true"/>\n<bool name="CustomEventScriptsBrowsing" value="true"/>')
        self.text("ES-DE/custom_systems/es_systems.xml", '<systemList><system><name>snes</name><fullname>Super Nintendo</fullname><extension>.sfc</extension><command>/fixture/retroarch %ROM%</command></system></systemList>')
        games, records = [], {}
        for i, name in enumerate(self.names):
            stem = name.replace(" ", "_")
            self.text(f"roms/snes/{stem}.sfc", "fixture only; not a game ROM")
            games.append(f'<game><path>./{stem}.sfc</path><name>{name}</name><desc>Explore a luminous world, discover hidden routes, and reunite the islands. Local integration fixture {i + 1}.</desc><genre>Adventure</genre><developer>Armada Test Studio</developer><releasedate>19950101T000000</releasedate><rating>0.9</rating><players>1</players></game>')
            records[f"snes/{stem}"] = dict(title=name, system="snes", path=str(self.home / f"roms/snes/{stem}.sfc"), total=7200 + i * 1800, plays=8 + i, last=int(time.time()) - i * 86400, favorite=i == 2)
            for kind, size in (("fanart", (1600, 1000)), ("covers", (400, 600)), ("screenshots", (640, 360))):
                path = self.home / f"ES-DE/downloaded_media/snes/{kind}/{stem}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                image = QImage(*size, QImage.Format.Format_RGB32)
                image.fill(QColor(["#10244b", "#074b59", "#482b6e"][i]))
                painter = QPainter(image)
                painter.setPen(QColor("#78d9d4"))
                painter.setBrush(QColor(["#266496", "#227a7e", "#8265b7"][i]))
                painter.drawEllipse(QRectF(size[0] * .12, size[1] * .13, size[0] * .72, size[1] * .65))
                painter.fillRect(0, int(size[1] * .78), size[0], int(size[1] * .22), QColor("#0d172c"))
                painter.end()
                assert image.save(str(path))
            self.text(f"ES-DE/downloaded_media/snes/marquees/{stem}.svg", f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="200" viewBox="0 0 800 200"><rect x="5" y="5" width="790" height="190" rx="40" fill="#182944" stroke="#7ce1d6" stroke-width="7"/><text x="400" y="120" text-anchor="middle" fill="#ffffff" font-family="sans-serif" font-size="65">{name}</text></svg>')
            self.text(f".config/retroarch/cheats/{stem}.cht", 'cheats = 2\ncheat0_desc = "Infinite health"\ncheat0_code = "7E0DBF63"\ncheat1_desc = "Unlock all stages"\ncheat1_code = "7E0DC164"\n')
            manual = self.home / f"ES-DE/downloaded_media/snes/manuals/{stem}.pdf"
            manual.parent.mkdir(parents=True, exist_ok=True)
            with pymupdf.open() as doc:
                for p in range(3):
                    page = doc.new_page(width=400, height=480)
                    page.insert_text((30, 55), name, fontsize=25)
                    page.insert_text((30, 100), f"FIELD GUIDE / PAGE {p + 1}", fontsize=15)
                    page.insert_textbox((30, 150, 370, 400), "Move with the directional pad.\nJump with B. Interact with A.\nExplore every island to complete your journey.\n\nA generated local manual for integration testing.", fontsize=17)
                doc.save(manual)
        self.text("ES-DE/gamelists/snes/gamelist.xml", "<gameList>" + "".join(games) + "</gameList>")
        self.data(".config/armadalive/ui.json", dict(theme="stock", onboarded=True, animate=True, dim=False, ledFollow=False))
        self.data(".config/armadalive/history.json", dict(games=records, days={}, imported=True, steam_baseline={}))
        self.data(".config/armadalive/apps.json", dict(slots=["smart:recent", "smart:favorites", "smart:esde:snes", "steam:10"], folders={}, next=1))
        self.text(".local/share/Steam/steamapps/appmanifest_10.acf", '"AppState" { "appid" "10" "name" "Fixture Steam Game" }')
        self.text("hardware/helper", "fixture helper")
        for name, value in dict(capacity=73, current_now=-1100000, voltage_now=4000000, charge_now=4800000, charge_full=6600000, status="Discharging", charge_control_end_threshold=100).items():
            self.text(f"hardware/battery/{name}", str(value))
        self.text("hardware/gpu_freq", "550000000")

    def install(self):
        original_expand = os.path.expanduser
        self.patches.append(patch("os.path.expanduser", lambda path: str(self.home) + path[1:] if isinstance(path, str) and (path == "~" or path.startswith("~/")) else original_expand(path)))
        for key, value in {"XDG_CONFIG_HOME": self.home / ".config", "XDG_CACHE_HOME": self.home / ".cache", "XDG_RUNTIME_DIR": self.root / "run", "ARMADALIVE_ESDE_DIR": self.es, "ARMADALIVE_LUDO_LOGS": self.home / "ludo-logs"}.items():
            Path(value).mkdir(parents=True, exist_ok=True)
            os.environ[key] = str(value)
        os.chmod(self.root / "run", 0o700)
        self.patches.extend([patch("subprocess.run", self.run), patch("subprocess.Popen", self.no_process), patch("urllib.request.urlopen", self.urlopen)])
        for p in self.patches:
            p.start()
        sys.dont_write_bytecode = True
        sys.addaudithook(self.audit)

    def finish_imports(self):
        from PySide6.QtCore import QTimer
        from armadalive import ayn, gamescope, privileged, steamui, system, battery
        # Boundary substitutions only: their owners, timers, signals and workers are real.
        class NoBus:
            def connect(self, *args):
                return False
        self.patches.append(patch.object(ayn.QDBusConnection, "systemBus", lambda: NoBus()))
        self.patches.append(patch.object(steamui, "_discover", lambda shared, done: QTimer.singleShot(0, lambda: done(None))))
        for p in self.patches[-2:]:
            p.start()
        gamescope.PIPE = str(self.root / "missing-stats.pipe")
        privileged.HELPER = str(self.home / "hardware/helper")
        system.BATTERY = battery.BATTERY = str(self.home / "hardware/battery")
        battery.LIMIT_FILE = str(self.home / "hardware/battery/charge_control_end_threshold")
        system.GPU_DEVFREQ = str(self.home / "hardware/gpu_freq")

    def audit(self, event, args):
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes)):
                path = os.fsdecode(path)
                write = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                if path.startswith("/dev/") and path != "/dev/null":
                    self.denied.append([event, path, "device"])
                    raise PermissionError(13, "device isolated by integration fixture", path)
                if write:
                    self.check_path(path, event)
        elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"):
            self.check_path(args[0], event)
        elif event in ("os.rename", "os.link", "os.symlink"):
            for path in args[:2]:
                self.check_path(path, event)
        elif event.startswith("socket.") or event in ("os.system", "subprocess.Popen", "os.kill", "os.killpg"):
            self.denied.append([event, "blocked"])
            raise PermissionError("External operation denied by integration fixture")

    def check_path(self, path, event):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        resolved = Path(os.fsdecode(path)).resolve()
        if resolved.is_relative_to(self.root) or resolved.is_relative_to(self.output):
            return
        self.denied.append([event, str(resolved), "write"])
        raise PermissionError(f"External write denied: {resolved}")

    def run(self, args, **kwargs):
        words = list(map(str, args))
        with self.lock:
            self.calls.append(dict(args=words, thread=threading.get_ident(), at=time.monotonic()))
        out, code = "", 0
        if words[0] == "xprop" and "-set" not in words:
            values = dict(GAMESCOPE_FOCUSED_APP=0, GAMESCOPE_FPS_LIMIT=60, GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK=60, GAMESCOPE_DYNAMIC_REFRESH=60)
            out = "\n".join(f"{name}(CARDINAL) = {values[name]}" for name in words if name in values)
        elif words[0] == "busctl" and "org.armada.Power" in words and "GetAll" in words:
            power = dict(Temperature=47, FanRpm=1800, FanPwm=35, Profile="balanced", AvailableProfiles=["eco", "balanced", "performance"], GpuPerformanceLevel="auto", ManualGpuClockMin=220, ManualGpuClockMax=680, ManualGpuClock=550)
            out = json.dumps({"data": [{k: {"data": v} for k, v in power.items()}]})
        elif words[0] == "busctl" and "list" in words:
            out = "[]" if "--json=short" in words else ""
        elif words[:3] == ["sudo", "-n", "-l"]:
            pass
        elif words[:3] == ["nmcli", "radio", "wifi"]:
            out = "enabled\n"
        elif words[:3] == ["rfkill", "list", "bluetooth"]:
            out = "Soft blocked: no\nHard blocked: no\n"
        elif words[0] == "iw" and "get" in words:
            out = "Power save: on\n"
        elif words[:2] == ["wpctl", "get-volume"]:
            out = "Volume: 0.45\n"
        elif words[:3] == ["tailscale", "status", "--json"]:
            out = '{"BackendState":"Stopped"}'
        else:
            code = 1
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, args, output="", stderr="fixture denies command")
        binary = not kwargs.get("text", False)
        return subprocess.CompletedProcess(args, code, out.encode() if binary else out, b"" if binary else "")

    def no_process(self, *args, **kwargs):
        self.denied.append(["Popen", str(args[0])])
        raise PermissionError("Process launch isolated by integration fixture")

    def urlopen(self, request, **kwargs):
        from urllib.parse import parse_qs, urlparse
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if not url.startswith("https://store.steampowered.com/api/appdetails?"):
            raise PermissionError("Network request isolated by integration fixture")
        appid = parse_qs(urlparse(url).query)["appids"][0]
        self.network_calls.append(appid)
        time.sleep(.12)
        return io.BytesIO(json.dumps({appid: {"success": True, "data": {"name": "Steam Fixture " + appid, "short_description": "Fetched on a real metadata worker from fixture response bytes.", "developers": ["Fixture Studio"]}}}).encode())

    def event(self, index=2):
        return dict(event="game-select", system="snes", path=str(self.home / "roms/snes" / (self.names[index].replace(" ", "_") + ".sfc")), name=self.names[index])
