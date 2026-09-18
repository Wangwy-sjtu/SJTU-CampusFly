"""SJTU route desk: map-first PySide6 editor and upload shell."""

from __future__ import annotations

import copy
import json
import math
import os
import sys

# This must run before importing PySide6.QtWebEngine through map_view.
from src.qt_runtime import configure_qt_runtime

configure_qt_runtime()

from PySide6.QtCore import QDateTime, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

# QApplication attributes must be set before the first QApplication instance;
# doing this at import time also covers tests and alternate Python launchers
# that import this module before constructing the application.
try:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
except AttributeError:  # Qt 6.8 keeps the legacy enum spelling only.
    QApplication.setAttribute(Qt.AA_UseSoftwareOpenGL, True)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from src.config_manager import (
    DEFAULT_CONFIG_FILE_NAME,
    ConfigError,
    ConfigManager,
)
from src.help_dialog import HelpDialog
from src.map_view import RouteMapWidget
from src.main import run_sports_upload
from src.route_model import (
    clone_route,
    load_route_file,
    normalise_route,
    route_metrics,
    save_route_file,
    validate_stroke_continuity,
)
from src.road_graph import RoadGraph, RoadGraphError
from src.tencent_roads import TencentRoadError, overlay_route_names, request_tencent_walking_routes
from src.utils import ValidationError, get_base_path, get_user_data_path, redact_secrets, validate_run_parameters, validate_route


ASSET_DIR = os.path.join(get_base_path(), "assets")
MINHANG_VIEW_BOUNDS = {
    "south": 31.0182,
    "west": 121.4179,
    "north": 31.0384,
    "east": 121.4467,
}


def _road_graph_options() -> list[tuple[str, str]]:
    """Return the Tencent sampled graph used by the editor."""

    base_path = get_base_path()
    candidate = os.path.join(base_path, "data", "tencent_road_graph.candidate.json")
    return [("腾讯步行采样路网（局部，需核验）", candidate)] if os.path.exists(candidate) else []


