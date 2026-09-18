"""Lifecycle checks without creating a WebEngine process or network request."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.map_view import RouteMapWidget, _AssetServer
from qtui import SportsUploaderUI
from src.data_generator import _point_at_distance, _sample_stroke, interpolate_points
from src.utils import ValidationError, SportsUploaderError


class RuntimeTests(unittest.TestCase):
    def test_sampling_keeps_vertices_and_duplicate_distance_boundaries(self):
        stroke = [{"latitude": 31, "longitude": x} for x in (121, 121.001, 121.001, 121.002)]
        cumulative = [0, 100, 100, 200]
        self.assertEqual(_point_at_distance(stroke, cumulative, 100), stroke[1])
        self.assertAlmostEqual(_point_at_distance(stroke, cumulative, 150)["longitude"], 121.0015)
        sampled, _, _ = _sample_stroke(stroke, 2.5, 3)
        for vertex in stroke:
            self.assertIn(vertex, [point["latLng"] for point in sampled])

    def test_tiny_sampling_interval_rejected_before_allocating_points(self):
        stroke = [{"latitude": 31, "longitude": x} for x in (121, 121.001)]
        for speed, interval in ((2.5, 1e-12), (1e-300, 1e-300)):
            with self.assertRaises(ValidationError):
                _sample_stroke(stroke, speed, interval)
            with self.assertRaises(ValidationError):
                interpolate_points(31, 121, 31, 121.001, speed, interval)

    def test_sampling_honors_cancellation(self):
        stroke = [{"latitude": 31, "longitude": x} for x in (121, 121.001)]
        with self.assertRaises(SportsUploaderError):
            _sample_stroke(stroke, 2.5, 3, lambda: True)

    def test_map_handshake_both_event_orders_and_reload(self):
        for order in (("ready", "loaded"), ("loaded", "ready")):
            view = SimpleNamespace(_closed=False, _page_ready=False, _state_pushed=False)
            def push():
                view._state_pushed = True
            view._push_state = Mock(side_effect=push)
            def event(kind):
                if kind == "ready":
                    RouteMapWidget._on_map_ready(view)
                else:
                    RouteMapWidget._on_load_finished(view, True)
            for kind in order:
                event(kind)
            self.assertEqual(view._push_state.call_count, 1)
            RouteMapWidget._on_load_started(view)
            for kind in order:
                event(kind)
            self.assertEqual(view._push_state.call_count, 2)
            view._closed = True
            view._state_pushed = False
            for kind in order:
                event(kind)
            self.assertEqual(view._push_state.call_count, 2)

    def test_graph_transport_preserves_geometry_and_is_detached(self):
        source = {"nodes": [{"id": "a", "latitude": 31, "longitude": 121}],
                  "edges": [{"id": "aa", "start": "a", "end": "a", "one_way": True,
                             "geometry": [{"latitude": 31, "longitude": 121}],
                             "source_queries": ["large unused provenance"]}]}
        view = SimpleNamespace(_run=Mock())
        RouteMapWidget.set_road_graph(view, source)
        self.assertEqual(view._road_graph["edges"][0]["geometry"], source["edges"][0]["geometry"])
        self.assertNotIn("source_queries", view._road_graph["edges"][0])
        source["edges"][0]["geometry"][0]["latitude"] = 99
        self.assertEqual(view._road_graph["edges"][0]["geometry"][0]["latitude"], 31)
        self.assertEqual(json.loads(view._run.call_args.args[1]), view._road_graph)

    def test_asset_server_close_is_idempotent_and_releases_thread(self):
        server = _AssetServer("assets")
        try:
            server.close()
            server.close()
            self.assertFalse(server.thread.is_alive())
        finally:
            server.close()

    def test_cannot_replace_worker_before_finished_signal(self):
        window = SimpleNamespace(_close_pending=False, thread=object(), get_settings_from_ui=Mock())
        SportsUploaderUI.start_upload(window)
        window.get_settings_from_ui.assert_not_called()

    def test_shutdown_stops_map_only_once(self):
        server = Mock()
        view = SimpleNamespace(_closed=False, _page_ready=True, stop=Mock(), _asset_server=server)
        RouteMapWidget.shutdown(view)
        RouteMapWidget.shutdown(view)
        view.stop.assert_called_once()
        server.close.assert_called_once()
        self.assertFalse(view._page_ready)
