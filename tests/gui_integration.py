#!/usr/bin/env python3
"""Run ArmadaLive itself under offscreen Qt with disposable external-I/O fixtures.

Usage: QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software \
    python tests/gui_integration.py --output-dir .coordination/validation/gui

This is a system-free integration probe, not a replacement UI. The unchanged
app.main constructs every production backend, loads Main.qml, and owns the
presentation lifecycle. Only OS/network boundaries are substituted. It does
not validate a compositor, real input devices, GPU rendering, or privileged
hardware changes. All artifacts and assertions are recorded in report.json.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
sys.dont_write_bytecode = True

from tests.gui_fixtures import Fixtures

ROUTES = dict(now="NowPlaying.qml", manual="Manual.qml", cheats="Cheats.qml", apps="Apps.qml", controls="Trackpad.qml", system="System.qml", prefs="Preferences.qml", tuning="Tuning.qml", graphs="Graphs.qml", rules="Rules.qml", tasks="Tasks.qml", stats="Stats.qml", info="GameInfo.qml")
BACKENDS = "steamui gamescope nowPlaying system ayn settings apps history gameinfo shell controls ui updater admin health rules tileStore battery media backup artwork steamShortcuts tasks onboarding activity manuals ludo trackpad cheats layout emulatorControl pad closer thermal".split()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture_tmp = tempfile.TemporaryDirectory(prefix="armadalive-gui-")
    fixtures = Fixtures(fixture_tmp.name, output)
    fixtures.seed()
    fixtures.install()

    import PySide6
    import pymupdf
    from PySide6.QtCore import QObject, QPoint, QPointF, QTimer, QThreadPool, Qt, qInstallMessageHandler, qVersion
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression, QJSValue
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from armadalive import app as production
    fixtures.finish_imports()

    report = dict(status="running", runtime=dict(pyside=PySide6.__version__, qt=qVersion(), pymupdf=pymupdf.__version__, platform=os.environ["QT_QPA_PLATFORM"], renderer=os.environ["QT_QUICK_BACKEND"]), assertions=[], routes=[], screenshots=[], warnings=[], shim_boundaries=fixtures.boundaries,
                  limitations=["This probe uses Qt's real offscreen software renderer; it does not exercise a window system or compositor.", "Hardware, external service success, compositor visibility, physical multitouch and GPU rendering require an Armada device or suitable desktop integration environment.", "Fixture boundaries replace process execution, store response bytes and Steam discovery; backend scheduling, workers, signals, models, file parsing and QML pages are production code."])
    started = time.monotonic()

    def message(kind, context, text):
        entry = dict(type=kind.name, message=text, file=context.file or "", line=context.line)
        report["warnings"].append(entry)
        print("QT:", text, file=sys.stderr, flush=True)

    previous_handler = qInstallMessageHandler(message)
    state = {}

    def check(condition, name, detail=None):
        report["assertions"].append(dict(name=name, passed=bool(condition), detail=detail))
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    def value(obj, name):
        v = obj.property(name)
        return v.toVariant() if isinstance(v, QJSValue) else v

    def wait_for(predicate, timeout=4000, description="condition"):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            if predicate():
                return
            QTest.qWait(20)
        check(False, "wait: " + description)

    def visual_items(root):
        # QML delegates can have a different QObject parent; follow the visual tree.
        out = []
        def visit(item):
            out.append(item)
            for child in item.childItems():
                visit(child)
        visit(root.contentItem() if hasattr(root, "contentItem") else root)
        return out

    def current_page(route=None):
        window = state["window"]
        route = route or window.property("page")
        candidates = [o for o in window.findChildren(QObject) if o.inherits("QQuickLoader") and str(o.property("source").toString()).endswith(ROUTES[route])]
        if not candidates:
            return None
        return candidates[0].property("item")

    def go(route):
        window = state["window"]
        window.setProperty("page", route)
        wait_for(lambda: window.property("page") == route and current_page(route) is not None, description=route + " real Loader")
        QTest.qWait(150)
        check(value(window, "current")["id"] == route, f"route {route} did not fall back")
        page = current_page(route)
        check(page is not None and page.metaObject().className().startswith(Path(ROUTES[route]).stem + "_"), f"route {route} loaded {ROUTES[route]}", page.metaObject().className() if page else None)
        return page

    def screenshot(name):
        window = state["window"]
        QTest.qWait(80)
        image = window.grabWindow()
        check(not image.isNull() and image.width() == window.width() and image.height() == window.height(), "render " + name, [image.width(), image.height()])
        path = output / (name + ".png")
        check(image.save(str(path)), "save " + name)
        report["screenshots"].append(path.name)

    def click_label(label):
        window = state["window"]
        candidates = [o for o in visual_items(window) if o.isVisible() and o.opacity() > .01 and o.property("label") == label and o.width() > 0 and o.height() > 0]
        check(bool(candidates), "click target " + label)
        item = candidates[-1]
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        check(0 <= point.x() < window.width() and 0 <= point.y() < window.height(), "onscreen target " + label, [point.x(), point.y()])
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(250)

    def click_text(text):
        window = state["window"]
        hits = [o for o in visual_items(window) if o.isVisible() and o.property("text") == text and o.width() > 0]
        check(bool(hits), "text target " + text)
        item = hits[-1]
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(250)

    def scenario():
        window, backend = state["window"], state["backends"]
        try:
            check(len(backend) == 34, "production backend context retained", len(backend))
            check(all(type(o).__module__.startswith("armadalive.") for o in backend.values()), "all application backends are real production objects")
            wait_for(lambda: not backend["apps"].catalogLoading and len(backend["apps"]._esde) > 0, description="real asynchronous catalog scan")
            check(len(backend["apps"]._esde) == 3, "three fixture games parsed by catalog", list(backend["apps"]._esde))
            check(backend["history"].info("snes/Final_Horizon")["total"] == 10800, "fixture play history parsed")
            wait_for(lambda: backend["system"].stats["battery"] == 73 and backend["system"].stats["temp"] == 47, description="telemetry pollers apply fixture reads")
            backend["nowPlaying"].pin(fixtures.event())
            QTest.qWait(450)
            check(backend["nowPlaying"].state["hasCheats"] and backend["nowPlaying"].state["manual"], "selected game discovers cheats and manual")
            check(len(backend["nowPlaying"].cheats()) == 2, "real cheat parser returns seeded entries")
            page = go("now")
            artwork_images = [item for item in visual_items(page) if item.inherits("QQuickImage") and item.property("source").toString() and item.isVisible()]
            check(len(artwork_images) >= 2, "real logo and fanart are loaded")
            check(all(item.property("asynchronous") for item in artwork_images), "artwork image decoding is asynchronous")
            check(all(0 < item.implicitWidth() <= 1280 and 0 < item.implicitHeight() <= 1080 for item in artwork_images), "decoded artwork stays within requested bounds", [[item.implicitWidth(), item.implicitHeight()] for item in artwork_images])
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(window.width() // 2, 180))
            QTest.qWait(220)
            check(page.property("extras"), "artwork click opens game actions")
            click_label("Info")
            check(window.property("page") == "info", "actual Info click navigates")
            check(value(current_page(), "d")["name"] == "Final Horizon", "real game metadata reaches Info")
            click_label("Apps")
            check(window.property("page") == "apps", "actual bottom navigation click")
            click_label("System")
            check(window.property("page") == "system", "actual System navigation click")
            click_label("Settings")
            check(window.property("page") == "prefs", "actual dashboard Settings click")
            prefs = current_page()
            click_text("Appearance")
            check(prefs.property("section") == "appearance", "actual Appearance menu click")
            # Choose a visible native theme swatch and check persistent UI settings.
            before = backend["ui"].state["theme"]
            swatches = [o for o in visual_items(prefs) if o.isVisible() and o.property("th") is not None and not o.property("on")]
            check(bool(swatches), "theme swatches available")
            swatch = swatches[0]
            point = swatch.mapToScene(QPointF(swatch.width()/2, swatch.height()/2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
            QTest.qWait(300)
            check(backend["ui"].state["theme"] != before, "actual theme swatch click persisted", backend["ui"].state["theme"])
            screenshot("theme-appearance")
            backend["ui"].setTheme("stock")

            for route in ROUTES:
                page = go(route)
                if route == "manual":
                    wait_for(lambda: any(o.inherits("QQuickLoader") and o.property("item") is not None for o in page.findChildren(QObject)), description="actual manual viewer")
                    viewers = [o.property("source").toString() for o in page.findChildren(QObject) if o.inherits("QQuickLoader") and o.property("item") is not None]
                    report["manual_viewer"] = viewers
                    # Some distributions lack the optional Qt PDF module and use the actual image fallback.
                    if any(s.endswith("ManualImages.qml") for s in viewers):
                        wait_for(lambda: len(backend["manuals"].pages) == 3, description="manual worker renders all pages")
                if route == "cheats":
                    label = next(o for o in visual_items(page) if o.property("text") == "Infinite health")
                    card = label.parentItem().parentItem()
                    width = lambda: QQmlExpression(state["engine"].rootContext(), card, "border.width").evaluate()[0]
                    before_border = width()
                    click_text("Infinite health")
                    check(backend["cheats"].is_active("snes/Final_Horizon", 0), "actual cheat click changes backend state")
                    config = fixtures.home / ".config/retroarch/config/Snes9x/Final_Horizon.opt"
                    check('cheat0_enable = "true"' in config.read_text(), "actual cheat click saves only disposable emulator configuration")
                    report["cheat_toggle"] = dict(before_border=before_border, after_border=width(), expected_enabled_border=2)
                    report["assertions"].append(dict(name="enabled cheat has selected border", passed=width() == 2, detail=report["cheat_toggle"]))
                if route == "tasks":
                    check(backend["tasks"]._active, "task listing activated by real page")
                if route == "info":
                    check(value(page, "d")["desc"].startswith("Explore a luminous world"), "description comes from real XML metadata")
                report["routes"].append(dict(route=route, source=ROUTES[route], object=page.metaObject().className(), size=[page.width(), page.height()]))
                screenshot("route-" + route)
            check(not backend["tasks"]._active, "task polling stops after leaving Tasks")

            # Exercise the supported image viewer through the real Manual.qml fallback
            # Loader even when this Qt distribution includes its native PDF module.
            page = go("manual")
            loaders = [o for o in page.findChildren(QObject) if o.inherits("QQuickLoader")]
            native = next(o for o in loaders if o.property("source").toString().endswith("ManualPdf.qml"))
            fallback = next(o for o in loaders if o is not native)
            native.setProperty("active", False)
            fallback.setProperty("active", True)
            wait_for(lambda: fallback.property("item") is not None and len(backend["manuals"].pages) == 3, description="real image fallback and manual render worker")
            check(fallback.property("source").toString().endswith("ManualImages.qml"), "supported manual image fallback is real component")
            check(backend["manuals"].model.rowCount() == 3, "manual worker populates incremental model")
            screenshot("manual-image-fallback")
            window.setProperty("blanked", True)
            QTest.qWait(100)
            check(not backend["manuals"]._presenting, "blanked manual pauses rendering")
            source = backend["manuals"]._source
            for index in (0, 1, 2):
                from PySide6.QtCore import QUrl
                path = fixtures.home / "ES-DE/downloaded_media/snes/manuals" / (fixtures.names[index].replace(" ", "_") + ".pdf")
                backend["manuals"].load(QUrl.fromLocalFile(str(path)).toString())
            check(backend["manuals"].pages == [], "hidden manual selection defers rendering")
            window.setProperty("blanked", False)
            wait_for(lambda: len(backend["manuals"].pages) == 3 and backend["manuals"]._active is None, description="manual resumes latest selection")
            check(backend["manuals"]._source == source, "rapid manual selections finish with latest document")
            go("now")

            # Rapid real selection updates and asynchronous Steam metadata reads.
            for index in (0, 1, 0, 2):
                backend["nowPlaying"].pin(fixtures.event(index))
                QTest.qWait(15)
            page = go("info")
            check(value(page, "d")["name"] == fixtures.names[-1], "rapid selection final metadata is current")
            backend["nowPlaying"].pin(ui=[10, "Steam Fixture 10"])
            page = go("info")
            backend["nowPlaying"].pin(ui=[20, "Steam Fixture 20"])
            backend["nowPlaying"].pin(ui=[30, "Steam Fixture 30"])
            wait_for(lambda: value(page, "d").get("name") == "Steam Fixture 30", description="latest queued Steam metadata response")
            check(backend["gameinfo"]._active is None, "metadata worker completes final request")
            check("30" in fixtures.network_calls, "real metadata fetch consumed mocked HTTP response", fixtures.network_calls)
            screenshot("rapid-steam-selection")
            backend["nowPlaying"].pin(fixtures.event())

            # Exercise actual root visibility wiring and observe actual poller dispatches.
            page = go("now")
            pollers = [(name, o) for name, b in backend.items() for o in b.findChildren(QObject) if type(o).__name__ == "Poller"]
            expected_gated = {
                ("system", "_detail_poller"): (1000, 15000),
                ("gamescope", "_display_poller"): (3000, 15000),
                ("admin", "_poller"): (3000, 15000),
                ("controls", "_poller"): (2000, 15000),
                ("controls", "_vpn_poller"): (6000, 30000),
                ("health", "_poller"): (5000, 30000),
                ("media", "_poller"): (3000, 15000),
            }
            named_pollers = {(name, attr): obj for name, owner in backend.items()
                             for attr, obj in vars(owner).items() if type(obj).__name__ == "Poller"}
            actual_gated = {key for key, p in named_pollers.items() if p._idle_ms is not None}
            check(actual_gated == set(expected_gated), "exact seven presentation pollers retain lifecycle gating", sorted(actual_gated))
            check(all((named_pollers[key]._ms, named_pollers[key]._idle_ms) == intervals
                      for key, intervals in expected_gated.items()), "presentation pollers retain expected active and idle intervals")
            expected_essential = {("system", "_power_poller"), ("system", "_essential_poller")}
            actual_essential = {key for key, p in named_pollers.items() if key[0] == "system" and p._idle_ms is None}
            check(actual_essential == expected_essential, "exact two system pollers retain essential telemetry", sorted(actual_essential))
            gated = [(name, named_pollers[(name, attr)]) for name, attr in expected_gated]
            essential = [named_pollers[key] for key in expected_essential]
            counters = Counter()
            for index, (_, poller) in enumerate(pollers):
                work = poller._work
                def counted(work=work, index=index):
                    counters[index] += 1
                    return work()
                poller._work = counted
            QTest.qWait(1150)
            first = page.property("breathe")
            QTest.qWait(120)
            check(page.property("breathe") != first, "visible logo animation advances")
            lifecycle = []
            for mode in ("blanked", "hidden", "minimized"):
                if mode == "blanked":
                    window.setProperty("blanked", True)
                elif mode == "hidden":
                    window.hide()
                else:
                    window.showMinimized()
                QTest.qWait(180)
                check(not window.property("presentationActive"), mode + " disables root presentation")
                check(all(not p._active and p._timer.interval() == p._idle_ms for _, p in gated), mode + " slows presentation pollers")
                check(all(p._active and p._timer.isActive() and p._timer.interval() == p._ms for p in essential), mode + " retains essential safety telemetry")
                before_counts = counters.copy()
                breathe = page.property("breathe")
                QTest.qWait(1200)
                check(page.property("breathe") == breathe, mode + " pauses logo animation")
                gated_indexes = [i for i, (_, p) in enumerate(pollers) if any(p is target for _, target in gated)]
                essential_indexes = [i for i, (_, p) in enumerate(pollers) if any(p is target for target in essential)]
                details = dict(mode=mode, presentation_reads=sum(counters[i] - before_counts[i] for i in gated_indexes), essential_reads=sum(counters[i] - before_counts[i] for i in essential_indexes))
                check(details["presentation_reads"] == 0 and details["essential_reads"] >= 2, mode + " measured reduced work with essential reads continuing", details)
                lifecycle.append(details)
                if mode == "blanked":
                    window.setProperty("blanked", False)
                else:
                    window.showNormal()
                wait_for(lambda: window.property("presentationActive") and all(p._active for _, p in gated), description=mode + " resume")
                before_counts = counters.copy()
                QTest.qWait(180)
                check(all(p._timer.interval() == p._ms for _, p in gated), mode + " restores active polling intervals")
                first = page.property("breathe")
                QTest.qWait(180)
                check(page.property("breathe") != first, mode + " resumes logo animation")
            report["lifecycle"] = lifecycle
            screenshot("resumed")

            page = go("info")
            window.setProperty("blanked", True)
            old_name = value(page, "d")["name"]
            backend["nowPlaying"].pin(ui=[40, "Steam Fixture 40"])
            window.setProperty("page", "info")
            QTest.qWait(300)
            check("40" not in fixtures.network_calls and value(current_page(), "d")["name"] == old_name, "hidden Info avoids new metadata read")
            window.setProperty("blanked", False)
            wait_for(lambda: value(current_page(), "d").get("name") == "Steam Fixture 40", description="Info catches up immediately after resume")
            backend["nowPlaying"].pin(fixtures.event())
            go("now")
            backend["ui"].setAnimate(False)
            QTest.qWait(150)
            frozen = current_page().property("breathe")
            QTest.qWait(150)
            check(current_page().property("breathe") == frozen, "animation preference stops motion")
            backend["ui"].setAnimate(True)
            backend["ui"].setTheme("follow")
            wait_for(lambda: backend["artwork"].color != "", description="follow theme computes artwork color")
            screenshot("follow-theme")
            backend["ui"].setTheme("stock")
            for width, height in ((1240, 1080), (800, 600), (620, 540)):
                window.resize(width, height)
                page = go("system")
                check(page.width() > 0 and page.height() > 0, "usable page geometry at " + str((width, height)))
                screenshot(f"size-{width}x{height}")
            check(all(c["thread"] != threading.get_ident() for c in fixtures.calls if c["args"][0] in ("xprop", "wpctl", "nmcli", "tailscale")), "slow subprocess boundaries execute off GUI thread")
            check(not [item for item in fixtures.denied if item[-1] == "write"], "no attempted external filesystem writes")
            report["status"] = "passed"
        except Exception:
            report["status"] = "failed"
            report["error"] = traceback.format_exc()
            print(report["error"], file=sys.stderr, flush=True)
        finally:
            # Keep owners alive until outstanding production workers have published.
            window.hide()
            QTest.qWait(250)
            QGuiApplication.instance().quit()

    class CapturingEngine(QQmlApplicationEngine):
        def load(self, url):
            super().load(url)
            state["engine"] = self
            state["application"] = QGuiApplication.instance()
            if self.rootObjects():
                state["window"] = self.rootObjects()[0]
                state["backends"] = {name: self.rootContext().contextProperty(name) for name in BACKENDS}
                QTimer.singleShot(0, scenario)

    production.QQmlApplicationEngine = CapturingEngine
    sys.argv = [sys.argv[0], "--windowed"]
    try:
        code = production.main()
        if code:
            report["status"] = "failed"
            report["error"] = f"Production app exited with {code}"
    except Exception:
        report["status"] = "failed"
        report["error"] = traceback.format_exc()
    finally:
        if "engine" in state:
            import shiboken6
            for backend in state.get("backends", {}).values():
                for timer in backend.findChildren(QTimer):
                    timer.stop()
            QThreadPool.globalInstance().waitForDone(5000)
            QGuiApplication.processEvents()
            shiboken6.delete(state.pop("engine"))
            for backend in state.get("backends", {}).values():
                if shiboken6.isValid(backend):
                    shiboken6.delete(backend)
        # The optional PDF module diagnostic documents a supported production fallback.
        optional = [w for w in report["warnings"] if 'module "QtQuick.Pdf" is not installed' in w["message"]]
        ignored = [w for w in report["warnings"] if "This plugin does not support raise()" in w["message"]]
        relevant = [w for w in report["warnings"] if w not in optional and w not in ignored and ("Warning" in w["type"] or "Critical" in w["type"] or "Fatal" in w["type"])]
        report["expected_platform_diagnostics"] = optional + ignored
        report["app_relevant_warnings"] = relevant
        if relevant or any(not a["passed"] for a in report["assertions"]):
            report["status"] = "failed"
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["boundary_call_count"] = len(fixtures.calls)
        report["boundary_calls"] = fixtures.calls
        report["denied_external_operations"] = fixtures.denied
        report["steam_metadata_requests"] = fixtures.network_calls
        (output / "report.json").write_text(json.dumps(report, indent=2, default=str))
        print(json.dumps({"status": report["status"], "assertions": len(report["assertions"]), "routes": len(report["routes"]), "warnings": len(relevant), "report": str(output / "report.json")}), flush=True)
        qInstallMessageHandler(previous_handler)
        # Keep fixtures until interpreter exit: Qt-owned background work may still drain.
        fixture_tmp._finalizer.detach()
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
