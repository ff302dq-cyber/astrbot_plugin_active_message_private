from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from astrbot_plugin_activemessage.core import ActiveMessageCore
from astrbot_plugin_tiangan_schedule.availability import (
    ScheduleAvailability,
    register_availability_provider,
    unregister_availability_provider,
)


class _Parent:
    def __init__(self, config):
        self.config = config
        self.context = object()


class _UpperBoundRandom:
    def randint(self, minimum, maximum):
        return maximum


def test_active_message_delay_is_chosen_by_code_within_bounds():
    core = ActiveMessageCore(
        _Parent(
            {
                "idle_check_config": {
                    "idle_trigger_min_minutes": 31,
                    "idle_trigger_max_minutes": 47,
                }
            }
        )
    )
    core._time_rng = _UpperBoundRandom()

    assert core._delay_bounds_seconds() == (31 * 60, 47 * 60)
    assert core._random_delay_seconds() == 47 * 60


def test_llm_no_longer_has_a_delay_seconds_output_contract():
    source = Path(__file__).parents[1].joinpath("core.py").read_text("utf-8")
    assert 'decision.get("delay_seconds")' not in source
    assert '"delay_seconds":' not in source


def test_schedule_sleep_blocks_and_defers_proactive_send():
    async def run():
        async def provider(bot_id):
            return ScheduleAvailability(
                bot_id=bot_id or "bot-1",
                state="SLEEPING",
                can_send_proactive=False,
                next_online_at=datetime.now() + timedelta(hours=7),
            )

        token = register_availability_provider(provider)
        try:
            core = ActiveMessageCore(
                _Parent(
                    {
                        "quiet_hours_config": {
                            "quiet_hours_enabled": False,
                        }
                    }
                )
            )
            old_timestamp = "2026-08-16T00:00:00"
            core.user_records["private:1"] = {
                "timestamp": old_timestamp,
                "trigger_seconds": 60,
                "bot_id": "bot-1",
            }

            allowed = await core._can_send_proactive(
                "private:1",
                bot_id="bot-1",
                check_cooldown=False,
            )

            assert allowed is False
            assert core.user_records["private:1"]["timestamp"] != old_timestamp
            assert core.user_records["private:1"]["trigger_seconds"] >= 30 * 60
        finally:
            unregister_availability_provider(token)

    asyncio.run(run())


def test_schedule_online_allows_send_when_quiet_hours_are_disabled():
    async def run():
        async def provider(bot_id):
            return ScheduleAvailability(
                bot_id=bot_id or "bot-1",
                state="ONLINE",
                can_send_proactive=True,
            )

        token = register_availability_provider(provider)
        try:
            core = ActiveMessageCore(
                _Parent(
                    {
                        "quiet_hours_config": {
                            "quiet_hours_enabled": False,
                        }
                    }
                )
            )
            assert await core._can_send_proactive(
                "private:1",
                bot_id="bot-1",
                check_cooldown=False,
            )
        finally:
            unregister_availability_provider(token)

    asyncio.run(run())


def test_first_check_after_waking_only_restarts_random_timer():
    async def run():
        state = {"value": "SLEEPING"}

        async def provider(bot_id):
            online = state["value"] == "ONLINE"
            return ScheduleAvailability(
                bot_id=bot_id or "bot-1",
                state=state["value"],
                can_send_proactive=online,
            )

        token = register_availability_provider(provider)
        try:
            core = ActiveMessageCore(
                _Parent(
                    {
                        "quiet_hours_config": {
                            "quiet_hours_enabled": False,
                        }
                    }
                )
            )
            core.user_records["private:1"] = {
                "timestamp": "2026-08-16T00:00:00",
                "trigger_seconds": 60,
                "bot_id": "bot-1",
            }

            assert not await core._can_send_proactive(
                "private:1", bot_id="bot-1", check_cooldown=False
            )
            state["value"] = "ONLINE"
            assert not await core._can_send_proactive(
                "private:1", bot_id="bot-1", check_cooldown=False
            )
            assert await core._can_send_proactive(
                "private:1", bot_id="bot-1", check_cooldown=False
            )
        finally:
            unregister_availability_provider(token)

    asyncio.run(run())
