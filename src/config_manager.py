"""JSON configuration and route migration without a Qt dependency."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from src.route_model import (
    DEFAULT_CAMPUS,
    DEFAULT_COORDINATE_SYSTEM,
    normalise_route,
    route_endpoints,
    route_from_legacy_config,
)
from src.utils import ValidationError, get_user_data_path, validate_run_parameters, validate_route


CONFIGS_DIR = os.path.join(get_user_data_path(), "configs")
DEFAULT_CONFIG_FILE_NAME = "default.json"
DEFAULT_CONFIG_FILE = os.path.join(CONFIGS_DIR, DEFAULT_CONFIG_FILE_NAME)
PRIVATE_MAP_CONFIG_FILE_NAME = "tencent.local.json"


class ConfigError(ValidationError):
    """Configuration file or value is invalid."""


def _default_route() -> dict[str, Any]:
    return {
        "version": 1,
        "campus": DEFAULT_CAMPUS,
        "coordinate_system": DEFAULT_COORDINATE_SYSTEM,
        "provider": "tencent",
        # Start empty so the first route is always user-selected or visibly
        # traced on the loaded map; coordinates from an old default route must
        # not look like verified campus geometry.
        "strokes": [],
    }


class ConfigManager:
    """Load/save app settings and migrate old endpoint-only files."""

    @staticmethod
    def save_credentials(user_id: str, cookie: str, map_key: str) -> None:
        """Persist credentials independently of route/parameter validation."""
        path = Path(CONFIGS_DIR) / "credentials.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "USER_ID": user_id.strip(), "COOKIE": cookie.strip(),
            "TENCENT_MAP_KEY": map_key.strip(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _load_credentials() -> dict[str, str]:
        path = Path(CONFIGS_DIR) / "credentials.local.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or any(
                not isinstance(payload.get(key), str)
                for key in ("USER_ID", "COOKIE", "TENCENT_MAP_KEY")
            ):
                raise ValueError("invalid credential fields")
            return {key: payload[key] for key in ("USER_ID", "COOKIE", "TENCENT_MAP_KEY")}
        except (OSError, ValueError) as exc:
            raise ConfigError("无法读取本机保存的账户凭据，请检查 credentials.local.json。") from exc

    @staticmethod
    def get_default_config() -> dict[str, Any]:
        return {
            "COOKIE": "",
            "USER_ID": "",
            "START_LATITUDE": 31.031599,
            "START_LONGITUDE": 121.442938,
            "END_LATITUDE": 31.026400,
            "END_LONGITUDE": 121.455100,
            "RUNNING_SPEED_MPS": 2.5,
            "INTERVAL_SECONDS": 3,
            "START_TIME_EPOCH_MS": None,
            "HOST": "pe.sjtu.edu.cn",
            "UID_URL": "https://pe.sjtu.edu.cn/sports/my/uid",
            "MY_DATA_URL": "https://pe.sjtu.edu.cn/sports/my/data",
            "POINT_RULE_URL": "https://pe.sjtu.edu.cn/api/running/point-rule",
            "UPLOAD_URL": "https://pe.sjtu.edu.cn/api/running/result/upload",
            # Explicit mock mode keeps local development from touching a school account.
            "API_MODE": "mock",
            "MOCK_RESPONSE": {"code": 0, "data": {"accepted": True}},
            "WAIT_BEFORE_UPLOAD": False,
            "TENCENT_MAP_KEY": "",
            "MAP_PROVIDER": "tencent",
            "ROUTE": _default_route(),
        }

    @staticmethod
    def _safe_filename(filename: str | os.PathLike[str]) -> str:
        name = os.fspath(filename)
        if os.path.isabs(name) or os.path.basename(name) != name:
            raise ConfigError("配置文件名必须是 configs 目录内的文件名。")
        if name in {".", ".."} or not name.lower().endswith(".json"):
            raise ConfigError("配置文件名必须以 .json 结尾。")
        return name

    @staticmethod
    def path_for(filename: str = DEFAULT_CONFIG_FILE_NAME) -> Path:
        return Path(CONFIGS_DIR) / ConfigManager._safe_filename(filename)

    @staticmethod
    def _private_map_path() -> Path:
        return Path(CONFIGS_DIR) / PRIVATE_MAP_CONFIG_FILE_NAME

    @staticmethod
    def _load_private_map_key() -> str:
        path = ConfigManager._private_map_path()
        if not path.exists():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"腾讯地图本地凭据无法读取: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError("腾讯地图本地凭据必须是 JSON 对象。")
        value = payload.get("TENCENT_MAP_KEY", "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ConfigError("腾讯地图本地 Key 必须是字符串。")
        return value.strip()

    @staticmethod
    def _save_private_map_key(key: str) -> None:
        path = ConfigManager._private_map_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"TENCENT_MAP_KEY": str(key).strip()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _with_migrated_route(config: Mapping[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(dict(config))
        # The provider was changed to Tencent JavaScript API. Do not carry old
        # provider credentials into the active configuration or UI.
        merged.pop("AMAP_KEY", None)
        merged.pop("AMAP_SECURITY_CODE", None)
        route = route_from_legacy_config(merged)
        merged["ROUTE"] = normalise_route(route)

        start, end = route_endpoints(merged["ROUTE"])
        if start and end:
            merged["START_LATITUDE"] = start["latitude"]
            merged["START_LONGITUDE"] = start["longitude"]
            merged["END_LATITUDE"] = end["latitude"]
            merged["END_LONGITUDE"] = end["longitude"]
        return merged

    @staticmethod
    def load_config(filename: str = DEFAULT_CONFIG_FILE_NAME) -> dict[str, Any]:
        path = ConfigManager.path_for(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        defaults = ConfigManager.get_default_config()
        loaded: dict[str, Any] = {}
        if path.exists():
            try:
                loaded_payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"配置文件无法读取: {path.name}: {exc}") from exc
            if not isinstance(loaded_payload, dict):
                raise ConfigError("配置文件根对象必须是 JSON 对象。")
            loaded = loaded_payload

        merged = {**defaults, **loaded}
        private_key = ConfigManager._load_private_map_key()
        if private_key:
            # Keep the key in a separate ignored file.  It never becomes part
            # of the regular user configuration or route JSON.
            merged["TENCENT_MAP_KEY"] = private_key
        merged.update(ConfigManager._load_credentials())
        if path.exists() and "ROUTE" not in loaded:
            # The old file has only four endpoint keys; preserve it through a
            # predictable one-stroke migration.
            merged.pop("ROUTE", None)
        try:
            return ConfigManager._with_migrated_route(merged)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ConfigError(f"路线配置无效: {exc}") from exc

    @staticmethod
    def prepare_for_save(config_data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(config_data, Mapping):
            raise ConfigError("配置必须是对象。")
        config = {**ConfigManager.get_default_config(), **copy.deepcopy(dict(config_data))}
        if "ROUTE" not in config_data:
            config.pop("ROUTE", None)
        config = ConfigManager._with_migrated_route(config)
        validate_route(config["ROUTE"], require_path=False)
        validate_run_parameters(config["RUNNING_SPEED_MPS"], config["INTERVAL_SECONDS"])
        return config

    @staticmethod
    def save_config(config_data: Mapping[str, Any], filename: str) -> bool:
        path = ConfigManager.path_for(filename)
        config = ConfigManager.prepare_for_save(config_data)
        ConfigManager.save_credentials(str(config.get("USER_ID", "")), str(config.get("COOKIE", "")), str(config.get("TENCENT_MAP_KEY", "")))
        ConfigManager._save_private_map_key(str(config.get("TENCENT_MAP_KEY", "")))
        config.pop("TENCENT_MAP_KEY", None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        return True
