from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qtui import SportsUploaderUI
from src import config_manager
from src.api_client import mock_upload_response
from src.data_generator import generate_running_data_payload
from src.main import run_sports_upload
from src.road_graph import AmbiguousRoadError, RoadGraph, RoadSnapError
from src.route_model import (
    load_route_file,
    normalise_route,
    route_distance_m,
    route_metrics,
    save_route_file,
    validate_stroke_continuity,
)
from src.tencent_roads import (
    TencentRoadError,
    decode_tencent_polyline,
    overlay_route_names,
    parse_tencent_walking_response,
    request_tencent_walking_routes,
)
from src.tencent_network import (
    build_candidate_graph,
    collect_query_batch,
    geometry_length_m,
    load_query_cache,
    query_id,
)
from src.utils import ValidationError, redact_secrets, validate_run_parameters, validate_route


def point(lat: float, lon: float) -> dict[str, float]:
    return {"latitude": lat, "longitude": lon}


def route(strokes: list[list[dict[str, float]]]) -> dict:
    return {
        "version": 1,
        "campus": "sjtu_minhang",
        "coordinate_system": "wgs84",
        "provider": "offline-grid",
        "strokes": strokes,
    }


class RouteModelTests(unittest.TestCase):
    def test_save_load_preserves_strokes_and_order(self) -> None:
        original = route([
            [point(31.0, 121.0), point(31.0, 121.001), point(31.001, 121.001)],
            [point(31.002, 121.002), point(31.003, 121.002)],
        ])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "route.json"
            save_route_file(original, path)
            loaded = load_route_file(path)
        self.assertEqual(loaded, original)

    def test_save_load_migrates_legacy_select_mode_without_losing_geometry(self) -> None:
        original = route([[point(31.0, 121.0), point(31.0, 121.001)]])
        original["road_graph"] = {
            "edge_ids": ["edge-a"],
            "start_node_id": "node-a",
            "end_node_id": "node-b",
            "mode": "select",
            "verification_status": "manual_seed_partial",
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "route.json"
            save_route_file(original, path)
            loaded = load_route_file(path)
        self.assertEqual(loaded["strokes"], original["strokes"])
        self.assertEqual(loaded["road_graph"]["mode"], "snap")
        self.assertEqual(loaded["road_graph"]["edge_ids"], ["edge-a"])
        self.assertEqual(loaded["road_graph"]["start_node_id"], "node-a")

    def test_save_load_preserves_safe_tencent_route_provenance(self) -> None:
        original = route([[point(31.0, 121.0), point(31.0, 121.001)]])
        original["route_source"] = {
            "provider": "tencent-webservice",
            "endpoint": "/ws/direction/v1/walking/",
            "coordinate_system": "gcj02",
            "route_id": 0,
            "distance_m": 120.0,
            "duration_min": 2.0,
            "road_names": ["测试道路"],
            "ignored": "not copied",
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "route.json"
            save_route_file(original, path)
            loaded = load_route_file(path)
        self.assertEqual(loaded["route_source"]["road_names"], ["测试道路"])
        self.assertNotIn("ignored", loaded["route_source"])

    def test_distance_does_not_connect_independent_strokes(self) -> None:
        first = [point(31.0, 121.0), point(31.0, 121.001)]
        second = [point(31.01, 121.01), point(31.01, 121.011)]
        value = route_distance_m(route([first, second]))
        self.assertAlmostEqual(value, route_distance_m(route([first])) + route_distance_m(route([second])), places=7)

    def test_closed_loop_and_crossing_points_are_retained(self) -> None:
        crossing = [
            point(31.0, 121.0), point(31.001, 121.001), point(31.0, 121.001),
            point(31.001, 121.0), point(31.0, 121.0),
        ]
        canonical = normalise_route(route([crossing]))
        self.assertEqual(canonical["strokes"][0][0], canonical["strokes"][0][-1])
        self.assertEqual(len(canonical["strokes"][0]), 5)

    def test_legacy_config_migrates_to_one_stroke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            previous = config_manager.CONFIGS_DIR
            config_manager.CONFIGS_DIR = temp
            try:
                Path(temp, "legacy.json").write_text(json.dumps({
                    "START_LATITUDE": 31.1,
                    "START_LONGITUDE": 121.1,
                    "END_LATITUDE": 31.2,
                    "END_LONGITUDE": 121.2,
                }), encoding="utf-8")
                migrated = config_manager.ConfigManager.load_config("legacy.json")
            finally:
                config_manager.CONFIGS_DIR = previous
        self.assertEqual(len(migrated["ROUTE"]["strokes"]), 1)
        self.assertEqual(migrated["ROUTE"]["strokes"][0][0]["latitude"], 31.1)

    def test_config_migrates_select_route_mode_to_snap_without_geometry_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            previous = config_manager.CONFIGS_DIR
            config_manager.CONFIGS_DIR = temp
            try:
                Path(temp, "legacy-select.json").write_text(json.dumps({
                    "ROUTE": {
                        **route([[point(31.0, 121.0), point(31.0, 121.001)]]),
                        "road_graph": {
                            "edge_ids": ["edge-a"],
                            "start_node_id": "node-a",
                            "end_node_id": "node-b",
                            "mode": "select",
                        },
                    },
                }), encoding="utf-8")
                migrated = config_manager.ConfigManager.load_config("legacy-select.json")
            finally:
                config_manager.CONFIGS_DIR = previous
        self.assertEqual(migrated["ROUTE"]["road_graph"]["mode"], "snap")
        self.assertEqual(migrated["ROUTE"]["strokes"][0][0], point(31.0, 121.0))


class RouteEditorUiCompatibilityTests(unittest.TestCase):
    def test_click_select_mode_and_bridge_are_removed_but_snap_search_remains(self) -> None:
        qtui_source = Path("qtui.py").read_text(encoding="utf-8")
        map_view_source = Path("src/map_view.py").read_text(encoding="utf-8")
        map_source = Path("assets/map.html").read_text(encoding="utf-8")
        self.assertNotIn('addItem("点击选路"', qtui_source)
        self.assertNotIn("def map_clicked", qtui_source)
        self.assertNotIn("mapClicked", map_view_source)
        self.assertNotIn("receiveMapClick", map_source)
        self.assertIn("function nodeAtPixel", map_source)
        self.assertIn("function graphPath", map_source)

    def test_planning_start_uses_snap_or_existing_route_origin(self) -> None:
        graph = RoadGraph({
            "version": 1,
            "metadata": {"coordinate_system": "gcj02"},
            "nodes": [
                {"id": "a", "latitude": 31.0, "longitude": 121.0},
                {"id": "b", "latitude": 31.0, "longitude": 121.001},
            ],
            "edges": [{
                "id": "ab",
                "start": "a",
                "end": "b",
                "geometry": [point(31.0, 121.0), point(31.0, 121.001)],
                "one_way": False,
            }],
        })
        ui = SportsUploaderUI.__new__(SportsUploaderUI)
        ui.road_graph = graph
        ui._selected_start_node = "b"
        ui.route = route([])
        self.assertEqual(ui._planning_start_node(), "b")

        ui._selected_start_node = None
        ui.route = route([[point(31.0, 121.0), point(31.0, 121.001)]])
        self.assertEqual(ui._planning_start_node(), "a")

        ui._selected_start_node = None
        ui.route = route([])
        self.assertIsNone(ui._planning_start_node())


class TencentRoadTests(unittest.TestCase):
    def test_decode_tencent_polyline_accumulates_microdegree_deltas(self) -> None:
        values = [31.0, 121.0, 1000, 2000, -500, 3000]
        decoded = decode_tencent_polyline(values)
        expected = [point(31.0, 121.0), point(31.001, 121.002), point(31.0005, 121.005)]
        self.assertEqual(len(decoded), len(expected))
        for actual, wanted in zip(decoded, expected):
            self.assertAlmostEqual(actual["latitude"], wanted["latitude"], places=9)
            self.assertAlmostEqual(actual["longitude"], wanted["longitude"], places=9)

    def test_parse_walking_response_splits_steps_by_flattened_indices(self) -> None:
        payload = {
            "status": 0,
            "message": "Success",
            "result": {
                "routes": [{
                    "mode": "WALKING",
                    "distance": 120,
                    "duration": 2,
                    "direction": "东",
                    "polyline": [31.0, 121.0, 1000, 0, 0, 1000, 1000, 0],
                    "steps": [
                        {"road_name": "测试路", "distance": 60, "polyline_idx": [0, 5]},
                        {"road_name": "第二路", "distance": 60, "polyline_idx": [4, 7]},
                    ],
                }],
            },
        }
        overlay = parse_tencent_walking_response(payload)
        route_data = overlay["routes"][0]
        self.assertEqual(len(route_data["geometry"]), 4)
        self.assertEqual(route_data["steps"][0]["geometry"], route_data["geometry"][:3])
        self.assertEqual(overlay_route_names(overlay), ["测试路", "第二路"])

    def test_request_uses_key_only_as_request_parameter(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "status": 0,
                    "message": "Success",
                    "result": {"routes": [{
                        "distance": 1,
                        "duration": 1,
                        "polyline": [31.0, 121.0, 0, 1000],
                        "steps": [{"road_name": "测试路", "distance": 1, "polyline_idx": [0, 3]}],
                    }]},
                }

        class FakeSession:
            def __init__(self) -> None:
                self.calls = []

            def get(self, url, *, params, timeout):
                self.calls.append((url, params, timeout))
                return FakeResponse()

        session = FakeSession()
        overlay = request_tencent_walking_routes(
            "unit-test-key",
            point(31.0, 121.0),
            point(31.001, 121.001),
            session=session,
        )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["key"], "unit-test-key")
        self.assertEqual(overlay["route_count"], 1)

    def test_invalid_response_is_rejected(self) -> None:
        with self.assertRaises(TencentRoadError):
            parse_tencent_walking_response({"status": 0, "result": {"routes": []}})


class TencentNetworkTests(unittest.TestCase):
    def test_cache_resume_avoids_duplicate_provider_request(self) -> None:
        specifications = [{
            "label": "unit-sample",
            "region": "unit",
            "from": point(31.0, 121.0),
            "to": point(31.0, 121.001),
        }]
        overlay = {
            "provider": "tencent-webservice",
            "endpoint": "/ws/direction/v1/walking/",
            "coordinate_system": "gcj02",
            "routes": [{
                "id": 0,
                "distance_m": 100.0,
                "duration_min": 2.0,
                "geometry": [point(31.0, 121.0), point(31.0, 121.001)],
                "steps": [{
                    "road_name": "测试路",
                    "geometry": [point(31.0, 121.0), point(31.0, 121.001)],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "routes.json"
            with patch("src.tencent_network.request_tencent_walking_routes", return_value=overlay) as request:
                first = collect_query_batch("local-key", specifications, cache_path=cache_path, delay_seconds=0, sleep_fn=None)
                second = collect_query_batch("local-key", specifications, cache_path=cache_path, delay_seconds=0, sleep_fn=None)
            saved = load_query_cache(cache_path)
        self.assertEqual(first["successes"], 1)
        self.assertEqual(second["cache_hits"], 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(saved["queries"][0]["query_id"], query_id(specifications[0]["from"], specifications[0]["to"]))

    def test_candidate_graph_keeps_partial_route_provenance_and_loads(self) -> None:
        cache = {
            "queries": [{
                "query_id": "q1",
                "status": "success",
                "overlay": {
                    "routes": [{
                        "id": 0,
                        "geometry": [point(31.0, 121.0), point(31.0, 121.001)],
                        "steps": [{
                            "road_name": "测试路",
                            "geometry": [point(31.0, 121.0), point(31.0, 121.001)],
                        }],
                    }],
                },
            }],
        }
        graph = build_candidate_graph(cache, bounds={"south": 30.9, "west": 120.9, "north": 31.1, "east": 121.1})
        self.assertEqual(graph["metadata"]["verification_status"], "provider_route_partial")
        self.assertEqual(graph["metadata"]["source_query_ids"], ["q1"])
        self.assertEqual(len(graph["edges"]), 1)
        self.assertAlmostEqual(graph["edges"][0]["length_m"], geometry_length_m(graph["edges"][0]["geometry"]))
        RoadGraph(graph)

    def test_candidate_graph_does_not_clip_out_of_scope_route(self) -> None:
        cache = {
            "queries": [{
                "query_id": "q-out",
                "status": "success",
                "overlay": {
                    "routes": [{
                        "id": 0,
                        "geometry": [point(31.0, 121.0), point(31.0, 121.2)],
                        "steps": [{
                            "road_name": "越界路线",
                            "geometry": [point(31.0, 121.0), point(31.0, 121.2)],
                        }],
                    }],
                },
            }],
        }
        graph = build_candidate_graph(cache, bounds={"south": 30.9, "west": 120.9, "north": 31.1, "east": 121.1})
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["metadata"]["out_of_scope_route_count"], 1)


class GeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "API_MODE": "mock",
            "USER_ID": "test",
            "RUNNING_SPEED_MPS": 2.0,
            "INTERVAL_SECONDS": 3,
            "START_TIME_EPOCH_MS": 1_700_000_000_000,
            "ROUTE": route([
                [point(31.0, 121.0), point(31.0, 121.001)],
                [point(31.01, 121.01), point(31.01, 121.011)],
            ]),
        }

    def test_payload_has_one_normal_track_per_stroke_and_id_is_preserved(self) -> None:
        payload, distance, duration = generate_running_data_payload(self.config, [], {"rules": {"id": 6}})
        tracks = payload[0]["tracks"]
        self.assertEqual(payload[0]["id"], 6)
        self.assertEqual(len(tracks), 2)
        self.assertTrue(all(track["status"] == "normal" and track["tstate"] == "0" for track in tracks))
        self.assertAlmostEqual(sum(track["distance"] for track in tracks), distance, delta=0.2)
        self.assertEqual(duration, round(distance / self.config["RUNNING_SPEED_MPS"] + 0.499999))

    def test_duration_has_no_extra_sampling_period(self) -> None:
        config = dict(self.config)
        config["ROUTE"] = route([[point(31.0, 121.0), point(31.0, 121.0001)]])
        payload, distance, duration = generate_running_data_payload(config, [], {"rules": {"id": 8}})
        track = payload[0]["tracks"][0]
        timestamp_duration = (track["points"][-1]["locatetime"] - track["points"][0]["locatetime"]) / 1000
        self.assertLessEqual(timestamp_duration, distance / 2.0 + 0.01)
        self.assertEqual(track["duration"], duration)


class ValidationAndUploadTests(unittest.TestCase):
    def test_nan_and_invalid_route_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_run_parameters("nan", 3)
        with self.assertRaises(ValidationError):
            validate_run_parameters(2.5, 0)
        with self.assertRaises(ValidationError):
            validate_route(route([[point(31.0, 121.0)]]), require_path=True)

    def test_empty_success_response_is_not_retried(self) -> None:
        config = {
            "API_MODE": "mock",
            "MOCK_BEHAVIOR": "empty",
            "USER_ID": "test",
            "RUNNING_SPEED_MPS": 2.0,
            "INTERVAL_SECONDS": 3,
            "START_TIME_EPOCH_MS": 1_700_000_000_000,
            "WAIT_BEFORE_UPLOAD": False,
            "ROUTE": route([[point(31.0, 121.0), point(31.0, 121.0002)]]),
        }
        calls = []

        def one_call(*args, **kwargs):
            calls.append(1)
            return {"code": 0, "data": None}

        with patch("src.main.upload_running_data", side_effect=one_call):
            success, message = run_sports_upload(config)
        self.assertTrue(success)
        self.assertEqual(len(calls), 1)
        self.assertIn("重复提交", message)
        self.assertIn("待核实", message)

    def test_disconnected_strokes_are_rejected_before_upload(self) -> None:
        config = {
            "API_MODE": "mock",
            "USER_ID": "test",
            "RUNNING_SPEED_MPS": 2.0,
            "INTERVAL_SECONDS": 3,
            "START_TIME_EPOCH_MS": 1_700_000_000_000,
            "ROUTE": route([
                [point(31.0, 121.0), point(31.0, 121.0002)],
                [point(31.01, 121.01), point(31.01, 121.0102)],
            ]),
        }
        success, message = run_sports_upload(config)
        self.assertFalse(success)
        self.assertIn("相距", message)

    def test_tencent_provider_is_the_new_default(self) -> None:
        defaults = config_manager.ConfigManager.get_default_config()
        self.assertEqual(defaults["MAP_PROVIDER"], "tencent")
        self.assertEqual(defaults["TENCENT_MAP_KEY"], "")
        self.assertNotIn("AMAP_KEY", defaults)

    def test_tencent_key_is_saved_separately_from_regular_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            previous = config_manager.CONFIGS_DIR
            config_manager.CONFIGS_DIR = temp
            try:
                config = config_manager.ConfigManager.get_default_config()
                config["TENCENT_MAP_KEY"] = "test-key"
                config_manager.ConfigManager.save_config(config, "default.json")
                stored = json.loads(Path(temp, "default.json").read_text(encoding="utf-8"))
                private = json.loads(Path(temp, "tencent.local.json").read_text(encoding="utf-8"))
                loaded = config_manager.ConfigManager.load_config("default.json")
            finally:
                config_manager.CONFIGS_DIR = previous
        self.assertNotIn("TENCENT_MAP_KEY", stored)
        self.assertEqual(private["TENCENT_MAP_KEY"], "test-key")
        self.assertEqual(loaded["TENCENT_MAP_KEY"], "test-key")

    def test_mock_modes_are_explicit(self) -> None:
        self.assertEqual(mock_upload_response({"MOCK_BEHAVIOR": "reject"})["code"], 1)
        self.assertEqual(mock_upload_response({"MOCK_BEHAVIOR": "empty"})["code"], 0)

    def test_secrets_are_redacted(self) -> None:
        text = redact_secrets("Cookie: keepalive=abc; JSESSIONID=secret Authorization: bearer-secret")
        self.assertNotIn("secret", text)
        self.assertNotIn("abc", text)


class RoadGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        nodes = [
            {"id": "a", "latitude": 31.0, "longitude": 121.0, "name": "A"},
            {"id": "b", "latitude": 31.0, "longitude": 121.001, "name": "B"},
            {"id": "c", "latitude": 31.001, "longitude": 121.001, "name": "C"},
            {"id": "d", "latitude": 31.001, "longitude": 121.0, "name": "D"},
        ]
        def edge(edge_id: str, start: str, end: str) -> dict:
            points = {item["id"]: item for item in nodes}
            return {
                "id": edge_id,
                "start": start,
                "end": end,
                "geometry": [
                    {"latitude": points[start]["latitude"], "longitude": points[start]["longitude"]},
                    {"latitude": points[end]["latitude"], "longitude": points[end]["longitude"]},
                ],
                "one_way": False,
            }
        self.graph = RoadGraph({
            "version": 1,
            "metadata": {"coordinate_system": "gcj02", "verification_status": "synthetic_test"},
            "nodes": nodes,
            "edges": [edge("ab", "a", "b"), edge("bc", "b", "c"), edge("cd", "c", "d"), edge("da", "d", "a")],
        })

    def test_snap_rejects_points_outside_threshold_and_keeps_edge_identity(self) -> None:
        candidate = self.graph.snap_point({"latitude": 31.00001, "longitude": 121.0005}, max_distance_m=35)
        self.assertEqual(candidate.edge_id, "ab")
        with self.assertRaises(RoadSnapError):
            self.graph.snap_point({"latitude": 31.01, "longitude": 121.01}, max_distance_m=35)

    def test_ambiguous_junction_requires_choice(self) -> None:
        with self.assertRaises(AmbiguousRoadError):
            self.graph.snap_point({"latitude": 31.0, "longitude": 121.001}, max_distance_m=35)

    def test_shortest_path_and_laps_use_declared_connectivity(self) -> None:
        edges, nodes, distance = self.graph.shortest_path("a", "c")
        self.assertIn(edges, (["ab", "bc"], ["da", "cd"]))
        self.assertIn(nodes, (["a", "b", "c"], ["a", "d", "c"]))
        self.assertGreater(distance, 200)
        expanded = self.graph.expand_laps(["ab", "bc", "cd", "da"], 2)
        self.assertEqual(len(expanded), 8)
        with self.assertRaises(Exception):
            self.graph.expand_laps(["ab", "bc"], 2)

    def test_laps_respect_reverse_start_orientation_and_blocked_edges(self) -> None:
        reversed_loop = self.graph.expand_laps(
            ["da", "cd", "bc", "ab"],
            2,
            start_node_id="a",
        )
        self.assertEqual(len(reversed_loop), 8)
        edges, nodes, _distance = self.graph.shortest_path("b", "c", blocked_edges={"bc"})
        self.assertEqual(edges, ["ab", "da", "cd"])
        self.assertEqual(nodes, ["b", "a", "d", "c"])

    def test_shipped_graph_is_provider_geometry_and_explicitly_partial(self) -> None:
        graph = RoadGraph.from_file("data/tencent_road_graph.candidate.json")
        self.assertEqual(graph.metadata["coordinate_system"], "gcj02")
        self.assertEqual(graph.metadata["verification_status"], "provider_route_partial")
        self.assertGreater(len(graph.nodes), 300)
        self.assertGreater(len(graph.edges), 400)

    def test_target_plan_matches_independent_endpoint_search(self) -> None:
        for start in self.graph.nodes:
            for target in (10, 100, 250, 1000):
                choices = []
                for end in self.graph.nodes:
                    if start != end:
                        edges, nodes, length = self.graph.shortest_path(start, end)
                        choices.append((abs(length - target), length, edges, nodes))
                best = min(choices, key=lambda item: item[:2])
                plan = self.graph.path_for_target_distance(start, target)
                self.assertEqual(plan["edge_ids"], best[2])
                self.assertEqual(plan["node_ids"], best[3])
                self.assertEqual(plan["distance_m"], best[1])

    def test_target_plan_preserves_directed_cycle_and_unreachable_nodes(self) -> None:
        payload = self.graph.to_dict()
        for edge in payload["edges"]:
            edge["one_way"] = True
        payload["nodes"].append({"id": "isolated", "latitude": 32, "longitude": 121})
        graph = RoadGraph(payload)
        cycle = graph.path_for_target_distance("a", 400, return_to_start=True)
        self.assertEqual(cycle["edge_ids"], ["ab", "bc", "cd", "da"])
        self.assertEqual(cycle["node_ids"], ["a", "b", "c", "d", "a"])
        self.assertNotIn("isolated", graph.path_for_target_distance("a", 1000)["node_ids"])
        with self.assertRaises(ValidationError):
            graph.path_for_target_distance("isolated", 100)


if __name__ == "__main__":
    unittest.main()
