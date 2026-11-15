"""Executable-only service fixtures for test_runtime_integration.

The tests install this file under command names in a temporary, exclusive PATH.
It never calls a system service, opens a socket, or writes outside its fixture.
"""
import json
import os
from pathlib import Path
import sys
import time


ROOT = Path(os.environ["ARMADALIVE_RUNTIME_FIXTURE"])
COMMAND = Path(sys.argv[0]).name
ARGS = sys.argv[1:]
CONFIG = json.loads((ROOT / "config.json").read_text())


def record(event, **extra):
    line = json.dumps(dict(event=event, command=COMMAND, args=ARGS,
                           pid=os.getpid(), time=time.monotonic(), **extra)) + "\n"
    fd = os.open(ROOT / "calls.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def gate(key):
    if name := CONFIG.get(key):
        record("waiting", gate=name)
        deadline = time.monotonic() + 10
        while not (ROOT / (name + ".release")).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Test did not release " + name)
            time.sleep(0.005)


def output(value):
    print(json.dumps(value))


def mode_result(mode):
    if mode == "hang":
        # Production subprocess timeouts must kill and reap this process.
        time.sleep(30)
    if mode == "fail":
        print("fixture service unavailable", file=sys.stderr)
        return 7
    if mode == "malformed":
        print("{not valid structured output")
        return 0
    return None


def main():
    if COMMAND == "wpctl" and ARGS == ["get-volume", "@DEFAULT_AUDIO_SINK@"]:
        snapshot = (ROOT / "volume").read_text()
        gate("volume_read_gate")
        print("Volume: " + snapshot)
    elif COMMAND == "wpctl" and ARGS[:2] == ["set-volume", "@DEFAULT_AUDIO_SINK@"]:
        gate("volume_write_gate")
        if CONFIG.get("volume_write_mode") == "fail":
            return 7
        temporary = ROOT / ("volume." + str(os.getpid()))
        temporary.write_text(ARGS[2])
        temporary.replace(ROOT / "volume")
    elif COMMAND == "nmcli" and ARGS == ["radio", "wifi"]:
        print("disabled")
    elif COMMAND == "rfkill" and ARGS == ["list", "bluetooth"]:
        print("Soft blocked: no\nHard blocked: no")
    elif COMMAND == "iw" and ARGS[-2:] == ["get", "power_save"]:
        print("Power save: off")
    elif COMMAND == "tailscale" and ARGS == ["status", "--json"]:
        output({"BackendState": "Stopped"})
    elif COMMAND == "busctl" and "--system" in ARGS:
        if ARGS != ["--system", "--json=short", "call", "org.armada.Power", "/org/armada/Power",
                    "org.freedesktop.DBus.Properties", "GetAll", "s", "org.armada.Power1"]:
            raise RuntimeError("Unexpected power request")
        if (result := mode_result(CONFIG.get("power_mode"))) is not None:
            return result
        output({"data": [{"Temperature": {"data": CONFIG.get("temperature", 51)},
                           "Profile": {"data": "Balanced"},
                           "AvailableProfiles": {"data": ["Balanced", "Performance"]}}]})
    elif COMMAND == "busctl" and ARGS[0].startswith("--address="):
        mode = CONFIG.get("media_mode", "healthy")
        if mode in ("malformed", "fail"):
            return mode_result(mode)
        names = ["org.mpris.MediaPlayer2." + name for name in ("broken", "healthy", "third")]
        if ARGS[-1] == "list":
            output([{"name": name} for name in names])
        elif ARGS[2] == "get-property" and ARGS[-1] in ("PlaybackStatus", "Metadata"):
            if mode == "hang":
                return mode_result("hang")
            if ARGS[3] == names[0]:
                return 7
            if ARGS[-1] == "PlaybackStatus":
                output({"data": "Playing"})
            else:
                output({"data": {"xesam:title": {"data": CONFIG.get("title", "Recovered music")},
                                  "xesam:artist": {"data": ["Fixture artist"]}}})
        else:
            raise RuntimeError("Unexpected media request")
    elif COMMAND == "sudo" and ARGS[:2] == ["-n", "-l"]:
        if (result := mode_result(CONFIG.get("helper_mode"))) is not None:
            return result
    else:
        raise RuntimeError("Unexpected command: " + repr([COMMAND, *ARGS]))
    return 0


record("start")
try:
    status = main()
except Exception as error:
    record("error", detail=str(error))
    raise
record("exit", status=status)
sys.exit(status)
