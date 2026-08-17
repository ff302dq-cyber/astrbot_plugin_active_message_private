from __future__ import annotations

import json
from collections.abc import MutableMapping
from copy import deepcopy
from pathlib import Path
from typing import Any

PLUGIN_ID = "astrbot_plugin_activemessage"
PLUGIN_ID_ALIASES = (
    "astrbot_plugin_active_message_private",
    "astrbot_plugin_active_message",
)
CONFIG_MIGRATION_VERSION = 1

_STATE_MAPPING_KEYS = (
    "user_records",
    "idle_counters",
    "last_proactive_sent",
    "session_bot_ids",
)

_CURRENT_DEFAULTS: dict[str, Any] = {
    "decision_provider_id": "",
    "decision_making_config": {"enable_decision_making": True},
    "idle_check_config": {
        "check_interval_seconds": 200,
        "idle_trigger_min_minutes": 30,
        "idle_trigger_max_minutes": 80,
        "max_consecutive_messages": 3,
        "trigger_probability": 80,
        "new_topic_probability": 60,
        "cleanup_inactive_days": 10,
    },
    "blacklist_config": {
        "blacklist_enabled": False,
        "blacklist_users": [],
    },
    "group_chat_config": {
        "enable_group_active_message": False,
        "group_whitelist": [],
    },
    "quiet_hours_config": {
        "quiet_hours_enabled": True,
        "quiet_hours_start": 23,
        "quiet_hours_end": 7,
    },
    "logging_config": {
        "log_periodic_check": False,
        "log_decision_json": False,
    },
}


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8-sig") as file:
            value = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def state_path_candidates(canonical_state_path: Path) -> list[Path]:
    """Return known state locations used by old and accidentally nested builds."""
    canonical_state_path = canonical_state_path.resolve()
    canonical_dir = canonical_state_path.parent
    plugin_data_root = canonical_dir.parent
    candidates = [
        canonical_state_path,
        canonical_dir / PLUGIN_ID / "state.json",
    ]
    for alias in PLUGIN_ID_ALIASES:
        alias_dir = plugin_data_root / alias
        candidates.extend(
            [
                alias_dir / "state.json",
                alias_dir / alias / "state.json",
                alias_dir / PLUGIN_ID / "state.json",
            ]
        )

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def load_compatible_state(
    canonical_state_path: Path,
) -> tuple[dict[str, Any], list[Path]]:
    """Merge all valid legacy state files, with the newest file winning conflicts."""
    valid: list[tuple[float, Path, dict[str, Any]]] = []
    for path in state_path_candidates(canonical_state_path):
        data = _read_json_object(path)
        if data is None:
            continue
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            modified_at = 0.0
        valid.append((modified_at, path, data))

    merged: dict[str, Any] = {}
    for _modified_at, _path, data in sorted(valid, key=lambda item: item[0]):
        for key, value in data.items():
            if key in _STATE_MAPPING_KEYS and isinstance(value, dict):
                existing = merged.setdefault(key, {})
                if isinstance(existing, dict):
                    existing.update(value)
                else:
                    merged[key] = deepcopy(value)
            else:
                merged[key] = deepcopy(value)

    return merged, [path for _mtime, path, _data in valid]


def config_path_candidates(current_config_path: Path) -> list[Path]:
    config_dir = current_config_path.resolve().parent
    names = (PLUGIN_ID, *PLUGIN_ID_ALIASES)
    return [
        (config_dir / f"{name}_config.json").resolve()
        for name in names
        if (config_dir / f"{name}_config.json").resolve()
        != current_config_path.resolve()
    ]


