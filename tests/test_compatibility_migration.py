from __future__ import annotations

import json
import os
from pathlib import Path

from astrbot_plugin_activemessage.compat import (
    PLUGIN_ID,
    load_compatible_state,
    migrate_plugin_config,
    state_path_candidates,
)
from astrbot_plugin_activemessage.core import ActiveMessageCore


class _Config(dict):
    def __init__(self, *args, config_path: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_path = str(config_path)
        self.saved = False

    def save_config(self):
        self.saved = True


class _Parent:
    def __init__(self, config):
        self.config = config
        self.context = object()


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_state_candidates_include_standard_and_accidental_nested_paths(tmp_path):
    canonical = tmp_path / PLUGIN_ID / "state.json"
    candidates = state_path_candidates(canonical)

    assert canonical.resolve() in candidates
    assert (tmp_path / PLUGIN_ID / PLUGIN_ID / "state.json").resolve() in candidates
    assert (
        tmp_path / "astrbot_plugin_active_message_private" / "state.json"
    ).resolve() in candidates


def test_legacy_state_files_are_merged_and_newest_value_wins(tmp_path):
    canonical = tmp_path / PLUGIN_ID / "state.json"
    nested = tmp_path / PLUGIN_ID / PLUGIN_ID / "state.json"
    alias = tmp_path / "astrbot_plugin_active_message_private" / "state.json"
    _write_json(canonical, {"user_records": {"private:1": {"value": "old"}}})
    _write_json(nested, {"user_records": {"private:2": {"value": "nested"}}})
    _write_json(alias, {"user_records": {"private:1": {"value": "new"}}})
    os.utime(canonical, (1, 1))
    os.utime(nested, (2, 2))
    os.utime(alias, (3, 3))

    data, sources = load_compatible_state(canonical)

    assert len(sources) == 3
    assert data["user_records"]["private:1"]["value"] == "new"
    assert data["user_records"]["private:2"]["value"] == "nested"


def test_old_directory_config_and_renamed_fields_are_migrated(tmp_path):
    current_path = tmp_path / "astrbot_plugin_active_message_private_config.json"
    old_path = tmp_path / f"{PLUGIN_ID}_config.json"
    _write_json(
        old_path,
        {
            "decision_provider_id": "expensive-provider",
            "idle_check_config": {
                "idle_trigger_min_minutes": 42,
                "idle_trigger_max_minutes": 99,
                "max_consecutive_messages": 2,
            },
            "group_chat_config": {
                "enable_in_group": True,
                "group_whitelist": [123456],
            },
            "quiet_hours_config": {
                "quiet_hours_enabled": False,
                "quiet_hours_start": 22,
                "quiet_hours_end": 8,
            },
            "logging_config": {"log_periodic_checks": True},
        },
    )
    config = _Config(
        {
            "decision_provider_id": "",
            "idle_check_config": {
                "idle_trigger_min_minutes": 30,
                "idle_trigger_max_minutes": 80,
                "max_consecutive_messages": 3,
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
            "compatibility_config": {"migration_version": 0},
        },
        config_path=current_path,
    )

    changed, imported = migrate_plugin_config(config)

    assert imported == [old_path.resolve()]
    assert config["decision_provider_id"] == "expensive-provider"
    assert config["idle_check_config"]["idle_trigger_min_minutes"] == 42
    assert config["group_chat_config"]["enable_group_active_message"] is True
    assert config["group_chat_config"]["group_whitelist"] == [123456]
    assert config["logging_config"]["log_periodic_check"] is True
    assert config["compatibility_config"]["migration_version"] == 1
    assert config.saved is True
    assert "group_chat_config.enable_group_active_message" in changed


def test_explicit_new_value_is_not_overwritten_by_old_default(tmp_path):
    current_path = tmp_path / "astrbot_plugin_active_message_private_config.json"
    old_path = tmp_path / f"{PLUGIN_ID}_config.json"
    _write_json(
        old_path,
        {"idle_check_config": {"idle_trigger_min_minutes": 42}},
    )
    config = _Config(
        {
            "idle_check_config": {"idle_trigger_min_minutes": 55},
            "compatibility_config": {"migration_version": 0},
        },
        config_path=current_path,
    )

    migrate_plugin_config(config)

    assert config["idle_check_config"]["idle_trigger_min_minutes"] == 55


def test_renamed_fields_in_the_same_config_file_are_migrated(tmp_path):
    current_path = tmp_path / f"{PLUGIN_ID}_config.json"
    config = _Config(
        {
            "group_chat_config": {
                "enable_group_active_message": False,
                "enable_in_group": True,
                "group_whitelist": [789],
            },
            "logging_config": {
                "log_periodic_check": False,
                "log_periodic_checks": True,
            },
            "compatibility_config": {"migration_version": 0},
        },
        config_path=current_path,
    )

    migrate_plugin_config(config)

    assert config["group_chat_config"]["enable_group_active_message"] is True
    assert config["group_chat_config"]["group_whitelist"] == [789]
    assert config["logging_config"]["log_periodic_check"] is True


def test_legacy_group_whitelist_is_enforced():
    core = ActiveMessageCore(
        _Parent(
            {
                "group_chat_config": {
                    "enable_group_active_message": True,
                    "group_whitelist": [123],
                },
                "blacklist_config": {"blacklist_enabled": False},
            }
        )
    )

    assert core._is_blocked("aiocqhttp:GroupMessage:123") is False
    assert core._is_blocked("aiocqhttp:GroupMessage:456") is True
