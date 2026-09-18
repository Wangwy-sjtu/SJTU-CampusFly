"""Qt map surface with a Tencent JavaScript API/WebChannel path and an offline fallback."""

from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from src.route_model import clone_route, normalise_route, simplify_route
from src.qt_runtime import configure_qt_runtime
from src.utils import get_base_path


# Keep direct imports of map_view safe as well as the normal qtui launcher.
configure_qt_runtime()


try:  # The offline algorithm remains importable without Qt/WebEngine.
    from PySide6.QtCore import QObject, QPointF, QUrl, Qt, Signal, Slot
    from PySide6.QtGui import QColor, QPainter, QPen
    from PySide6.QtWidgets import QWidget
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEB_ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on minimal installs.
    WEB_ENGINE_AVAILABLE = False


if WEB_ENGINE_AVAILABLE:

    class _SilentAssetHandler(SimpleHTTPRequestHandler):
        """Serve the local map page without writing request URLs to stdout."""

        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - server noise
            return


    class _AssetServer:
        """Serve the map page from a localhost origin instead of file://."""

        def __init__(self, directory: str) -> None:
            handler = partial(_SilentAssetHandler, directory=directory)
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            # The map requests only a handful of small local files.  Daemon
            # request threads and a short accept timeout keep shutdown from
            # holding the Qt process open if WebEngine is closing a page.
            self.server.daemon_threads = True
            self.server.allow_reuse_address = True
            self.server.timeout = 0.25
            self._closed = False
            self._close_lock = threading.Lock()
            self._ready = threading.Event()
            self.thread = threading.Thread(
                target=self._serve,
                name="sjtu-map-assets",
                daemon=True,
            )
            self.thread.start()
            self._ready.wait(timeout=1.0)

        def _serve(self) -> None:
            self._ready.set()
            self.server.serve_forever(poll_interval=0.1)

        @property
        def url(self) -> str:
            return f"http://127.0.0.1:{self.server.server_port}/map.html"

        def close(self) -> None:
            with self._close_lock:
                if self._closed:
                    return
                self._closed = True
                self.server.shutdown()
                self.server.server_close()
            # Do not leave a named server thread behind when a top-level
            # window is closed.  A bounded join avoids making an abnormal
            # WebEngine shutdown hang the UI indefinitely.
            if self.thread.is_alive():
                self.thread.join(timeout=1.0)

    class MapBridge(QObject):
        routeChanged = Signal(str)
        ready = Signal()
        statusChanged = Signal(str)

        @Slot(str)
        def receiveRoute(self, route_json: str) -> None:
            self.routeChanged.emit(route_json)

        @Slot()
        def mapReady(self) -> None:
            self.ready.emit()

        @Slot(str)
        def reportStatus(self, status: str) -> None:
            self.statusChanged.emit(status)


    class RouteMapWidget(QWebEngineView):
        routeChanged = Signal(str)
        statusChanged = Signal(str)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._route: dict[str, Any] = normalise_route(None)
            self._road_graph: dict[str, Any] = {"edges": []}
            self._snap_mode = True
            self._tencent_road_overlay: dict[str, Any] = {"routes": []}
            self._map_credentials: dict[str, str] = {"key": ""}
            self._map_view: dict[str, Any] = {
                "latitude": 31.0281,
                "longitude": 121.4323,
                "zoom": 15,
                "bounds": {"south": 31.0182, "west": 121.4179, "north": 31.0384, "east": 121.4467},
            }
            self._page_ready = False
            self._closed = False
            self._drawing = False
            self._state_pushed = False
            self._offline_fallback_attempted = False
            try:
                self._asset_server: _AssetServer | None = _AssetServer(os.path.join(get_base_path(), "assets"))
            except OSError:
                # The local port is only a compatibility aid for Tencent's
                # SDK.  If it cannot be created, load the same page from its
                # file URL and keep offline editing available.
                self._asset_server = None
                self.statusChanged.emit("本地地图服务不可用，正在切换离线网格")
            self._bridge = MapBridge()
            self._bridge.routeChanged.connect(self._route_received)
            self._bridge.ready.connect(self._on_map_ready)
            self._bridge.statusChanged.connect(self.statusChanged)
            self._channel = QWebChannel(self.page())
            self._channel.registerObject("bridge", self._bridge)
            self.page().setWebChannel(self._channel)
            settings = self.settings()
            settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            self.loadFinished.connect(self._on_load_finished)
            self.loadStarted.connect(self._on_load_started)
            # A localhost origin keeps SDK loading and callback behavior
            # consistent across QtWebEngine profiles.
            try:
                if self._asset_server is not None:
                    self.setUrl(QUrl(self._asset_server.url))
                else:
                    self._load_local_page()
            except RuntimeError:
                self._load_local_page()

        def _load_local_page(self) -> None:
            """Load the editor without relying on the localhost asset server."""

            try:
                map_path = os.path.join(get_base_path(), "assets", "map.html")
                with open(map_path, "r", encoding="utf-8") as handle:
                    html = handle.read()
                self._offline_fallback_attempted = True
                self._state_pushed = False
                self.setHtml(html, QUrl.fromLocalFile(map_path))
            except (OSError, RuntimeError):
                self.statusChanged.emit("地图界面加载失败，请使用离线绘图")

        def _on_load_started(self) -> None:
            self._page_ready = False
            self._state_pushed = False

        def _on_load_finished(self, ok: bool) -> None:
            if self._closed:
                return
            if not ok:
                # A local HTTP origin is preferred because it keeps Tencent's
                # SDK callbacks consistent. If that origin is unavailable,
                # load the same page directly so the offline grid and route
                # editor remain usable instead of leaving a blank widget.
                if not self._offline_fallback_attempted:
                    self._offline_fallback_attempted = True
                    self._load_local_page()
                    self.statusChanged.emit("地图服务加载失败，正在切换离线网格")
                else:
                    self.statusChanged.emit("地图界面加载失败，请使用离线绘图")
                return
            self._page_ready = True
            if not self._state_pushed:
                self._push_state()

        def _on_map_ready(self) -> None:
            # loadFinished normally pushed the state already.  The bridge
            # callback is retained as a fallback for WebEngine builds that
            # report the document load before the page script is ready, but
            # must not replay the 1 MB candidate graph a second time.
            if self._closed:
                return
            self._page_ready = True
            if not self._state_pushed:
                self._push_state()

        def _route_received(self, route_json: str) -> None:
            if self._closed:
                return
            try:
                route = normalise_route(json.loads(route_json))
                # Pointer events can arrive at high frequency.  One-metre
                # decimation removes jitter without smoothing or corner cuts.
                route = simplify_route(route, min_spacing_m=1.0)
                self._route = route
                self.routeChanged.emit(json.dumps(route, ensure_ascii=False))
            except (TypeError, ValueError, KeyError) as exc:
                self.statusChanged.emit(f"路线数据无效: {exc}")

        def _run(self, function_name: str, argument: str | None = None) -> None:
            if not self._page_ready:
                return
            if argument is None:
                self.page().runJavaScript(f"window.{function_name}();")
            else:
                encoded = json.dumps(argument, ensure_ascii=False)
                self.page().runJavaScript(f"window.{function_name}({encoded});")

        def _push_state(self) -> None:
            if not self._page_ready:
                return
            self._run("setRoute", json.dumps(self._route, ensure_ascii=False))
            self._run("setRoadGraph", json.dumps(self._road_graph, ensure_ascii=False))
            self._run("setSnapMode", "true" if self._snap_mode else "false")
            self._run("setTencentRoadOverlay", json.dumps(self._tencent_road_overlay, ensure_ascii=False))
            self._run("configureTencent", json.dumps(self._map_credentials, ensure_ascii=False))
            self._run("setMapView", json.dumps(self._map_view, ensure_ascii=False))
            self._run("setDrawingMode", "true" if self._drawing else "false")
            self._state_pushed = True

        def set_route(self, route: dict[str, Any]) -> None:
            self._route = clone_route(route)
            self._run("setRoute", json.dumps(self._route, ensure_ascii=False))

        def route(self) -> dict[str, Any]:
            return clone_route(self._route)

        def set_map_credentials(self, key: str) -> None:
            self._map_credentials = {"key": key.strip()}
            self._run("configureTencent", json.dumps(self._map_credentials, ensure_ascii=False))

        def set_road_graph(self, graph: dict[str, Any]) -> None:
            # Provider response/provenance is retained by RoadGraph in Python.
            # The editor only needs topology and geometry, not query histories.
            compact = {
                "nodes": [{key: node[key] for key in ("id", "latitude", "longitude")} for node in graph.get("nodes", [])],
                "edges": [{key: edge[key] for key in ("id", "start", "end", "geometry", "length_m", "one_way") if key in edge} for edge in graph.get("edges", [])],
            }
            encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            self._road_graph = json.loads(encoded)
            self._run("setRoadGraph", encoded)

        def set_tencent_road_overlay(self, overlay: dict[str, Any]) -> None:
            """Display provider route geometry without changing the verified graph."""
            self._tencent_road_overlay = json.loads(json.dumps(overlay, ensure_ascii=False))
            self._run("setTencentRoadOverlay", json.dumps(self._tencent_road_overlay, ensure_ascii=False))

        def clear_tencent_road_overlay(self) -> None:
            self.set_tencent_road_overlay({"routes": []})

        def set_map_view(
            self,
            latitude: float,
            longitude: float,
            zoom: int = 15,
            *,
            bounds: dict[str, float] | None = None,
        ) -> None:
            self._map_view = {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "zoom": int(zoom),
                **({"bounds": json.loads(json.dumps(bounds, ensure_ascii=False))} if bounds else {}),
            }
            self._run(
                "setMapView",
                json.dumps(self._map_view, ensure_ascii=False),
            )

        def set_drawing_mode(self, enabled: bool) -> None:
            self._drawing = bool(enabled)
            self._run("setDrawingMode", "true" if enabled else "false")

        def set_snap_mode(self, enabled: bool) -> None:
            self._snap_mode = bool(enabled)
            self._run("setSnapMode", "true" if self._snap_mode else "false")

        def undo_stroke(self) -> None:
            self._run("undoStroke")

        def clear_route(self) -> None:
            self._run("clearRoute")

        def finish_stroke(self) -> None:
            self._run("finishStroke")

        def closeEvent(self, event) -> None:
            self.shutdown()
            super().closeEvent(event)

        def shutdown(self) -> None:
            """Stop page activity and release the local asset server once."""

            if self._closed:
                return
            self._closed = True
            self._page_ready = False
            try:
                self.stop()
            except RuntimeError:
                pass
            server = self._asset_server
            self._asset_server = None
            if server is not None:
                server.close()