def _copy_if_default(
    target: MutableMapping[str, Any],
    source: MutableMapping[str, Any],
    defaults: MutableMapping[str, Any],
    *,
    prefer_source: bool,
    preserve_existing_paths: set[str] | None = None,
) -> list[str]:
    changed: list[str] = []
    preserve_existing_paths = preserve_existing_paths or set()
    for key, default in defaults.items():
        if key not in source:
            continue
        source_value = source[key]
        if isinstance(default, dict):
            target_value = target.setdefault(key, {})
            if not isinstance(target_value, dict) or not isinstance(source_value, dict):
                continue
            for child_key, child_default in default.items():
                if child_key not in source_value:
                    continue
                field_path = f"{key}.{child_key}"
                if field_path in preserve_existing_paths and child_key in target_value:
                    continue
                if (
                    prefer_source
                    or target_value.get(child_key, child_default) == child_default
                ) and target_value.get(child_key) != source_value[child_key]:
                    target_value[child_key] = deepcopy(source_value[child_key])
                    changed.append(field_path)
        elif prefer_source or target.get(key, default) == default:
            if target.get(key) != source_value:
                target[key] = deepcopy(source_value)
                changed.append(key)
    return changed


def migrate_plugin_config(
    config: MutableMapping[str, Any],
    *,
    config_path: Path | None = None,
    first_deploy: bool | None = None,
) -> tuple[list[str], list[Path]]:
    """Import legacy config aliases and translate renamed fields once."""
    if config_path is None:
        raw_path = getattr(config, "config_path", None)
        config_path = Path(raw_path) if raw_path else None
    if first_deploy is None:
        first_deploy = bool(getattr(config, "first_deploy", False))

    compatibility = config.setdefault("compatibility_config", {})
    if not isinstance(compatibility, dict):
        compatibility = {}
        config["compatibility_config"] = compatibility
    migration_version = int(compatibility.get("migration_version", 0) or 0)
    if migration_version >= CONFIG_MIGRATION_VERSION:
        return [], []

    imported_paths: list[Path] = []
    sources: list[dict[str, Any]] = []
    if config_path is not None:
        valid_sources: list[tuple[float, Path, dict[str, Any]]] = []
        for path in config_path_candidates(config_path):
            data = _read_json_object(path)
            if data is None:
                continue
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                modified_at = 0.0
            valid_sources.append((modified_at, path, data))
        for _mtime, path, _data in sorted(valid_sources, key=lambda item: item[0]):
            imported_paths.append(path)
        if valid_sources:
            # One installation should have only one active config. If aliases coexist,
            # the newest valid file is the safest source of the user's last choices.
            sources.append(max(valid_sources, key=lambda item: item[0])[2])

    changed: list[str] = []
    for source in sources:
        changed.extend(
            _copy_if_default(
                config,
                source,
                _CURRENT_DEFAULTS,
                prefer_source=bool(first_deploy),
                # 当前面板里的免打扰选择拥有最高优先级。不能因为它恰好
                # 等于默认值，就被旧别名配置中的关闭状态重新覆盖。
                preserve_existing_paths={
                    "quiet_hours_config.quiet_hours_enabled",
                    "quiet_hours_config.quiet_hours_start",
                    "quiet_hours_config.quiet_hours_end",
                },
            )
        )

    legacy_sources = [*sources, dict(config)]
    for source in legacy_sources:
        legacy_group = source.get("group_chat_config", {})
        current_group = config.setdefault("group_chat_config", {})
        if isinstance(legacy_group, dict) and isinstance(current_group, dict):
            if (
                legacy_group.get("enable_in_group") is True
                and current_group.get("enable_group_active_message", False) is False
            ):
                current_group["enable_group_active_message"] = True
                changed.append("group_chat_config.enable_group_active_message")
            legacy_groups = legacy_group.get("group_whitelist")
            if (
                isinstance(legacy_groups, list)
                and legacy_groups
                and not current_group.get("group_whitelist")
            ):
                current_group["group_whitelist"] = deepcopy(legacy_groups)
                changed.append("group_chat_config.group_whitelist")

        legacy_logging = source.get("logging_config", {})
        current_logging = config.setdefault("logging_config", {})
        if (
            isinstance(legacy_logging, dict)
            and isinstance(current_logging, dict)
            and legacy_logging.get("log_periodic_checks") is True
            and current_logging.get("log_periodic_check", False) is False
        ):
            current_logging["log_periodic_check"] = True
            changed.append("logging_config.log_periodic_check")

    compatibility["migration_version"] = CONFIG_MIGRATION_VERSION
    save_config = getattr(config, "save_config", None)
    if callable(save_config):
        save_config()
    return list(dict.fromkeys(changed)), imported_paths
