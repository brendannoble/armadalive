# Container validation recipe

This image runs the complete Python unittest suite and the real Qt graphical user
interface (GUI) integration harness. The harness uses offscreen software rendering,
temporary fixture data and command shims instead of a connected handheld. It saves
screenshots and interaction results. It does not emulate Armada OS (operating system),
Steam, a display compositor, input hardware or privileged services.

**Execution status:** the image has not been built or run in the authoring workspace.
Docker, Podman and a Docker daemon socket are unavailable there. Local Python runs
are separate evidence; they are not evidence of a successful container build or run.

## Build and run

Run these commands from the repository root on a machine with Docker. Building needs
network access to Docker Hub, Debian package repositories and the Python package
index. Running needs no network access and does not install dependencies.

```sh
docker build --platform=linux/amd64 -f Dockerfile.validation -t armadalive-validation .

# Use a new output directory for each run; do not reuse a previous successful run.
report_dir=$(mktemp -d "$PWD/validation-report.XXXXXXXX")
docker run --rm --init --platform=linux/amd64 \
    --network=none --read-only --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --user "$(id -u):$(id -g)" \
    --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
    --mount "type=bind,src=$report_dir,dst=/reports" \
    armadalive-validation
```

Run this command as an ordinary host user so the container remains nonroot and can
write its bind-mounted reports. The image itself defaults to user and group 10001.
The source is included in the image, made read-only during the build, and protected
by the read-only runtime filesystem. Only temporary state and the report mount are
writable. Temporary state contains executable command shims, so `/tmp` intentionally
does not use `noexec`. Do not mount device nodes, host configuration, display sockets,
service sockets or the Docker daemon socket into the test container.

The default image is Python 3.12 on Debian Bookworm, with **PySide6 6.12.0** and
**PyMuPDF 1.26.6**. PySide6 pins its matching Addons, Essentials and shiboken6 packages.
The recipe targets `linux/amd64`: PySide6 6.12.0's arm64 wheel requires a newer GNU C
Library than Bookworm provides. Running this target through emulation on an arm64
host does not validate native handheld behavior.

Python's base tag and Debian package revisions are not digest/snapshot pinned, so
the recipe is repeatable but not byte-for-byte reproducible. For an immutable build,
record the resolved image digest and package versions, and pass a verified base
reference using `--build-arg PYTHON_IMAGE=python@sha256:...`. Do not invent a digest.

## Results and failure behavior

The runner always attempts both validation phases, even if the unittest suite fails,
crashes or times out. It never filters out baseline failures. Each phase has a
300-second deadline, with ten seconds for termination before forced shutdown. Set
`ARMADALIVE_UNIT_TIMEOUT` or `ARMADALIVE_GUI_TIMEOUT` with `docker run -e` to change
those deadlines, in whole seconds. A timeout normally returns status 124; forced
termination can return 137. The outer runner returns the first nonzero phase status.

The reports contain:

| File | Contents |
| --- | --- |
| `environment.json` | Actual Python, Qt and package versions, platform and rendering mode |
| `unittest.log` | Complete discovery output, including every failure and traceback |
| `gui.log` | Complete GUI harness output |
| `gui/` | Screenshots and structured results produced by the harness |
| `summary.json` | Both exit statuses and the completed unittest count, if present |

If unittest crashes, its completed test count is `null`; partial execution must not
be reported as a passing suite. The runner refuses to reuse a directory containing
an earlier log or summary. Interrupting the outer process can leave partial logs and
no summary; that is an incomplete run. No screenshot or log is deleted on a phase
failure. Inspect the individual logs, structured GUI report and screenshots before
making a release decision.

Without Docker, the same runner can use locally installed dependencies:

```sh
bash scripts/validate-container.sh "$(mktemp -d "$PWD/local-validation.XXXXXXXX")"
```

This local command isolates preferences but does not provide Docker's network or
filesystem isolation. It also inherits host restrictions: this authoring workspace
denies Unix socket creation, which prevents several existing `tests/test_run_sh.py`
cases from completing normally. Those failures remain in the suite and its exit
status. A native Qt crash in a local run must likewise remain visible in its report.

Software rendering exercises Qt Quick layout, Python bindings, event delivery and
application behavior. It cannot validate graphics acceleration, shader effects,
frame pacing, actual device input, hardware power control, or live desktop service
integration. Those checks still require the target device.

## Dependency references

- [Qt for Python installation and bundled Qt libraries](https://doc.qt.io/qtforpython-6/gettingstarted.html)
- [PySide6 6.12.0 wheel platform requirements](https://pypi.org/project/PySide6/6.12.0/)
- [PyMuPDF 1.26.6 release files](https://pypi.org/project/PyMuPDF/1.26.6/)
- [Python 3.12 slim Bookworm image](https://hub.docker.com/_/python/tags?name=3.12-slim-bookworm)
- [Qt Quick software rendering limitations](https://doc.qt.io/qt-6/qtquick-visualcanvas-adaptations-software.html)
