#!/usr/bin/env bash
# Runs inside Docker or directly on a Linux host with the pinned dependencies.
# Always runs both phases, keeps their logs, and returns the first failing status.
set -euo pipefail

if (( $# > 1 )); then
    echo "Usage: $0 [report-directory]" >&2
    exit 2
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
report_dir=${1:-/reports}
mkdir -p -- "$report_dir"
report_dir=$(cd -- "$report_dir" && pwd)
# Do not let an old successful report survive an interrupted new run.
if [[ -e "$report_dir/summary.json" || -e "$report_dir/unittest.log" || -e "$report_dir/gui.log" ]]; then
    echo "Use a fresh report directory: $report_dir already contains validation results" >&2
    exit 2
fi
run_dir=$(mktemp -d "${TMPDIR:-/tmp}/armadalive-validation.XXXXXXXX")
trap 'rm -rf -- "$run_dir"' EXIT
mkdir -p -- "$run_dir/home" "$run_dir/config" "$run_dir/cache" "$run_dir/runtime" "$run_dir/data"
chmod 700 "$run_dir/runtime"

python_bin=${ARMADALIVE_VALIDATION_PYTHON:-python3}
unit_timeout=${ARMADALIVE_UNIT_TIMEOUT:-300}
gui_timeout=${ARMADALIVE_GUI_TIMEOUT:-300}
for duration in "$unit_timeout" "$gui_timeout"; do
    if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
        echo 'Validation timeouts must be positive whole seconds' >&2
        exit 2
    fi
done
command -v timeout >/dev/null
command -v "$python_bin" >/dev/null
# Keep interpreter-installed user packages available when HOME is isolated.
# Docker installs globally; this matters for direct local runs using pip --user.
user_site=$("$python_bin" -c 'import site; print(site.getusersitepackages())')
module_path=$source_dir
if [[ -d "$user_site" ]]; then module_path="$module_path:$user_site"; fi

# Isolate preferences and runtime files, including when invoked outside Docker.
# Do not propagate session displays or a real system/session bus address.
runtime_env=(env -u DISPLAY -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS
    -u DBUS_SYSTEM_BUS_ADDRESS -u QSG_RHI_BACKEND
    HOME="$run_dir/home" XDG_CONFIG_HOME="$run_dir/config"
    XDG_CACHE_HOME="$run_dir/cache" XDG_DATA_HOME="$run_dir/data"
    XDG_RUNTIME_DIR="$run_dir/runtime" TMPDIR="$run_dir" PYTHONPATH="$module_path"
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1
    QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QT_SCALE_FACTOR=1
    LANG=C.UTF-8 LC_ALL=C.UTF-8)
cd -- "$source_dir"

"${runtime_env[@]}" "$python_bin" - "$report_dir/environment.json" <<'PY'
import importlib.metadata, json, os, pathlib, platform, sys
from PySide6.QtCore import qVersion
versions = {name: importlib.metadata.version(name) for name in
            ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "PyMuPDF")}
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "python": sys.version, "platform": platform.platform(), "qt": qVersion(),
    "packages": versions, "uid": os.getuid(), "source": os.getcwd(),
    "rendering": {name: os.environ[name] for name in
                  ("QT_QPA_PLATFORM", "QT_QUICK_BACKEND", "QT_SCALE_FACTOR")},
}, indent=2) + "\n")
PY

echo "Running complete unittest discovery; log: $report_dir/unittest.log"
unit_status=0
"${runtime_env[@]}" timeout --signal=TERM --kill-after=10s "${unit_timeout}s" \
    "$python_bin" -m unittest discover -s tests -t . -v \
    >"$report_dir/unittest.log" 2>&1 || unit_status=$?
echo "Unittest exit status: $unit_status"

echo "Running graphical interface integration; log: $report_dir/gui.log"
gui_status=0
"${runtime_env[@]}" timeout --signal=TERM --kill-after=10s "${gui_timeout}s" \
    "$python_bin" tests/gui_integration.py --output-dir "$report_dir/gui" \
    >"$report_dir/gui.log" 2>&1 || gui_status=$?
echo "Graphical interface exit status: $gui_status"

"${runtime_env[@]}" "$python_bin" - "$report_dir" "$unit_status" "$gui_status" <<'PY'
import json, pathlib, re, sys
reports = pathlib.Path(sys.argv[1])
unit_status, gui_status = map(int, sys.argv[2:])
log = (reports / "unittest.log").read_text(errors="replace")
count = re.search(r"^Ran (\d+) tests? in .+$", log, re.MULTILINE)
summary_line = next((line for line in reversed(log.splitlines())
                     if line.startswith(("FAILED", "OK"))), None)
summary = {
    "unittest": {"exit_code": unit_status, "completed": bool(count),
                 "tests": int(count.group(1)) if count else None,
                 "summary": summary_line, "log": "unittest.log"},
    "gui": {"exit_code": gui_status, "log": "gui.log", "artifacts": "gui/"},
    "exit_code": unit_status or gui_status,
}
(reports / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
PY

if (( unit_status != 0 )); then exit "$unit_status"; fi
exit "$gui_status"