class WorkerThread(QThread):
    progress_update = Signal(int, int, str)
    log_output = Signal(str, str)
    # QThread already owns a no-argument ``finished`` signal.  Giving the
    # worker result a different name keeps Qt's lifecycle signal available for
    # deleteLater() and avoids destroying a still-running thread on shutdown.
    completed = Signal(bool, str)

    def __init__(self, config_data: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config_data = copy.deepcopy(config_data)

    def run(self) -> None:
        try:
            success, message = run_sports_upload(
                self.config_data,
                progress_callback=lambda current, total, message: self.progress_update.emit(current, total, message),
                log_cb=lambda message, level: self.log_output.emit(message, level),
                stop_check_cb=self.isInterruptionRequested,
            )
        except Exception as exc:  # Keep unexpected worker errors on the UI channel.
            self.log_output.emit(f"任务异常: {redact_secrets(exc)}", "error")
            success, message = False, "任务异常，详见日志。"
        if self.isInterruptionRequested() and not success:
            message = "任务已手动终止。"
        self.completed.emit(success, message)


class TencentRoadThread(QThread):
    """Fetch one two-point Tencent walking plan without blocking the editor."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, key: str, from_point: dict, to_point: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._from_point = copy.deepcopy(from_point)
        self._to_point = copy.deepcopy(to_point)

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        try:
            # A route lookup is an optional overlay.  Keep its upper bound
            # short so closing the editor never waits through a long network
            # retry, and discard the result when shutdown was requested.
            overlay = request_tencent_walking_routes(
                self._key, self._from_point, self._to_point, timeout=8.0
            )
        except TencentRoadError as exc:
            self.failed.emit(str(exc))
        except Exception:
            # Do not surface an exception string: a requests exception can
            # include a URL with the private key in it.
            self.failed.emit("腾讯步行路线请求失败，请检查网络或 Key 权限。")
        else:
            if self.isInterruptionRequested():
                return
            self.succeeded.emit(overlay)


class Section(QGroupBox):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setObjectName("section")


class SportsUploaderUI(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SJTU校园飞")
        icon_path = os.path.join(ASSET_DIR, "campusfly.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1440, 900)
        self.setMinimumSize(1080, 700)
        self.thread: WorkerThread | None = None
        self.route = normalise_route(None)
        self.config: dict = {}
        self.current_config_filename = DEFAULT_CONFIG_FILE_NAME
        self._drawing = False
        self._route_mode = "snap"
        self._map_provider = "offline"
        self._route_guard = False
        self._last_good_route = clone_route(self.route)
        self._selected_edge_ids: list[str] = []
        self._selected_start_node: str | None = None
        self._selected_current_node: str | None = None
        self._tencent_road_thread: TencentRoadThread | None = None
        self._tencent_overlay: dict = {"routes": []}
        self._close_pending = False
        self._allow_close = False
        self.road_graph: RoadGraph | None = None
        try:
            self.road_graph = RoadGraph.from_file(os.path.join(get_base_path(), "data", "tencent_road_graph.candidate.json"))
        except (OSError, ValueError, RoadGraphError) as exc:
            self._graph_load_error = str(exc)
        else:
            self._graph_load_error = ""

        self._apply_style()
        self._build_ui()
        self._credentials_dirty = False
        self._credentials_timer = QTimer(self)
        self._credentials_timer.setSingleShot(True)
        self._credentials_timer.setInterval(500)
        self._credentials_timer.timeout.connect(self._persist_credentials)
        for field in (self.user_id_input, self.keepalive_input, self.jsessionid_input, self.tencent_key_input):
            field.textEdited.connect(self._credentials_edited)
            field.editingFinished.connect(self._persist_credentials)
        self.load_settings_to_ui(DEFAULT_CONFIG_FILE_NAME)

    def _apply_style(self) -> None:
        self.setStyleSheet(r"""
            QWidget { color: #e6eee9; background: #12252d; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 13px; }
            QFrame#mapFrame, QGroupBox#section { background: #173038; border: 1px solid #31545a; border-radius: 7px; }
            QGroupBox#section { margin-top: 12px; padding: 14px 10px 10px; }
            QGroupBox#section::title { subcontrol-origin: margin; left: 13px; padding: 0 6px; color: #f7c9a8; background: #12252d; font-weight: 600; letter-spacing: .04em; }
            QLabel#eyebrow { color: #91b0aa; font-size: 11px; font-weight: 600; letter-spacing: .13em; }
            QLabel#heroTitle { color: #f6f0df; font-size: 24px; font-weight: 700; }
            QLabel#mapStatus { color: #f7c9a8; background: #1a3941; border: 1px solid #41666a; padding: 7px 9px; border-radius: 4px; }
            QLabel#metricValue { color: #f6c898; font-family: Consolas, monospace; font-size: 20px; font-weight: 700; }
            QLabel#metricLabel { color: #8ea9a4; font-size: 11px; }
            QLabel#note { color: #9ab1ad; line-height: 1.4; }
            QLineEdit, QDateTimeEdit, QComboBox { color: #e6eee9; background: #10242b; border: 1px solid #3a6065; border-radius: 4px; padding: 7px; selection-background-color: #bd5d50; }
            QLineEdit:focus, QDateTimeEdit:focus, QComboBox:focus { border: 1px solid #f1745d; }
            QPushButton { color: #f7f0e4; background: #28535a; border: 1px solid #4a7473; border-radius: 4px; padding: 8px 13px; font-weight: 600; }
            QPushButton:hover { background: #34676c; }
            QPushButton:pressed { background: #1d444c; }
            QPushButton:disabled { color: #76908d; background: #1a3036; border-color: #2b454a; }
            QPushButton#drawButton { color: #fff3df; background: #bd5d50; border-color: #dd7963; }
            QPushButton#drawButton:checked { background: #8c403d; }
            QPushButton#uploadButton { color: #10242b; background: #f6c898; border-color: #f6c898; }
            QPushButton#uploadButton:hover { background: #ffd9ae; }
            QPushButton#stopButton { background: #713d40; border-color: #9b5754; }
            QPlainTextEdit { color: #c9d8d2; background: #0f2026; border: 1px solid #31545a; border-radius: 4px; font-family: Consolas, monospace; font-size: 11px; }
            QProgressBar { color: #f6f0df; background: #10242b; border: 1px solid #36575d; border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background: #bd5d50; border-radius: 3px; }
            QScrollArea { border: none; background: transparent; }
            QCheckBox { spacing: 7px; }
            QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #527478; border-radius: 3px; background: #10242b; }
            QCheckBox::indicator:checked { background: #bd5d50; border-color: #f1745d; }
        """)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        eyebrow = QLabel("SJTU校园飞 / MINHANG")
        eyebrow.setObjectName("eyebrow")
        root.addWidget(eyebrow)
        title = QLabel("把路线画在校园里")
        title.setObjectName("heroTitle")
        root.addWidget(title)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        map_frame = QFrame()
        map_frame.setObjectName("mapFrame")
        map_layout = QVBoxLayout(map_frame)
        map_layout.setContentsMargins(12, 12, 12, 12)
        map_layout.setSpacing(9)
        map_header = QHBoxLayout()
        map_header.addWidget(QLabel("二维路线画布"))
        self.map_status = QLabel("路线载入中…")
        self.map_status.setObjectName("mapStatus")
        map_header.addWidget(self.map_status, 1)
        map_layout.addLayout(map_header)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("编辑模式"))
        self.route_mode_combo = QComboBox()
        self.route_mode_combo.addItem("道路吸附画笔", "snap")
        self.route_mode_combo.addItem("自由手绘（兼容）", "free")
        self.route_mode_combo.currentIndexChanged.connect(self.change_route_mode)
        toolbar.addWidget(self.route_mode_combo)
        toolbar.addWidget(QLabel("路网来源"))
        self.road_graph_combo = QComboBox()
        for label, path in _road_graph_options():
            self.road_graph_combo.addItem(label, path)
        self.road_graph_combo.currentIndexChanged.connect(self.change_road_graph)
        toolbar.addWidget(self.road_graph_combo)
        self.draw_button = QPushButton("开始手绘")
        self.draw_button.setObjectName("drawButton")
        self.draw_button.setCheckable(True)
        self.draw_button.clicked.connect(self.toggle_drawing)
        toolbar.addWidget(self.draw_button)
        self.finish_button = QPushButton("结束本笔")
        self.finish_button.clicked.connect(self.finish_stroke)
        toolbar.addWidget(self.finish_button)
        self.undo_button = QPushButton("撤销上一笔")
        self.undo_button.clicked.connect(self.undo_stroke)
        toolbar.addWidget(self.undo_button)
        self.clear_button = QPushButton("清空路线")
        self.clear_button.clicked.connect(self.clear_route)
        toolbar.addWidget(self.clear_button)
        toolbar.addStretch(1)
        map_layout.addLayout(toolbar)
        self.map_view = RouteMapWidget()
        self.map_view.routeChanged.connect(self.route_changed)
        self.map_view.statusChanged.connect(self._on_map_status)
        if self.road_graph is not None:
            self.map_view.set_road_graph(self.road_graph.to_dict())
        self.map_view.set_snap_mode(True)
        map_layout.addWidget(self.map_view, 1)
        map_note = QLabel("道路画笔吸附到腾讯步行采样路网的真实节点；拖动时会高亮候选节点并预览已声明的连接路径，不可达时保留已有路线。腾讯候选覆盖有限，未标注道路不会被自动补齐。无 Key 时仅显示坐标网格。")
        map_note.setObjectName("note")
        map_note.setWordWrap(True)
        map_layout.addWidget(map_note)
        splitter.addWidget(map_frame)

        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(4, 0, 4, 0)
        side_layout.setSpacing(10)
        side_scroll.setWidget(side)
        splitter.addWidget(side_scroll)
        splitter.setSizes([920, 410])

        route_section = Section("路线快照")
        route_layout = QVBoxLayout(route_section)
        metrics_row = QHBoxLayout()
        self.distance_value, self.duration_value = QLabel("—"), QLabel("—")
        self.distance_value.setObjectName("metricValue"); self.duration_value.setObjectName("metricValue")
        for value, label in ((self.distance_value, "距离"), (self.duration_value, "预计时长")):
            box = QVBoxLayout(); box.addWidget(value); small = QLabel(label); small.setObjectName("metricLabel"); box.addWidget(small); metrics_row.addLayout(box)
        route_layout.addLayout(metrics_row)
        self.route_meta = QLabel("坐标系：—\n起点：—\n终点：—")
        self.route_meta.setObjectName("note")
        route_layout.addWidget(self.route_meta)
        route_buttons = QHBoxLayout()
        self.save_route_button = QPushButton("保存路线")
        self.save_route_button.clicked.connect(self.save_route_dialog)
        self.load_route_button = QPushButton("载入路线")
        self.load_route_button.clicked.connect(self.load_route_dialog)
        route_buttons.addWidget(self.save_route_button); route_buttons.addWidget(self.load_route_button)
        route_layout.addLayout(route_buttons)
        side_layout.addWidget(route_section)

        planning = Section("路网选路与距离规划")
        planning_form = QFormLayout(planning)
        self.lap_input = QSpinBox(); self.lap_input.setRange(1, 99); self.lap_input.setValue(1)
        planning_form.addRow("闭合路线圈数", self.lap_input)
        self.target_distance_input = QLineEdit(); self.target_distance_input.setPlaceholderText("开放路线目标公里数，例如 3.0")
        planning_form.addRow("目标距离（km）", self.target_distance_input)
        plan_row = QHBoxLayout()
        self.plan_distance_button = QPushButton("按目标距离规划")
        self.plan_distance_button.clicked.connect(self.plan_target_distance)
        self.apply_laps_button = QPushButton("应用圈数")
        self.apply_laps_button.clicked.connect(self.apply_laps)
        plan_row.addWidget(self.plan_distance_button); plan_row.addWidget(self.apply_laps_button)
        planning_form.addRow("", plan_row)
        planning_note = QLabel("节点吸附画笔的首个已选节点或当前路线起点用于规划；默认规划开放路线，终点由实际可达路网决定。圈数只接受已闭合基础路线，不会静默补边。")
        planning_note.setObjectName("note"); planning_note.setWordWrap(True)
        planning_form.addRow("", planning_note)
        side_layout.addWidget(planning)

        params = Section("参数")
        params_form = QFormLayout(params)
        self.speed_input = QLineEdit(); self.speed_input.setPlaceholderText("例如 2.5 m/s")
        self.interval_input = QLineEdit(); self.interval_input.setPlaceholderText("例如 3 秒")
        self.speed_input.editingFinished.connect(self.update_metrics)
        params_form.addRow("速度", self.speed_input)
        params_form.addRow("采样间隔", self.interval_input)
        self.use_current_time = QCheckBox("使用点击上传时刻")
        self.use_current_time.setChecked(True)
        self.use_current_time.toggled.connect(self.toggle_time_input)
        params_form.addRow("开始时间", self.use_current_time)
        self.start_datetime = QDateTimeEdit(QDateTime.currentDateTime())
        self.start_datetime.setCalendarPopup(True)
        self.start_datetime.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.start_datetime.setEnabled(False)
        params_form.addRow("历史时间", self.start_datetime)
        side_layout.addWidget(params)

        connection = Section("连接与地图")
        connection_form = QFormLayout(connection)
        self.mode_combo = QComboBox(); self.mode_combo.addItem("离线模拟（推荐）", "mock"); self.mode_combo.addItem("真实接口（需凭据）", "real")
        connection_form.addRow("上传模式", self.mode_combo)
        self.user_id_input = QLineEdit(); self.user_id_input.setPlaceholderText("真实接口才需要")
        connection_form.addRow("用户 ID", self.user_id_input)
        self.keepalive_input = QLineEdit(); self.keepalive_input.setEchoMode(QLineEdit.Password); self.keepalive_input.setPlaceholderText("keepalive 值")
        self.jsessionid_input = QLineEdit(); self.jsessionid_input.setEchoMode(QLineEdit.Password); self.jsessionid_input.setPlaceholderText("JSESSIONID 值")
        connection_form.addRow("Keepalive", self.keepalive_input); connection_form.addRow("JSESSIONID", self.jsessionid_input)
        pe_link = QLabel('<a href="http://pe.sjtu.edu.cn/phone/#/indexPortrait" style="color: #f6c898;">打开交大体育网页 ↗</a>')
        pe_link.setOpenExternalLinks(True)
        pe_link.setToolTip("在默认浏览器中打开，方便查看并获取 Keepalive / JSESSIONID")
        connection_form.addRow("", pe_link)
        credentials_note = QLabel("账户和地图 Key 自动保存在本机，下次打开恢复。")
        credentials_note.setWordWrap(True)
        connection_form.addRow("", credentials_note)
        self.tencent_key_input = QLineEdit(); self.tencent_key_input.setEchoMode(QLineEdit.Password); self.tencent_key_input.setPlaceholderText("可选：腾讯地图 JavaScript API V2 Key")
        connection_form.addRow("腾讯地图 Key", self.tencent_key_input)
        tencent_link = QLabel('<a href="https://lbs.qq.com/" style="color: #f6c898;">获取腾讯地图 Key · lbs.qq.com ↗</a>')
        tencent_link.setOpenExternalLinks(True)
        tencent_link.setToolTip("打开腾讯位置服务官网，注册或登录后创建 Key")
        connection_form.addRow("", tencent_link)
        apply_map = QPushButton("应用地图凭据")
        apply_map.clicked.connect(self.apply_map_credentials)
        connection_form.addRow("", apply_map)
        self.tencent_fetch_button = QPushButton("查询并标记腾讯步行道路")
        self.tencent_fetch_button.clicked.connect(self.fetch_tencent_walking_route)
        connection_form.addRow("", self.tencent_fetch_button)
        self.tencent_import_button = QPushButton("采用首选腾讯路线")
        self.tencent_import_button.setEnabled(False)
        self.tencent_import_button.clicked.connect(self.import_tencent_walking_route)
        connection_form.addRow("", self.tencent_import_button)
        connection_note = QLabel("腾讯 Key 只用于地图加载和两点步行路线请求；返回几何会以黄色叠加线标记，不能代表完整校园路网。服务器坐标系仍未核实。")
        connection_note.setObjectName("note"); connection_note.setWordWrap(True)
        connection_form.addRow("", connection_note)
        side_layout.addWidget(connection)

        config_buttons = QHBoxLayout()
        self.load_config_button = QPushButton("加载配置")
        self.load_config_button.clicked.connect(lambda: self.load_settings_to_ui(DEFAULT_CONFIG_FILE_NAME))
        self.save_config_button = QPushButton("保存配置")
        self.save_config_button.clicked.connect(lambda: self.save_current_settings(self.current_config_filename))
        config_buttons.addWidget(self.load_config_button); config_buttons.addWidget(self.save_config_button)
        side_layout.addLayout(config_buttons)

        action_row = QHBoxLayout()
        self.upload_button = QPushButton("开始上传")
        self.upload_button.setObjectName("uploadButton")
        self.upload_button.clicked.connect(self.start_upload)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_upload)
        self.help_button = QPushButton("帮助")
        self.help_button.clicked.connect(self.show_help_dialog)
        action_row.addWidget(self.upload_button, 2); action_row.addWidget(self.stop_button); action_row.addWidget(self.help_button)
        side_layout.addLayout(action_row)
        self.progress = QProgressBar(); self.progress.setValue(0); side_layout.addWidget(self.progress)
        self.status_label = QLabel("状态：待命")
        side_layout.addWidget(self.status_label)
        self.log_output_area = QPlainTextEdit(); self.log_output_area.setReadOnly(True); self.log_output_area.setMinimumHeight(160); side_layout.addWidget(self.log_output_area)
        self.log_output_area.setMaximumBlockCount(2000)
        self.log_output_area.setUndoRedoEnabled(False)
        side_layout.addStretch(1)

    def route_changed(self, route_json: str) -> None:
        try:
            incoming = normalise_route(json.loads(route_json))
            if self._route_guard:
                self.route = incoming
                self.update_metrics()
                return
            incoming_graph = incoming.get("road_graph") if isinstance(incoming.get("road_graph"), dict) else {}
            if (
                self._route_mode == "snap"
                and self.road_graph is not None
                and incoming_graph.get("mode") == "snap_nodes"
            ):
                raw_edges = incoming_graph.get("edge_ids", [])
                if not isinstance(raw_edges, list):
                    raise RoadGraphError("节点吸附路线缺少有效边序列。")
                edge_ids = [str(edge_id) for edge_id in raw_edges]
                start_node = incoming_graph.get("start_node_id")
                end_node = incoming_graph.get("end_node_id")
                if start_node is not None and str(start_node) not in self.road_graph.nodes:
                    raise RoadGraphError("节点吸附路线起点不在当前腾讯路网中。")
                if end_node is not None and str(end_node) not in self.road_graph.nodes:
                    raise RoadGraphError("节点吸附路线终点不在当前腾讯路网中。")
                if edge_ids:
                    if start_node is None:
                        raise RoadGraphError("节点吸附路线缺少起点节点。")
                    canonical_points = self.road_graph.path_points(edge_ids, start_node_id=str(start_node))
                    if not canonical_points:
                        raise RoadGraphError("节点吸附路线没有有效几何。")
                    incoming["strokes"] = [canonical_points]
                    end_node = end_node or self.road_graph.edges[edge_ids[-1]]["end"]
                # A legacy snap snapshot may carry geometry without edge IDs.
                # Keep that geometry visible while the user establishes a new
                # node-snap start; an empty route remains empty.
                incoming["road_graph"] = {
                    **incoming_graph,
                    "edge_ids": edge_ids,
                    "start_node_id": str(start_node) if start_node is not None else None,
                    "end_node_id": str(end_node) if end_node is not None else None,
                    "mode": "snap_nodes",
                    **self._road_graph_provenance(),
                }
                self._selected_edge_ids = edge_ids
                self._selected_start_node = incoming["road_graph"]["start_node_id"]
                self._selected_current_node = incoming["road_graph"]["end_node_id"]
            elif self._route_mode == "snap" and self.road_graph is not None and incoming["strokes"]:
                if self._map_provider != "tencent":
                    raise RoadGraphError("道路吸附需要腾讯真实底图；当前是离线坐标网格，请先加载 Key 或切换自由手绘。")
                snapped_strokes: list[list[dict[str, float]]] = []
                edge_ids: list[str] = []
                for stroke in incoming["strokes"]:
                    snapped, stroke_edges = self.road_graph.snap_stroke(stroke, max_distance_m=35.0)
                    snapped_strokes.append(snapped)
                    for edge_id in stroke_edges:
                        if not edge_ids or edge_id != edge_ids[-1]:
                            edge_ids.append(edge_id)
                incoming["strokes"] = snapped_strokes
                incoming["road_graph"] = {
                    "edge_ids": edge_ids,
                    "mode": "snap",
                    **self._road_graph_provenance(),
                }
                self._route_guard = True
                try:
                    self.map_view.set_route(incoming)
                finally:
                    self._route_guard = False
            elif self._route_mode == "free":
                incoming.pop("road_graph", None)
            self.route = incoming
            self._last_good_route = clone_route(incoming)
            self.update_metrics()
        except (ValueError, TypeError, KeyError, RoadGraphError) as exc:
            self.log_output_text(f"路线更新失败：{exc}", "error")
            if self._route_mode == "snap" and self.road_graph is not None:
                self._route_guard = True
                try:
                    self.map_view.set_route(self._last_good_route)
                finally:
                    self._route_guard = False

    def change_route_mode(self) -> None:
        self._route_mode = str(self.route_mode_combo.currentData() or "snap")
        if self._route_mode == "snap":
            self.map_view.set_snap_mode(True)
            self.map_status.setText("节点吸附画笔 · 拖动到节点后沿腾讯真实连接预览")
        else:
            self.map_view.set_snap_mode(False)
            self.map_status.setText("自由手绘兼容模式 · 不自动吸附道路")

    def change_road_graph(self) -> None:
        """Switch the optional graph used by snap and planning."""

        path = self.road_graph_combo.currentData()
        if not path:
            return
        previous_index = getattr(self, "_road_graph_index", 0)
        try:
            graph = RoadGraph.from_file(str(path))
        except (OSError, ValueError, RoadGraphError) as exc:
            self.road_graph_combo.blockSignals(True)
            self.road_graph_combo.setCurrentIndex(previous_index)
            self.road_graph_combo.blockSignals(False)
            self.log_output_text(f"路网载入失败：{exc}", "error")
            self.map_status.setText("路网载入失败，保留当前路网")
            return
        self._road_graph_index = self.road_graph_combo.currentIndex()
        self.road_graph = graph
        self._graph_load_error = ""
        self.map_view.set_road_graph(graph.to_dict())
        # Edge ids and endpoint nodes belong to one graph. Clear any selected
        # route before switching so a candidate graph cannot inherit stale ids.
        self.clear_route()
        label = self.road_graph_combo.currentText()
        status = f"已切换路网 · {label} · {len(graph.edges)} 条边"
        if graph.metadata.get("verification_status") == "provider_route_partial":
            status += " · 仅候选覆盖"
        self.map_status.setText(status)
        self.log_output_text(status, "info")

    @staticmethod
    def _point_distance(first: dict, second: dict) -> float:
        from src.utils import haversine_distance

        return haversine_distance(first["latitude"], first["longitude"], second["latitude"], second["longitude"])

    def _road_graph_provenance(self) -> dict[str, object]:
        """Keep a compact, non-secret graph identity in saved route snapshots."""

        if self.road_graph is None:
            return {"verification_status": "unknown"}
        metadata = self.road_graph.metadata
        result: dict[str, object] = {
            "verification_status": metadata.get("verification_status", "unknown"),
        }
        for field in ("graph_id", "provider", "collected_at", "query_count"):
            if metadata.get(field) is not None:
                result[field] = metadata[field]
        return result

    def _planning_start_node(self) -> str | None:
        """Resolve a safe graph start from snap state or the current route."""

        if self.road_graph is None:
            return None
        if self._selected_start_node is not None:
            selected = str(self._selected_start_node)
            if selected in self.road_graph.nodes:
                self._selected_start_node = selected
                return selected

        graph_meta = self.route.get("road_graph") if isinstance(self.route, dict) else None
        if isinstance(graph_meta, dict):
            saved = graph_meta.get("start_node_id")
            if saved is not None and str(saved) in self.road_graph.nodes:
                self._selected_start_node = str(saved)
                return self._selected_start_node

        strokes = self.route.get("strokes") if isinstance(self.route, dict) else None
        points = [point for stroke in (strokes or []) if stroke for point in stroke]
        if not points:
            return None
        start_point = points[0]
        candidates = sorted(
            (
                self._point_distance(start_point, node),
                node_id,
            )
            for node_id, node in self.road_graph.nodes.items()
        )
        if not candidates or candidates[0][0] > 35.0:
            return None
        if len(candidates) > 1 and candidates[1][0] - candidates[0][0] <= 4.0:
            return None
        self._selected_start_node = candidates[0][1]
        return self._selected_start_node

    def update_metrics(self) -> None:
        try:
            speed, _ = validate_run_parameters(self.speed_input.text(), self.interval_input.text())
            metrics = route_metrics(self.route, speed)
            self.distance_value.setText(f"{metrics['distance_m']:.0f} m")
            self.duration_value.setText(self._format_duration(metrics["duration_s"]))
            start, end = metrics["start"], metrics["end"]
            system = self.route.get("coordinate_system", "unknown")
            self.route_meta.setText(
                f"坐标系：{system}（服务器坐标系未知）\n"
                f"笔画：{metrics['stroke_count']} · 起点：{self._format_point(start)}\n"
                f"终点：{self._format_point(end)}"
            )
        except (ValidationError, TypeError, ValueError):
            self.distance_value.setText("—"); self.duration_value.setText("—")

    def _on_map_status(self, status: str) -> None:
        self._map_provider = "tencent" if str(status).startswith("腾讯地图") else "offline"
        self.map_status.setText(status)

    @staticmethod
    def _format_point(point: dict | None) -> str:
        if not point: return "—"
        return f"{point['latitude']:.6f}, {point['longitude']:.6f}"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds == float("inf") or seconds < 0: return "—"
        total = int(round(seconds)); return f"{total // 60:02d}:{total % 60:02d}"

    def toggle_drawing(self, checked: bool) -> None:
        self._drawing = bool(checked)
        self.draw_button.setText("结束画笔" if checked else "开始画笔")
        self.map_view.set_drawing_mode(checked)
        self.finish_button.setEnabled(checked)

    def finish_stroke(self) -> None:
        if hasattr(self.map_view, "finish_stroke"):
            self.map_view.finish_stroke()

    def undo_stroke(self) -> None:
        self.map_view.undo_stroke()

    def clear_route(self) -> None:
        self.map_view.clear_route()
        self.map_view.clear_tencent_road_overlay()
        self._tencent_overlay = {"routes": []}
        self.tencent_import_button.setEnabled(False)
        self.route = normalise_route(None)
        self.route["strokes"] = []
        self._selected_edge_ids = []
        self._selected_start_node = None
        self._selected_current_node = None
        self._last_good_route = clone_route(self.route)
        self.update_metrics()

    def plan_target_distance(self) -> None:
        if self.road_graph is None:
            self.log_output_text("当前没有可用的人工核验路网。", "error")
            return
        if self._map_provider != "tencent":
            self.log_output_text("目标距离规划需要腾讯真实底图；当前是离线坐标网格。", "warning")
            self.map_status.setText("目标距离规划需要腾讯真实底图")
            return
        start_node = self._planning_start_node()
        if start_node is None:
            self.log_output_text("请先用道路吸附画笔选中首个节点，或载入包含路网起点的路线，再规划目标距离。", "warning")
            self.map_status.setText("请先选中节点或载入已有路线起点")
            return
        try:
            raw = self.target_distance_input.text().strip()
            target_km = float(raw)
            if not (math.isfinite(target_km) and target_km > 0):
                raise ValueError
            result = self.road_graph.path_for_target_distance(start_node, target_km * 1000.0, return_to_start=False)
            self._selected_edge_ids = list(result["edge_ids"])
            self._selected_current_node = result["node_ids"][-1]
            route_points = self.road_graph.path_points(self._selected_edge_ids, start_node_id=start_node)
            self.route = normalise_route({
                "version": 1,
                "campus": "sjtu_minhang",
                "coordinate_system": self.road_graph.metadata.get("coordinate_system", "unknown"),
                "provider": "tencent",
                "strokes": [route_points],
                "road_graph": {
                    "edge_ids": list(self._selected_edge_ids),
                    "start_node_id": start_node,
                    "end_node_id": self._selected_current_node,
                    "mode": "plan",
                    **self._road_graph_provenance(),
                },
            })
            self._route_guard = True
            try:
                self.map_view.set_route(self.route)
            finally:
                self._route_guard = False
            self._last_good_route = clone_route(self.route)
            self.update_metrics()
            actual_km = result["distance_m"] / 1000.0
            error_km = result["error_m"] / 1000.0
            detail = f"规划完成 · 目标 {target_km:.2f} km · 实际 {actual_km:.2f} km · 误差 {error_km:+.2f} km"
            if abs(result["error_m"]) > max(250.0, target_km * 1000.0 * 0.20):
                detail += " · 误差较大，请手动调整"
            self.map_status.setText(detail)
        except (ValueError, TypeError, RoadGraphError):
            self.log_output_text("目标距离必须是正的有限公里数，且当前骨架必须存在可达路线。", "error")

    def apply_laps(self) -> None:
        if self.road_graph is None:
            self.log_output_text("当前没有可用的人工核验路网。", "error")
            return
        if self._map_provider != "tencent":
            self.log_output_text("圈数展开需要腾讯真实底图；当前是离线坐标网格。", "warning")
            self.map_status.setText("圈数展开需要腾讯真实底图")
            return
        if not self._selected_edge_ids:
            self.log_output_text("请先用道路吸附画笔建立闭合基础路线。", "warning")
            return
        try:
            laps = int(self.lap_input.value())
            start_node = self._planning_start_node()
            if start_node is None:
                raise RoadGraphError("当前路线没有可用的路网起点，请先从节点开始吸附。")
            expanded = self.road_graph.expand_laps(
                self._selected_edge_ids,
                laps,
                start_node_id=start_node,
            )
            points = self.road_graph.path_points(expanded, start_node_id=start_node)
            self._selected_edge_ids = expanded
            self.route = normalise_route({
                **self.route,
                "strokes": [points],
                "road_graph": {
                    **(self.route.get("road_graph") or {}),
                    "edge_ids": expanded,
                    "laps": laps,
                    "mode": "laps",
                },
            })
            self._route_guard = True
            try:
                self.map_view.set_route(self.route)
            finally:
                self._route_guard = False
            self._last_good_route = clone_route(self.route)
            self.update_metrics()
            self.map_status.setText(f"已展开 {laps} 圈完整路线 · 距离按实际边长累计")
        except RoadGraphError as exc:
            self.map_status.setText(str(exc))
            self.log_output_text(str(exc), "warning")

    def apply_map_credentials(self) -> None:
        self._persist_credentials()
        self.map_view.set_map_credentials(self.tencent_key_input.text())
        self.log_output_text("已更新地图加载设置；底图是否覆盖校内道路需现场核验。", "info")

    def _tencent_request_endpoints(self) -> tuple[dict[str, float], dict[str, float]] | None:
        if self.route.get("coordinate_system") != "gcj02":
            self.map_status.setText("腾讯步行路线需要 GCJ-02 路线坐标；当前路线未核实坐标系")
            self.log_output_text("腾讯步行路线请求已跳过：当前路线不是 GCJ-02。", "warning")
            return None
        strokes = [stroke for stroke in self.route.get("strokes", []) if stroke]
        if len(strokes) != 1 or len(strokes[0]) < 2:
            self.map_status.setText("请先建立一笔至少含起终点的 GCJ-02 路线")
            self.log_output_text("腾讯步行路线请求需要一笔连续路线作为起终点。", "warning")
            return None
        return copy.deepcopy(strokes[0][0]), copy.deepcopy(strokes[0][-1])

    def fetch_tencent_walking_route(self) -> None:
        if self._close_pending:
            return
        if self._tencent_road_thread is not None:
            self.map_status.setText("腾讯步行路线请求正在进行")
            return
        key = self.tencent_key_input.text().strip()
        if not key:
            self.map_status.setText("未配置腾讯地图 Key")
            self.log_output_text("腾讯步行路线请求需要本地腾讯地图 Key。", "warning")
            return
        endpoints = self._tencent_request_endpoints()
        if endpoints is None:
            return
        self.tencent_fetch_button.setEnabled(False)
        self.tencent_import_button.setEnabled(False)
        self.map_status.setText("正在请求腾讯步行路线…")
        self._tencent_road_thread = TencentRoadThread(key, endpoints[0], endpoints[1], self)
        self._tencent_road_thread.succeeded.connect(self._tencent_route_loaded)
        self._tencent_road_thread.failed.connect(self._tencent_route_failed)
        self._tencent_road_thread.finished.connect(self._tencent_route_finished)
        self._tencent_road_thread.start()

    def _tencent_route_loaded(self, overlay: object) -> None:
        if self._close_pending:
            return
        if not isinstance(overlay, dict) or not overlay.get("routes"):
            self._tencent_route_failed("腾讯步行接口没有可用路线。")
            return
        self._tencent_overlay = copy.deepcopy(overlay)
        self.map_view.set_tencent_road_overlay(self._tencent_overlay)
        self.tencent_import_button.setEnabled(True)
        first = self._tencent_overlay["routes"][0]
        names = overlay_route_names(self._tencent_overlay)
        name_text = "、".join(names[:4]) if names else "道路名称未返回"
        if len(names) > 4:
            name_text += "…"
        self.map_status.setText(
            f"腾讯步行路线已标记 · {float(first.get('distance_m', 0)):.0f} m · 道路：{name_text}"
        )
        self.log_output_text(
            f"腾讯接口返回 {len(self._tencent_overlay['routes'])} 条两点步行路线；黄色线为真实 polyline，当前只覆盖请求起终点。",
            "success",
        )

    def _tencent_route_failed(self, message: str) -> None:
        if self._close_pending:
            return
        self.map_status.setText("腾讯步行路线请求失败")
        self.log_output_text(message, "error")

    def _tencent_route_finished(self) -> None:
        thread = self._tencent_road_thread
        self._tencent_road_thread = None
        self.tencent_fetch_button.setEnabled(True)
        if thread is not None:
            thread.deleteLater()

    def import_tencent_walking_route(self) -> None:
        routes = self._tencent_overlay.get("routes") if isinstance(self._tencent_overlay, dict) else None
        if not isinstance(routes, list) or not routes or not routes[0].get("geometry"):
            self.map_status.setText("请先查询可用的腾讯步行路线")
            return
        selected = routes[0]
        names = overlay_route_names(self._tencent_overlay)
        route = normalise_route({
            "version": 1,
            "campus": "sjtu_minhang",
            "coordinate_system": "gcj02",
            "provider": "tencent-webservice-walking",
            "strokes": [copy.deepcopy(selected["geometry"])],
            "route_source": {
                "provider": self._tencent_overlay.get("provider", "tencent-webservice"),
                "endpoint": self._tencent_overlay.get("endpoint", "/ws/direction/v1/walking/"),
                "coordinate_system": self._tencent_overlay.get("coordinate_system", "gcj02"),
                "route_id": selected.get("id", 0),
                "distance_m": selected.get("distance_m"),
                "duration_min": selected.get("duration_min"),
                "road_names": names,
            },
        })
        self.route_mode_combo.setCurrentIndex(1)
        self._route_guard = True
        try:
            self.map_view.set_route(route)
        finally:
            self._route_guard = False
        self.route = route
        self._last_good_route = clone_route(route)
        self._selected_edge_ids = []
        self._selected_start_node = None
        self._selected_current_node = None
        self.update_metrics()
        self.map_status.setText("已采用腾讯首选步行 polyline · 仍只代表当前两点路线")
        self.log_output_text("已将腾讯返回的首选路线写入当前路线快照；未加入完整校园路网。", "success")

    def load_settings_to_ui(self, filename: str) -> None:
        self._persist_credentials()
        try:
            self.config = ConfigManager.load_config(filename)
        except ConfigError as exc:
            QMessageBox.critical(self, "配置加载失败", str(exc)); return
        self.current_config_filename = filename
        self.route = clone_route(self.config["ROUTE"])
        self.map_view.set_route(self.route)
        self.map_view.clear_tencent_road_overlay()
        self._tencent_overlay = {"routes": []}
        self.tencent_import_button.setEnabled(False)
        if self.road_graph is not None:
            self.map_view.set_road_graph(self.road_graph.to_dict())
            self.map_view.set_map_view(31.0281, 121.4323, 15, bounds=MINHANG_VIEW_BOUNDS)
        self.route_mode_combo.setCurrentIndex(0)
        self._restore_road_graph_state(self.route)
        self.speed_input.setText(str(self.config.get("RUNNING_SPEED_MPS", 2.5)))
        self.interval_input.setText(str(self.config.get("INTERVAL_SECONDS", 3)))
        self.user_id_input.setText(str(self.config.get("USER_ID", "")))
        self._set_cookie_fields(str(self.config.get("COOKIE", "")))
        self.mode_combo.setCurrentIndex(0 if self.config.get("API_MODE", "mock") == "mock" else 1)
        self.tencent_key_input.setText(str(self.config.get("TENCENT_MAP_KEY", "")))
        start_ms = self.config.get("START_TIME_EPOCH_MS")
        if start_ms is None:
            self.use_current_time.setChecked(True)
        else:
            self.use_current_time.setChecked(False); self.start_datetime.setDateTime(QDateTime.fromMSecsSinceEpoch(int(start_ms)))
        self.apply_map_credentials()
        self.update_metrics()
        self.log_output_text(f"已载入配置：{os.path.basename(filename)}", "info")

    def _credentials_edited(self, _text: str) -> None:
        self._credentials_dirty = True
        self._credentials_timer.start()

    def _persist_credentials(self) -> None:
        if not self._credentials_dirty:
            return
        self._credentials_timer.stop()
        cookie = "; ".join(
            f"{name}={field.text().strip()}"
            for name, field in (("keepalive", self.keepalive_input), ("JSESSIONID", self.jsessionid_input))
            if field.text().strip()
        )
        try:
            ConfigManager.save_credentials(self.user_id_input.text(), cookie, self.tencent_key_input.text())
            self._credentials_dirty = False
        except OSError:
            self.log_output_text("账户凭据自动保存失败，请检查本机配置目录的写入权限。", "error")

    def _set_cookie_fields(self, cookie: str) -> None:
        values: dict[str, str] = {}
        for item in cookie.split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1); values[key.strip()] = value.strip()
        self.keepalive_input.setText(values.get("keepalive", "")); self.jsessionid_input.setText(values.get("JSESSIONID", ""))

    def _restore_road_graph_state(self, route: dict) -> None:
        graph_meta = route.get("road_graph") if isinstance(route, dict) else None
        if not isinstance(graph_meta, dict):
            self._selected_edge_ids = []
            self._selected_start_node = None
            self._selected_current_node = None
            return
        raw_edges = graph_meta.get("edge_ids")
        self._selected_edge_ids = [str(edge_id) for edge_id in raw_edges] if isinstance(raw_edges, list) else []
        start_node = graph_meta.get("start_node_id")
        current_node = graph_meta.get("end_node_id")
        self._selected_start_node = str(start_node) if start_node is not None else None
        self._selected_current_node = str(current_node) if current_node is not None else None

    def get_settings_from_ui(self) -> dict:
        speed, interval = validate_run_parameters(self.speed_input.text(), self.interval_input.text())
        validate_route(self.route, require_path=True)
        validate_stroke_continuity(self.route)
        cookie = ""
        if self.keepalive_input.text().strip(): cookie = f"keepalive={self.keepalive_input.text().strip()}"
        if self.jsessionid_input.text().strip(): cookie = f"{cookie}; " if cookie else ""; cookie += f"JSESSIONID={self.jsessionid_input.text().strip()}"
        start_ms = None if self.use_current_time.isChecked() else self.start_datetime.dateTime().toMSecsSinceEpoch()
        return {
            **self.config,
            "COOKIE": cookie,
            "USER_ID": self.user_id_input.text().strip(),
            "RUNNING_SPEED_MPS": speed,
            "INTERVAL_SECONDS": interval,
            "START_TIME_EPOCH_MS": start_ms,
            "API_MODE": self.mode_combo.currentData(),
            "TENCENT_MAP_KEY": self.tencent_key_input.text().strip(),
            "ROUTE": clone_route(self.route),
        }

    def save_current_settings(self, filename: str) -> None:
        try:
            config = self.get_settings_from_ui()
            ConfigManager.save_config(config, filename)
            self.config = config; self.log_output_text(f"配置已保存：{os.path.basename(filename)}", "success")
        except (ConfigError, ValidationError, ValueError) as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def save_route_dialog(self) -> None:
        os.makedirs(os.path.join(get_user_data_path(), "routes"), exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "保存路线", os.path.join(get_user_data_path(), "routes", "minhang-route.json"), "Route JSON (*.json)")
        if path:
            try: save_route_file(self.route, path); self.log_output_text(f"路线已保存：{os.path.basename(path)}", "success")
            except (OSError, ValidationError) as exc: QMessageBox.critical(self, "路线保存失败", str(exc))

    def load_route_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "载入路线", os.path.join(get_user_data_path(), "routes"), "Route JSON (*.json)")
        if not path: return
        try:
            route = load_route_file(path)
            self.route = route
            self._last_good_route = clone_route(route)
            graph_meta = route.get("road_graph") or {}
            if graph_meta:
                self.route_mode_combo.setCurrentIndex(0)
            self._restore_road_graph_state(route)
            self.map_view.set_route(route)
            self.map_view.clear_tencent_road_overlay()
            self._tencent_overlay = {"routes": []}
            self.tencent_import_button.setEnabled(False)
            self.update_metrics(); self.log_output_text(f"路线已载入：{os.path.basename(path)}", "success")
        except (OSError, ValidationError) as exc: QMessageBox.critical(self, "路线载入失败", str(exc))

    def _set_editing_enabled(self, enabled: bool) -> None:
        for widget in (self.route_mode_combo, self.draw_button, self.finish_button, self.undo_button, self.clear_button, self.save_route_button, self.load_route_button, self.load_config_button, self.save_config_button, self.plan_distance_button, self.apply_laps_button, self.lap_input, self.target_distance_input, self.speed_input, self.interval_input, self.use_current_time, self.start_datetime, self.user_id_input, self.keepalive_input, self.jsessionid_input, self.tencent_key_input, self.mode_combo, self.tencent_fetch_button, self.tencent_import_button):
            widget.setEnabled(enabled)
        if not enabled:
            self.draw_button.setChecked(False); self._drawing = False; self.map_view.set_drawing_mode(False)

    def start_upload(self) -> None:
        if self._close_pending or self.thread is not None:
            return
        try:
            config = self.get_settings_from_ui()
            if config["API_MODE"] == "real" and (not config["COOKIE"] or not config["USER_ID"]):
                raise ValidationError("真实接口模式需要 Cookie 和用户 ID；本地路线编辑不需要登录。")
        except (ValidationError, ValueError, ConfigError) as exc:
            self.log_output_text(f"配置错误：{exc}", "error"); QMessageBox.critical(self, "无法开始", str(exc)); return
        self.config = copy.deepcopy(config)
        self.log_output_area.clear(); self.progress.setValue(0); self.status_label.setText("状态：路线快照已锁定")
        self._set_editing_enabled(False); self.upload_button.setEnabled(False); self.stop_button.setEnabled(True); self.help_button.setEnabled(False)
        self.thread = WorkerThread(config, self)
        self.thread.progress_update.connect(self.update_progress)
        self.thread.log_output.connect(self.log_output_text)
        self.thread.completed.connect(self.upload_finished)
        self.thread.finished.connect(self._worker_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def stop_upload(self) -> None:
        if self.thread and self.thread.isRunning():
            self.thread.requestInterruption(); self.stop_button.setEnabled(False); self.status_label.setText("状态：正在停止…"); self.log_output_text("已发送停止请求。", "warning")

    def update_progress(self, current: int, total: int, message: str) -> None:
        self.progress.setMaximum(total); self.progress.setValue(current); self.status_label.setText(f"状态：{message}")

    def log_output_text(self, message: str, level: str = "info") -> None:
        self.log_output_area.moveCursor(QTextCursor.End)
        self.log_output_area.insertPlainText(f"[{level.upper()}] {redact_secrets(message)}\n")
        self.log_output_area.ensureCursorVisible()

    def upload_finished(self, success: bool, message: str) -> None:
        if self._close_pending:
            return
        self.stop_button.setEnabled(False)
        self.progress.setValue(100 if success else self.progress.value())
        pending = "待核实" in message
        self.status_label.setText("状态：已提交，待核实" if pending else ("状态：上传成功" if success else "状态：上传失败"))
        self.log_output_text(message, "warning" if pending else ("success" if success else "error"))

    def _worker_thread_finished(self) -> None:
        thread = self.sender()
        if thread is self.thread:
            self.thread = None
            if not self._close_pending:
                self._set_editing_enabled(True)
                self.upload_button.setEnabled(True)
                self.help_button.setEnabled(True)

    def toggle_time_input(self, use_current: bool) -> None:
        self.start_datetime.setEnabled(not use_current)
        if use_current: self.start_datetime.setDateTime(QDateTime.currentDateTime())

    def show_help_dialog(self) -> None:
        dialog = HelpDialog(self, markdown_relative_path=os.path.join("assets", "help.md"))
        dialog.exec()

    def _running_threads(self) -> list[QThread]:
        return [
            thread for thread in (self.thread, self._tencent_road_thread)
            if thread is not None and thread.isRunning()
        ]

    def _poll_close(self) -> None:
        if not self._close_pending:
            return
        active = self._running_threads()
        if active:
            QTimer.singleShot(100, self._poll_close)
            return
        self._allow_close = True
        self.close()

    def closeEvent(self, event) -> None:
        """Stop background work before Qt tears down the WebEngine child."""
        self._persist_credentials()
        if self._allow_close:
            shutdown = getattr(self.map_view, "shutdown", None)
            if callable(shutdown):
                shutdown()
            event.accept()
            return
        active = self._running_threads()
        if active:
            self._close_pending = True
            for thread in active:
                thread.requestInterruption()
            self.status_label.setText("状态：正在结束后台任务…")
            self.setEnabled(False)
            event.ignore()
            QTimer.singleShot(0, self._poll_close)
            return
        shutdown = getattr(self.map_view, "shutdown", None)
        if callable(shutdown):
            shutdown()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = SportsUploaderUI()
    window.show()

    # Let the first event-loop turn create the native window before asking
    # Windows to activate it.  This avoids racing WebEngine construction with
    # the initial show/raise sequence on slower graphics drivers.
    def activate_window() -> None:
        if window.isVisible():
            window.showNormal()
            window.raise_()
            window.activateWindow()

    QTimer.singleShot(0, activate_window)
    sys.exit(app.exec())
