"""Exercise the shared buttons in an offscreen Qt Quick window."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


QML = Path(__file__).resolve().parents[1] / "armadalive" / "qml"


class AccessibleButtons(unittest.TestCase):
    def test_focus_keyboard_pointer_and_accessible_activation(self):
        for button in ("PillButton", "CircleButton"):
            with self.subTest(button=button):
                result = subprocess.run(
                    [sys.executable, __file__, button],
                    env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), capture_output=True,
                    text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def exercise(button):
    from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QUrl
    from PySide6.QtGui import QAccessible, QGuiApplication, QKeyEvent
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest

    app = QGuiApplication([])
    engine = QQmlEngine()
    component = QQmlComponent(engine)
    component.setData(f"""
        import QtQuick
        Window {{
            id: window
            width: 300; height: 180; visible: true
            property int clicks: 0
            property int holds: 0
            {button} {{
                objectName: "button"
                x: 20; y: 20
                width: 100; height: 100
                label: "Keep"
                onClicked: window.clicks++
                {"onHeld: window.holds++" if button == "CircleButton" else ""}
            }}
        }}
    """.encode(), QUrl.fromLocalFile(str(QML / "ButtonTest.qml")))
    assert component.isReady(), component.errorString()
    window = component.create()
    actual = window.findChild(QQuickItem, "button")
    assert QTest.qWaitForWindowActive(window)
    assert not actual.hasActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Tab)
    assert actual.hasActiveFocus(), "Tab did not focus the button"

    accessible = QAccessible.queryAccessibleInterface(actual)
    assert accessible is not None
    assert accessible.role() == QAccessible.Role.Button
    assert accessible.text(QAccessible.Text.Name) == "Keep"
    action = accessible.actionInterface()
    assert action is not None
    press = action.pressAction()
    assert press in action.actionNames()

    for expected, key in enumerate((Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space), 1):
        QTest.keyPress(window, key)
        for _ in range(3):
            for kind in (QEvent.Type.KeyRelease, QEvent.Type.KeyPress):
                event = QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier, "", True)
                QCoreApplication.sendEvent(window, event)
        QTest.keyRelease(window, key)
        assert window.property("clicks") == expected, key

    point = QPoint(65, 65)
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
    assert window.property("clicks") == 4
    action.doAction(press)
    assert window.property("clicks") == 5

    if button == "CircleButton":
        QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=point)
        QTest.qWait(app.styleHints().mousePressAndHoldInterval() + 50)
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=point)
        assert window.property("holds") == 1
        assert window.property("clicks") == 5

    # A press interrupted by disabling must not activate, even after re-enabling.
    actual.forceActiveFocus()
    QTest.keyPress(window, Qt.Key.Key_Return)
    actual.setEnabled(False)
    actual.setEnabled(True)
    actual.forceActiveFocus()
    QTest.keyRelease(window, Qt.Key.Key_Return)
    assert window.property("clicks") == 5

    actual.setEnabled(False)
    QTest.keyClick(window, Qt.Key.Key_Tab)
    assert not actual.hasActiveFocus()
    QTest.keyClick(window, Qt.Key.Key_Space)
    QTest.keyClick(window, Qt.Key.Key_Return)
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
    action.doAction(press)
    assert window.property("clicks") == 5
    window.close()


if __name__ == "__main__":
    exercise(sys.argv[1])
