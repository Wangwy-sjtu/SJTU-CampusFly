"""Qt runtime settings used before importing QtWebEngine.

QtWebEngine creates its Chromium/GPU process during widget initialisation. On
some Windows graphics drivers that process exits before the main window can be
shown. Keep the workaround in one small, import-safe module so every launcher
uses the same software rendering policy.
"""

from __future__ import annotations

import os


_SOFTWARE_FLAGS = (
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-gpu-vsync",
    "--disable-gpu-rasterization",
)


def _append_missing_flags(current: str, required: tuple[str, ...]) -> str:
    values = current.split()
    for flag in required:
        if flag not in values:
            values.append(flag)
    return " ".join(values)


def configure_qt_runtime() -> None:
    """Configure Qt/Chromium before any PySide6 module is imported.

    The setting is deliberately limited to Windows, where the reported
    ``Failed to create shared context for virtualization`` crash occurs. An
    existing Chromium flag string is preserved and only missing safety flags
    are appended.
    """

    if os.name != "nt":
        return
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _append_missing_flags(existing, _SOFTWARE_FLAGS)


configure_qt_runtime()