else:

    from PySide6.QtCore import QPointF, Qt, Signal
    from PySide6.QtGui import QColor, QPainter, QPen
    from PySide6.QtWidgets import QWidget

    class RouteMapWidget(QWidget):  # pragma: no cover - requires a minimal Qt install.
        routeChanged = Signal(str)
        statusChanged = Signal(str)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._route = normalise_route(None)
            self._road_graph: dict[str, Any] = {"edges": []}
            self._snap_mode = True
            self._tencent_road_overlay: dict[str, Any] = {"routes": []}
            self._map_view: dict[str, Any] = {
                "latitude": 31.0281,
                "longitude": 121.4323,
                "zoom": 15,
                "bounds": {"south": 31.0182, "west": 121.4179, "north": 31.0384, "east": 121.4467},
            }
            self._drawing = False
            self._current: list[dict[str, float]] | None = None
            self._bounds = (121.438, 121.460, 31.024, 31.035)
            self.setMinimumSize(500, 420)

        def set_route(self, route: dict[str, Any]) -> None:
            self._route = clone_route(route)
            self._recompute_bounds()
            self.update()

        def route(self) -> dict[str, Any]:
            return clone_route(self._route)

        def set_map_credentials(self, key: str) -> None:
            self.statusChanged.emit("未安装 QtWebEngine，使用离线坐标网格；真实道路底图未加载")

        def set_road_graph(self, graph: dict[str, Any]) -> None:
            self._road_graph = json.loads(json.dumps(graph, ensure_ascii=False))

        def set_tencent_road_overlay(self, overlay: dict[str, Any]) -> None:
            self._tencent_road_overlay = json.loads(json.dumps(overlay, ensure_ascii=False))

        def clear_tencent_road_overlay(self) -> None:
            self.set_tencent_road_overlay({"routes": []})

        def set_map_view(
            self,
            latitude: float,
            longitude: float,
            zoom: int = 15,
            *,
            bounds: dict[str, float] | None = None,
        ) -> None:
            self._map_view = {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "zoom": int(zoom),
                **({"bounds": json.loads(json.dumps(bounds, ensure_ascii=False))} if bounds else {}),
            }

        def set_drawing_mode(self, enabled: bool) -> None:
            self._drawing = bool(enabled)
            if not self._drawing:
                self.finish_stroke()
            self.update()

        def set_snap_mode(self, enabled: bool) -> None:
            self._snap_mode = bool(enabled)
            self.update()

        def undo_stroke(self) -> None:
            if self._current:
                self._current = None
            elif self._route["strokes"]:
                self._route["strokes"].pop()
            self._emit_route()
            self.update()

        def clear_route(self) -> None:
            self._route["strokes"] = []
            self._current = None
            self._emit_route()
            self.update()

        def finish_stroke(self) -> None:
            if self._current:
                self._route["strokes"].append(self._current)
                self._current = None
                self._emit_route()
                self._recompute_bounds()
                self.update()

        def _emit_route(self) -> None:
            route = simplify_route(self._route, min_spacing_m=1.0)
            self._route = route
            self.routeChanged.emit(json.dumps(route, ensure_ascii=False))

        def _recompute_bounds(self) -> None:
            points = [point for stroke in self._route["strokes"] for point in stroke]
            if not points:
                return
            lons = [point["longitude"] for point in points]
            lats = [point["latitude"] for point in points]
            lon_pad = max(.001, (max(lons) - min(lons)) * .15)
            lat_pad = max(.001, (max(lats) - min(lats)) * .15)
            self._bounds = (min(lons) - lon_pad, max(lons) + lon_pad, min(lats) - lat_pad, max(lats) + lat_pad)

        def _point_from_pos(self, pos: QPointF) -> dict[str, float]:
            left, right, bottom, top = self._bounds
            x = max(0.0, min(float(self.width()), pos.x())) / max(1, self.width())
            y = max(0.0, min(float(self.height()), pos.y())) / max(1, self.height())
            return {"latitude": top - y * (top - bottom), "longitude": left + x * (right - left)}

        def _pos_from_point(self, point: dict[str, float]) -> QPointF:
            left, right, bottom, top = self._bounds
            return QPointF(
                (point["longitude"] - left) / (right - left) * self.width(),
                (top - point["latitude"]) / (top - bottom) * self.height(),
            )

        def mousePressEvent(self, event) -> None:
            if self._drawing and event.button() == Qt.LeftButton:
                self._current = [self._point_from_pos(event.position())]
                self.update()

        def mouseMoveEvent(self, event) -> None:
            if self._drawing and self._current:
                self._current.append(self._point_from_pos(event.position()))
                self.update()

        def mouseReleaseEvent(self, event) -> None:
            if self._drawing and event.button() == Qt.LeftButton:
                self.finish_stroke()

        def paintEvent(self, event) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#142c33"))
            painter.setPen(QPen(QColor("#36545a"), 1))
            for x in range(0, self.width(), 56): painter.drawLine(x, 0, x, self.height())
            for y in range(0, self.height(), 56): painter.drawLine(0, y, self.width(), y)
            painter.setRenderHint(QPainter.Antialiasing)
            for stroke in self._route["strokes"] + ([self._current] if self._current else []):
                if not stroke: continue
                painter.setPen(QPen(QColor("#f1745d"), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                for first, second in zip(stroke, stroke[1:]): painter.drawLine(self._pos_from_point(first), self._pos_from_point(second))
