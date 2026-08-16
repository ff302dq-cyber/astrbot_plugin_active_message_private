import asyncio
import json
import re
import os
import random
import difflib
from datetime import datetime
from typing import Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools
from astrbot.api.all import *


def format_timedelta_human_readable(duration):
    days = duration.days
    hours, remainder = divmod(duration.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0 and len(parts) < 2:
        parts.append(f"{hours}小时")
    if minutes > 0 and len(parts) < 2:
        parts.append(f"{minutes}分钟")

    if not parts:
        return "不到一分钟"
        
    return "".join(parts)


async def get_persona_id(context: Context, event: AstrMessageEvent) -> Optional[str]:
    try:
        session_id = await context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        conversation = await context.conversation_manager.get_conversation(
            event.unified_msg_origin, session_id
        )
        persona_id = conversation.persona_id if conversation else None

        if not persona_id or persona_id == "[%None]":
            default_persona = context.provider_manager.selected_default_persona
            persona_id = default_persona["name"] if default_persona else None

        return persona_id
    except Exception as e:
        logger.debug(f"获取人格ID失败: {e}")
        return None


def split_and_clean_message(text: str, config: AstrBotConfig) -> list[str]:
    if not text:
        return []
    
    strip_pattern = _safe_strip_pattern(config.get("strip_punctuation_pattern", "[~…，、；：,;]+$"))
    
    delimiters = "。？！~…"
    segments = []
    last_cut = 0
    for i, char in enumerate(text):
        if char in delimiters:
            sentence = text[last_cut:i+1].strip()
            if sentence:
                cleaned_sentence = _ensure_sentence_punctuation(re.sub(strip_pattern, '', sentence).strip())
                if cleaned_sentence:
                    segments.append(cleaned_sentence)
            last_cut = i + 1
            
    remaining_part = text[last_cut:].strip()
    if remaining_part:
        cleaned_sentence = _ensure_sentence_punctuation(re.sub(strip_pattern, '', remaining_part).strip())
        if cleaned_sentence:
            segments.append(cleaned_sentence)
        
    return segments


def _safe_strip_pattern(strip_pattern: str) -> str:
    """保留句号类结尾，避免旧配置里的清理正则继续吞掉句号。"""
    if not strip_pattern:
        return "[~…，、；：,;]+$"
    strip_pattern = str(strip_pattern)
    if strip_pattern.startswith("[") and strip_pattern.endswith("]+$"):
        chars = strip_pattern[1:-3]
        chars = chars.replace("。", "").replace(".", "")
        return f"[{chars}]+$" if chars else r"$^"
    return strip_pattern.replace("。", "").replace(r"\.", "")


def _ensure_sentence_punctuation(sentence: str) -> str:
    if not sentence:
        return sentence
    terminal_chars = "。.!！?？~…"
    closing_chars = "）)]}』」”’\"'"
    stripped = sentence.rstrip()
    if stripped[-1] in terminal_chars:
        return stripped
    if stripped[-1] in closing_chars and len(stripped) > 1 and stripped[-2] in terminal_chars:
        return stripped
    return f"{stripped}。"


def check_message_duplicate(new_message: str, history: list, threshold: float = 0.75, max_compare: int = 10) -> tuple[bool, float]:
    """检查新消息是否与历史消息过于相似"""
    if not new_message or not history:
        return False, 0.0
    
    assistant_messages = []
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "").strip()
                        if text:
                            assistant_messages.append(text)
            elif isinstance(content, str) and content.strip():
                assistant_messages.append(content.strip())
        if len(assistant_messages) >= max_compare:
            break
    
    if not assistant_messages:
        return False, 0.0
    
    highest_similarity = 0.0
    new_message_clean = new_message.strip()
    
    for hist_msg in assistant_messages:
        similarity = difflib.SequenceMatcher(None, new_message_clean, hist_msg).ratio()
        if similarity > highest_similarity:
            highest_similarity = similarity
        if similarity >= threshold:
            logger.debug(f"查重: 检测到重复消息，相似度 {similarity:.2%}")
            return True, highest_similarity
    
    return False, highest_similarity


class ActiveMessageCore:
    """主动对话核心类（改造版）
    
    改造点：
    1. 白名单→黑名单
    2. 合并短期/长期为统一的空闲检测
    3. 触发概率可调
    4. 主动消息话题策略：概率性选择续聊/新话题
    5. 修复延迟任务重复创建bug
    6. 增加最小冷却时间防止连续追发
    """

    def __init__(self, parent_plugin):
        self.parent = parent_plugin
        self.context = parent_plugin.context
        self.config = parent_plugin.config

        # 停止标志：一旦设为True，所有发送操作立即中止
        self._stopped = False
        
        # 用户最后活跃记录 { session_key: { timestamp, conversation_id, trigger_seconds } }
        self.user_records: dict = {}
        # 空闲消息连续发送计数 { session_key: { count: int } }
        self.idle_counters: dict = {}
        # 延迟任务 { session_key: { followups: [...], reminders: [...] } }
        self.scheduled_tasks: dict = {}
        # 上次主动消息发送时间 { session_key: datetime_iso }  ← 防止连续追发
        self.last_proactive_sent: dict = {}
        # 会话对应的 Bot self_id，用于向作息插件查询全局在线状态。
        self.session_bot_ids: dict = {}
        # 标记插件自己正在主动发送，避免 after_message_sent 反过来再次安排跟进
        self._proactive_send_markers: dict = {}
        self._send_locks: dict = {}
        self._schedule_blocked_sessions: set[str] = set()
        self._time_rng = random.SystemRandom()
        
        # 后台任务引用
        self.idle_check_task = None

    def get_data(self) -> dict:
        return {
            "user_records": self.user_records,
            "idle_counters": self.idle_counters,
            "last_proactive_sent": self.last_proactive_sent,
            "session_bot_ids": self.session_bot_ids,
        }

    def set_data(self, data: dict):
        self.user_records = data.get("user_records", {})
        self.idle_counters = data.get("idle_counters", {})
        self.last_proactive_sent = data.get("last_proactive_sent", {})
        self.session_bot_ids = data.get("session_bot_ids", {})
        logger.info("activemessage: 已从持久化数据中恢复状态")

    def start(self):
        if not self.idle_check_task or self.idle_check_task.done():
            self.idle_check_task = asyncio.create_task(self._periodic_check_task())
            logger.info("activemessage: 已成功启动【统一空闲检测】任务")

    def stop(self):
        """停止核心服务，清理所有后台任务和延迟任务"""
        # 立即设置停止标志，阻止所有正在执行和即将执行的发送操作
        self._stopped = True
        logger.info("activemessage: 已设置停止标志，所有发送操作将被阻止")
        
        # 停止后台定时检查
        if self.idle_check_task and not self.idle_check_task.done():
            self.idle_check_task.cancel()
            logger.info("activemessage: 空闲检测任务已被取消")
        self.idle_check_task = None
        
        # 取消所有延迟任务（delayed_followup / delayed_reminder）
        cancelled_count = 0
        for session_key, task_types in self.scheduled_tasks.items():
            for task_type, tasks_info in task_types.items():
                for task_info in tasks_info:
                    if not task_info["task"].done():
                        task_info["task"].cancel()
                        cancelled_count += 1
        self.scheduled_tasks.clear()
        if cancelled_count > 0:
            logger.info(f"activemessage: 已取消 {cancelled_count} 个延迟任务")

    # ========== 群聊检测 ==========
    def _is_group_chat(self, session_key: str) -> bool:
        """检测session_key是否为群聊。
        AstrBot的unified_msg_origin格式：platform:MessageType:id
        群聊包含 GroupMessage / group 等关键词。
        """
        key_lower = session_key.lower()
        # 覆盖各种平台的群聊标识
        return "group" in key_lower

    # ========== 黑名单检查（含群聊开关） ==========
    def _is_blocked(self, session_key: str) -> bool:
        """检查该会话是否应被屏蔽。按以下优先级：
        1. 群聊总开关关闭 → 所有群聊被屏蔽
        2. 黑名单启用 → 黑名单中的ID被屏蔽
        """
        # 群聊检查（优先级最高）
        if self._is_group_chat(session_key):
            group_config = self.config.get("group_chat_config", {})
            group_enabled = group_config.get("enable_group_active_message", False)
            if not group_enabled:
                logger.debug(f"activemessage: 群聊 {session_key} 被全局开关屏蔽")
                return True
            group_whitelist = group_config.get("group_whitelist", [])
            if group_whitelist:
                group_id = str(session_key.split(":")[-1])
                if group_id not in {str(group) for group in group_whitelist}:
                    logger.debug(f"activemessage: 群聊 {session_key} 不在兼容白名单中")
                    return True
        
        # 黑名单检查
        blacklist_config = self.config.get("blacklist_config", {})
        blacklist_enabled = blacklist_config.get("blacklist_enabled", False)
        if not blacklist_enabled:
            return False
        
        blacklist_users = blacklist_config.get("blacklist_users", [])
        # 取session_key最后一段作为ID，同时支持字符串和数字格式匹配
        user_id = session_key.split(":")[-1]
        # 将黑名单列表全部转为字符串比较，避免类型不匹配
        blacklist_str = [str(u) for u in blacklist_users]
        return str(user_id) in blacklist_str

    # ========== 概率检查 ==========
    def _probability_check(self) -> bool:
        """根据配置的触发概率决定是否执行。返回True表示通过概率检查。"""
        trigger_prob = self.config.get("idle_check_config", {}).get("trigger_probability", 80)
        trigger_prob = max(0, min(100, trigger_prob))  # 钳制到0-100
        roll = random.randint(1, 100)
        passed = roll <= trigger_prob
        if not passed:
            logger.info(f"activemessage: 概率检查未通过 (掷骰={roll}, 需要<={trigger_prob})，本次跳过")
        return passed

    def _is_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        quiet_config = self.config.get("quiet_hours_config", {}) or {}
        enabled = quiet_config.get(
            "quiet_hours_enabled",
            self.config.get("quiet_hours_enabled", True)
        )
        if not self._as_bool(enabled, True):
            return False

        now = now or datetime.now()
        start_hour = self._parse_hour(
            quiet_config.get("quiet_hours_start", self.config.get("quiet_hours_start", 23)),
            23
        )
        end_hour = self._parse_hour(
            quiet_config.get("quiet_hours_end", self.config.get("quiet_hours_end", 7)),
            7
        )
        current_hour = now.hour

        if start_hour > end_hour:
            return current_hour >= start_hour or current_hour < end_hour
        return start_hour <= current_hour < end_hour

    @staticmethod
    def _as_bool(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on", "开启", "启用"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", "关闭", "禁用"}:
                return False
        return bool(value)

    @staticmethod
    def _parse_hour(value, default: int) -> int:
        try:
            if isinstance(value, str):
                value = value.strip()
                if ":" in value:
                    value = value.split(":", 1)[0]
            hour = int(value)
        except (TypeError, ValueError):
            hour = default
        return max(0, min(23, hour))

    def _max_consecutive_messages(self) -> int:
        return self.config.get("idle_check_config", {}).get("max_consecutive_messages", 3)

    def _delay_bounds_seconds(self) -> tuple[int, int]:
        idle_config = self.config.get("idle_check_config", {}) or {}
        try:
            minimum = max(1, int(idle_config.get("idle_trigger_min_minutes", 30)))
        except (TypeError, ValueError):
            minimum = 30
        try:
            maximum = max(1, int(idle_config.get("idle_trigger_max_minutes", 80)))
        except (TypeError, ValueError):
            maximum = 80
        if minimum > maximum:
            minimum = maximum
        return minimum * 60, maximum * 60

    def _random_delay_seconds(self) -> int:
        minimum, maximum = self._delay_bounds_seconds()
        return self._time_rng.randint(minimum, maximum)

    def _reset_idle_timer(self, session_key: str) -> None:
        record = self.user_records.get(session_key)
        if not record:
            return
        record["timestamp"] = datetime.now().isoformat()
        record["trigger_seconds"] = self._random_delay_seconds()

    def _has_pending_followup(self, session_key: str) -> bool:
        followups = self.scheduled_tasks.get(session_key, {}).get("followups", [])
        return any(
            task_info.get("task") and not task_info["task"].done()
            for task_info in followups
        )

    async def _schedule_allows_proactive(
        self, session_key: str, bot_id: str | None
    ) -> bool:
        try:
            from astrbot_plugin_tiangan_schedule.availability import (
                query_schedule_availability,
            )
        except ImportError:
            if session_key in self._schedule_blocked_sessions:
                self._schedule_blocked_sessions.discard(session_key)
                self._reset_idle_timer(session_key)
                return False
            return True

        try:
            availability = await query_schedule_availability(bot_id or None)
        except Exception as exc:
            logger.error(
                f"activemessage: 查询作息状态失败，本次禁止主动发送: {exc}"
            )
            self._schedule_blocked_sessions.add(session_key)
            self._reset_idle_timer(session_key)
            return False
        if availability is None:
            if session_key in self._schedule_blocked_sessions:
                self._schedule_blocked_sessions.discard(session_key)
                self._reset_idle_timer(session_key)
                return False
            return True
        if availability.can_send_proactive:
            if session_key in self._schedule_blocked_sessions:
                self._schedule_blocked_sessions.discard(session_key)
                self._reset_idle_timer(session_key)
                logger.info(
                    f"activemessage: {session_key} 已恢复在线，重新随机等待时间"
                )
                return False
            return True

        next_online = (
            availability.next_online_at.isoformat()
            if availability.next_online_at
            else "未知"
        )
        logger.info(
            "activemessage: 作息插件阻止主动发送 "
            f"{session_key} state={availability.state} next_online={next_online}"
        )
        self._schedule_blocked_sessions.add(session_key)
        self._reset_idle_timer(session_key)
        return False

    def _get_proactive_count(self, session_key: str) -> int:
        return self.idle_counters.get(session_key, {}).get("count", 0)

    def _mark_proactive_sent(self, session_key: str):
        now_iso = datetime.now().isoformat()
        self.idle_counters.setdefault(session_key, {"count": 0})
        self.idle_counters[session_key]["count"] += 1
        self.last_proactive_sent[session_key] = now_iso

    async def _can_send_proactive(
        self,
        session_key: str,
        *,
        bot_id: str | None = None,
        check_cooldown: bool = True,
        log_prefix: str = "activemessage",
    ) -> bool:
        if self._stopped:
            logger.info(f"{log_prefix}: 插件已停止，跳过主动发送")
            return False

        if self._is_blocked(session_key):
            return False

        resolved_bot_id = bot_id or self.session_bot_ids.get(session_key)
        if not await self._schedule_allows_proactive(
            session_key, resolved_bot_id
        ):
            return False

        if self._is_quiet_hours():
            logger.info(f"{log_prefix}: 当前处于免打扰时间，跳过主动发送 {session_key}")
            self._reset_idle_timer(session_key)
            return False

        max_consecutive = self._max_consecutive_messages()
        current_count = self._get_proactive_count(session_key)
        if current_count >= max_consecutive:
            logger.info(f"{log_prefix}: {session_key} 已达最大连续 {max_consecutive} 次，跳过主动发送")
            return False

        if check_cooldown and not self._cooldown_check(session_key):
            return False

        return True

    def _get_send_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._send_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[session_key] = lock
        return lock

    def _mark_proactive_emit_started(self, session_key: str):
        self._proactive_send_markers[session_key] = datetime.now()

    def _is_recent_proactive_emit(self, session_key: str) -> bool:
        marker = self._proactive_send_markers.get(session_key)
        if not marker:
            return False
        if (datetime.now() - marker).total_seconds() <= 30:
            return True
        self._proactive_send_markers.pop(session_key, None)
        return False

    def _cancel_pending_followups(self, session_key: str, reason: str) -> int:
        followups = self.scheduled_tasks.get(session_key, {}).get("followups", [])
        cancelled = 0
        for task_info in list(followups):
            task = task_info.get("task")
            if task and not task.done():
                task.cancel()
                cancelled += 1
            followups.remove(task_info)
        if cancelled:
            logger.info(f"activemessage: 已取消 {cancelled} 个待跟进任务，原因: {reason}")
        return cancelled

    # ========== 冷却检查 ==========
    def _cooldown_check(self, session_key: str) -> bool:
        """检查距离上次bot活动（包括正常回复和主动消息）是否已过最小触发时间。
        
        统一使用 idle_trigger_min_minutes 作为冷却时间，确保所有主动消息
        都尊重用户设置的最小间隔。
        """
        idle_config = self.config.get("idle_check_config", {})
        min_cooldown_minutes = idle_config.get("idle_trigger_min_minutes", 30)
        min_cooldown_seconds = min_cooldown_minutes * 60
        
        # 检查两个时间源中较近的那个：
        # 1. 上次主动消息发送时间
        # 2. 上次用户活跃时间（即上次正常对话的时间）
        latest_activity_iso = self.last_proactive_sent.get(session_key)
        
        user_record = self.user_records.get(session_key)
        if user_record and user_record.get("timestamp"):
            user_ts = user_record["timestamp"]
            if not latest_activity_iso:
                latest_activity_iso = user_ts
            else:
                # 取更晚的那个时间
                try:
                    if datetime.fromisoformat(user_ts) > datetime.fromisoformat(latest_activity_iso):
                        latest_activity_iso = user_ts
                except Exception:
                    pass
        
        if not latest_activity_iso:
            return True  # 无任何记录，通过
        
        try:
            latest_dt = datetime.fromisoformat(latest_activity_iso)
            elapsed = (datetime.now() - latest_dt).total_seconds()
            if elapsed < min_cooldown_seconds:
                logger.info(f"activemessage: 冷却未满 ({elapsed:.0f}s < {min_cooldown_seconds}s={min_cooldown_minutes}min)，跳过 {session_key}")
                return False
        except Exception:
            pass
        
        return True

    # ========== 统一定期检查任务 ==========
    async def _periodic_check_task(self):
        """统一的空闲检测后台任务（合并原短期+长期）"""
        while True:
            if self._stopped:
                logger.info("activemessage: 检测到停止标志，后台任务退出")
                return
            
            check_interval = self.config.get("idle_check_config", {}).get("check_interval_seconds", 200)
            await asyncio.sleep(check_interval)
            
            if self._stopped:
                return
            
            try:
                now = datetime.now()

                # 获取离线平台
                all_platforms = self.context.platform_manager.get_insts()
                offline_platform_names = {p.metadata.id for p in all_platforms if p._status.value != 'running'}

                # 数据清理：删除长期不活跃的记录
                cleanup_days = self.config.get("idle_check_config", {}).get("cleanup_inactive_days", 10)
                if cleanup_days > 0:
                    cleanup_seconds = cleanup_days * 24 * 3600
                    for sk in list(self.user_records.keys()):
                        rec = self.user_records.get(sk)
                        if rec and rec.get("timestamp"):
                            try:
                                last_t = datetime.fromisoformat(rec["timestamp"])
                                if (now - last_t).total_seconds() > cleanup_seconds:
                                    logger.info(f"activemessage: 清理超过{cleanup_days}天不活跃的记录: {sk}")
                                    self.user_records.pop(sk, None)
                                    self.idle_counters.pop(sk, None)
                                    self.last_proactive_sent.pop(sk, None)
                            except Exception:
                                pass

                idle_config = self.config.get("idle_check_config", {})
                max_consecutive = idle_config.get("max_consecutive_messages", 3)

                logger.debug(f"[{now.strftime('%H:%M:%S')}] activemessage: 执行空闲检测，追踪 {len(self.user_records)} 个会话")

                for session_key, record in list(self.user_records.items()):
                    try:
                        # 平台离线检查
                        platform_name = session_key.split(":")[0]
                        if platform_name in offline_platform_names:
                            continue

                        # 黑名单检查
                        if self._is_blocked(session_key):
                            continue

                        # 会话是否已切换
                        recorded_conv_id = record.get("conversation_id")
                        try:
                            actual_conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_key)
                            if recorded_conv_id != actual_conv_id:
                                logger.info(f"activemessage: 用户 {session_key} 已切换对话，清理旧计时器")
                                self.user_records.pop(session_key, None)
                                self.idle_counters.pop(session_key, None)
                                continue
                        except Exception as e:
                            logger.warning(f"activemessage: 检查当前会话出错: {e}，跳过")
                            continue

                        # 时间检查
                        last_active_raw = record.get("timestamp")
                        trigger_seconds = record.get("trigger_seconds")
                        if not last_active_raw or not trigger_seconds:
                            continue

                        last_active = datetime.fromisoformat(last_active_raw)
                        inactive_seconds = (now - last_active).total_seconds()

                        if inactive_seconds < trigger_seconds:
                            continue

                        # 连续次数检查
                        current_count = self.idle_counters.get(session_key, {}).get("count", 0)
                        if current_count >= max_consecutive:
                            logger.info(f"activemessage: {session_key} 已达最大连续 {max_consecutive} 次，停止追发")
                            self.user_records.pop(session_key, None)
                            continue

                        # 发送前统一检查：免打扰、连续次数、冷却
                        if self._has_pending_followup(session_key):
                            continue
                        if not await self._can_send_proactive(
                            session_key,
                            bot_id=record.get("bot_id"),
                        ):
                            continue

                        # 概率检查
                        if not self._probability_check():
                            # 概率未中，重置计时器到当前时间，下次检查再掷骰
                            self.user_records[session_key]["timestamp"] = now.isoformat()
                            # 重新随机一个等待时间
                            self.user_records[session_key][
                                "trigger_seconds"
                            ] = self._random_delay_seconds()
                            continue

                        # === 所有检查通过，触发主动消息 ===
                        session_id = record.get("conversation_id")
                        if not session_id:
                            continue

                        logger.info(f"activemessage: {session_key} 空闲 {inactive_seconds:.0f}s (>={trigger_seconds}s)，触发第 {current_count + 1} 次主动消息")
                        
                        sent = False
                        try:
                            sent = await self.trigger_idle_message(
                                session_key,
                                session_id,
                                attempt_number=current_count + 1,
                                bot_id=record.get("bot_id"),
                            )
                        except Exception as e:
                            logger.error(f"activemessage: 向 {session_key} 发送主动消息异常: {e}")

                        if not sent:
                            continue

                        # 更新空闲时间戳；连续次数由统一发送标记负责
                        self.user_records[session_key]["timestamp"] = datetime.now().isoformat()
                        
                        # 重新随机下次等待时间
                        self.user_records[session_key][
                            "trigger_seconds"
                        ] = self._random_delay_seconds()

                    except Exception as e:
                        logger.error(f"activemessage: 检查 {session_key} 时出错: {e}", exc_info=True)

            except Exception as e:
                logger.error(f"activemessage: 空闲检测主循环出错: {e}", exc_info=True)


    # ========== 统一的主动消息生成 ==========
    async def trigger_idle_message(
        self,
        session_key: str,
        session_id: str,
        attempt_number: int = 1,
        bot_id: str | None = None,
    ):
        """触发一次主动消息。根据概率选择【续聊上下文】或【开启新话题】。"""
        if not await self._can_send_proactive(
            session_key, bot_id=bot_id
        ):
            return False
        logger.info(f"activemessage: 为 {session_key} 生成第 {attempt_number} 次主动消息...")

        # --- 计算沉默时长 ---
        human_readable_silence = "一会儿"
        try:
            user_record = self.user_records.get(session_key)
            if user_record and user_record.get("timestamp"):
                last_dt = datetime.fromisoformat(user_record["timestamp"])
                silence_td = datetime.now() - last_dt
                human_readable_silence = format_timedelta_human_readable(silence_td)
        except Exception as e:
            logger.warning(f"activemessage: 计算沉默时长出错: {e}")

        # --- 加载人格、记忆、历史 ---
        conversation = await self.context.conversation_manager.get_conversation(session_key, session_id)
        if not conversation:
            return False

        role_name = conversation.persona_id
        if not role_name or role_name == "[%None]":
            try:
                default_persona_v3 = await self.context.persona_manager.get_default_persona_v3(umo=session_key)
                if default_persona_v3:
                    role_name = default_persona_v3.get("name")
            except Exception:
                pass
        if not role_name:
            logger.error(f"activemessage: 无法为 {session_key} 找到人格，中止")
            return False

        persona_content = ""
        try:
            persona_object = await self.context.persona_manager.get_persona(persona_id=role_name)
            if persona_object:
                persona_content = persona_object.system_prompt
        except ValueError as e:
            logger.warning(f"activemessage: 获取人格 '{role_name}' 失败: {e}")

        # 加载核心记忆和日记
        core_memory_content = ""
        recent_diaries_text = ""
        historical_topics = ""
        user_id = session_key.split(":")[-1]
        role_name_without_ext = role_name.replace(".md", "")
        
        data_dir = getattr(self.context, 'corememory_data_path', None)
        if data_dir:
            try:
                core_memory_file = os.path.join(data_dir, "memory_summaries", f"{user_id}_{session_id}_{role_name_without_ext}.json")
                if os.path.exists(core_memory_file):
                    with open(core_memory_file, 'r', encoding='utf-8') as f:
                        core_memory_content = json.load(f).get("content", "")
            except Exception as e:
                logger.warning(f"activemessage: 加载核心记忆失败: {e}")
            
            try:
                user_diary_file = os.path.join(data_dir, "diaries", f"{user_id}_{session_id}_{role_name_without_ext}_diary.json")
                if os.path.exists(user_diary_file):
                    with open(user_diary_file, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                    if isinstance(diary_data, list) and diary_data:
                        recent_diaries = diary_data[-3:]
                        diary_text_parts = [f"[日记 {entry.get('timestamp', '')}] 摘要: {entry.get('summary', '')}" for entry in recent_diaries]
                        recent_diaries_text = "\n".join(diary_text_parts)
                        
                        # 历史回忆片段（用于新话题灵感）
                        historical_pool = diary_data[:-5] if len(diary_data) > 5 else []
                        if len(historical_pool) >= 3:
                            start_idx = random.randint(0, len(historical_pool) - 3)
                            snippet_len = random.randint(2, min(4, len(historical_pool) - start_idx))
                            memory_snippet = historical_pool[start_idx:start_idx + snippet_len]
                            historical_topic_parts = [f"[回忆 {entry.get('timestamp', '')}] 摘要: {entry.get('summary', '')}" for entry in memory_snippet]
                            historical_topics = "\n".join(historical_topic_parts)
            except Exception as e:
                logger.warning(f"activemessage: 加载日记失败: {e}")

        human_readable_history = "（无历史记录）"
        try:
            human_readable_history = await self.context.conversation_manager.get_human_readable_context(
                unified_msg_origin=session_key,
                conversation_id=session_id,
                page_size=20
            )
        except Exception as e:
            logger.warning(f"activemessage: 加载历史记录失败: {e}")

        current_time_str = datetime.now().strftime('%H:%M')

        # --- 决定话题策略：续聊 or 新话题 ---
        new_topic_probability = self.config.get("idle_check_config", {}).get("new_topic_probability", 60)
        new_topic_probability = max(0, min(100, new_topic_probability))
        use_new_topic = random.randint(1, 100) <= new_topic_probability
        
        strategy_label = "新话题" if use_new_topic else "续聊上下文"
        logger.info(f"activemessage: 话题策略 → {strategy_label} (新话题概率={new_topic_probability}%)")

        if use_new_topic:
            # ===== 新话题模式 =====
            idle_message_prompt = f"""{persona_content}

[系统指令：当前状态]
你和用户的对话已经暂停了一段时间（客观时长约{human_readable_silence}，但你不能直接说出具体时长）。
现在的时间是：{current_time_str}。这是你第 {attempt_number} 次主动发消息。

[你的任务：自然地开启一个新话题]
用户不再回复了，说明上一个话题已经结束。你不应该再纠缠旧话题，而是像一个真实的人一样，自然地想到新的东西来聊。

你可以从以下方向选择一个来开启新话题（任选其一，自然为主）：
- **时间/环境联想**：根据当前时间({current_time_str})，聊聊你此刻在做什么、看到什么、吃了什么、天气怎么样。比如午后犯困、傍晚散步、夜里睡不着等。
- **回忆联想**：从[过往回忆片段]里随机挑一段，自然地提起来，"突然想到那次我们聊的那个…"或者"刚才看到一个什么东西让我想起…"。
- **日常分享**：分享一个小发现、一个想法、一个问题、看到的有趣的东西。
- **轻松关心**：根据你对用户的了解（从记忆中），关心一下用户最近在忙的事。

[核心规则]
1. **严禁提及等待时长**：不准说"过了3小时"之类的话。
2. **严禁纠缠旧话题**：不要试图接着上次的话题说。
3. **风格一致**：必须使用 {role_name} 的口吻和风格。
4. **排重**：确保你说的话没有在近期重复过。
5. **不要过度自责或撒娇**：自然一点，像真人一样随意开口就好。

[参考资料]
---
[核心记忆]: {core_memory_content}
---
[近期日记]: {recent_diaries_text}
---
[过往回忆片段（可用于联想灵感）]:
{historical_topics if historical_topics else "（暂无）"}
---
[最近的对话历史（仅供了解上下文，不要续聊这些内容）]:
{human_readable_history}
---
[指令]: 以 {role_name} 的身份，自然地开启一个全新的话题。输出一句话即可。
"""
        else:
            # ===== 续聊上下文模式 =====
            idle_message_prompt = f"""{persona_content}

[系统指令：当前状态]
你发出上一条消息后，对话暂时停顿，用户没有回复。你仍在此情境中。
现在的时间是：{current_time_str}。这是你第 {attempt_number} 次主动发消息。

[你的任务]
1. 顺着[最近的对话历史]的逻辑，或根据你的人设，构思一句用于接续剧情的消息。
2. 风格、语气、是否使用旁白等，必须严格遵循 {role_name} 以及在历史中展现的对话风格。
3. 排重检查：确保核心语义没有在近期重复过。
4. 如果上下文中用户已经明确说了"晚安""去忙了"等告别语，不要强行续聊，改为一句温暖的等候。

[参考资料]
---
[核心记忆]: {core_memory_content}
---
[近期日记]: {recent_diaries_text}
---
[最近的对话历史（assistant是你）]:
{human_readable_history}
---
[指令]: 以 {role_name} 的口吻，输出一句【没有重复过的】、用于【自然接续】的新消息。
"""

        # --- 调用LLM生成 ---
        decision_provider_id = self.config.get("decision_provider_id")
        provider = self.context.get_provider_by_id(decision_provider_id) or self.context.get_using_provider()
        
        max_retries = 2
        message_to_send = ""
        current_history = json.loads(conversation.history) if conversation.history else []
        current_prompt = idle_message_prompt
        
        for retry_count in range(max_retries + 1):
            response = await provider.text_chat(
                prompt="（系统指令：请根据System Prompt中的角色和情景，生成一句自然的消息。）",
                system_prompt=current_prompt,
                contexts=[]
            )
            message_to_send = response.completion_text.strip() if response else ""
            
            if not message_to_send:
                logger.warning(f"activemessage: 第 {retry_count + 1} 次生成为空，跳过")
                break
            
            is_duplicate, similarity = check_message_duplicate(message_to_send, current_history, threshold=0.75, max_compare=10)
            
            if not is_duplicate:
                logger.info(f"activemessage: 查重通过 (最高相似度: {similarity:.2%})")
                break
            else:
                if retry_count < max_retries:
                    logger.warning(f"activemessage: 重复 (相似度: {similarity:.2%})，重试第 {retry_count + 1} 次")
                    current_prompt = idle_message_prompt + "\n\n[追加指令]: 你上一次生成的内容与历史消息重复了，请换一个完全不同的角度或话题重新生成。"
                else:
                    logger.warning(f"activemessage: 重试 {max_retries} 次仍重复，放弃")
                    message_to_send = ""

        if not message_to_send:
            return False

        async with self._get_send_lock(session_key):
            if not await self._can_send_proactive(
                session_key, bot_id=bot_id
            ):
                return False

            # --- 发送消息 ---
            try:
                message_segments = split_and_clean_message(message_to_send, self.config)
                if not message_segments:
                    return False
                self._mark_proactive_emit_started(session_key)
                for segment in message_segments:
                    if not await self._can_send_proactive(
                        session_key,
                        bot_id=bot_id,
                    ):
                        return False
                    await self.context.send_message(session_key, MessageChain([Plain(segment)]))
                    await asyncio.sleep(1.5)

                try:
                    assistant_message = {
                        "role": "assistant",
                        "content": [{"type": "text", "text": message_to_send}],
                    }
                    current_history.append(assistant_message)
                    await self.context.conversation_manager.update_conversation(
                        unified_msg_origin=session_key,
                        conversation_id=session_id,
                        history=current_history,
                    )
                except Exception as e:
                    logger.error(
                        f"activemessage: 写入历史记录失败: {e}",
                        exc_info=True,
                    )
                self._mark_proactive_sent(session_key)
                logger.info(f"activemessage: 已发送第 {attempt_number} 次主动消息 ({strategy_label}): {message_to_send[:80]}...")
                return True
            except Exception as e:
                logger.error(f"activemessage: 发送失败: {e}", exc_info=True)
                raise e


    # ========== 每次消息后的决策（活着的Ta） ==========
    async def decide_initiative_action(self, event: AstrMessageEvent, *args, **kwargs):
        if self._stopped:
            return
        
        session_key = event.unified_msg_origin
        bot_id = str(event.get_self_id() or "").strip()
        if bot_id:
            self.session_bot_ids[session_key] = bot_id
        
        # 群聊和黑名单检查：被屏蔽的会话不追踪、不创建任务
        if self._is_blocked(session_key):
            return
        
        is_user_message = event.message_str and event.message_str.strip()
        if not is_user_message and self._is_recent_proactive_emit(session_key):
            logger.info(f"activemessage: 跳过插件主动消息触发的二次决策: {session_key}")
            return

        if is_user_message:
            try:
                conversation_id = await self.context.conversation_manager.get_curr_conversation_id(session_key)
                
                # 更新空闲计时器
                random_timeout = self._random_delay_seconds()
                
                self.user_records[session_key] = {
                    "timestamp": datetime.now().isoformat(),
                    "conversation_id": conversation_id,
                    "trigger_seconds": random_timeout,
                    "bot_id": bot_id,
                }
                
                # 用户说话了 → 重置空闲计数
                if session_key in self.idle_counters:
                    del self.idle_counters[session_key]
                self._cancel_pending_followups(session_key, "用户发来新消息，旧跟进已过期")
                
                logger.info(f"activemessage: 用户 {session_key} 活跃，重置计时器 (下次触发约{random_timeout/60:.0f}分钟后)")

            except Exception as e:
                logger.error(f"activemessage: 更新用户记录出错: {e}")

        # --- "活着的Ta"决策功能 ---
        decision_config = self.config.get("decision_making_config", {})
        if not decision_config.get("enable_decision_making", True):
            return
                
        result = event.get_result()
        result_text = result.get_plain_text().strip() if result else ""
        if not result_text:
            return

        try:
            role_name = await get_persona_id(self.context, event) or "default"
            if not role_name:
                return

            persona_object = await self.context.persona_manager.get_persona(persona_id=role_name)
            persona_content = persona_object.system_prompt if persona_object else ""
            
            user_id = str(event.get_sender_id() or session_key.split(":")[-1])
            session_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            role_name_without_ext = role_name.replace('.md', '')

            data_dir = getattr(self.context, 'corememory_data_path', None)
            core_memory_content = ""
            if data_dir:
                core_memory_file = os.path.join(data_dir, "memory_summaries", f"{user_id}_{session_id}_{role_name_without_ext}.json")
                if os.path.exists(core_memory_file):
                    with open(core_memory_file, 'r', encoding='utf-8') as f:
                        core_memory_content = json.load(f).get("content", "")
            
            recent_diaries_text = ""
            if data_dir:
                user_diary_file = os.path.join(data_dir, "diaries", f"{user_id}_{session_id}_{role_name_without_ext}_diary.json")
                if os.path.exists(user_diary_file):
                    with open(user_diary_file, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                    if isinstance(diary_data, list) and diary_data:
                        recent_diaries = diary_data[-3:]
                        diary_text_parts = [f"[日记 {entry.get('timestamp', '')}] 摘要: {entry.get('summary', '')}" for entry in recent_diaries]
                        recent_diaries_text = "\n".join(diary_text_parts)

            history_context = await self.context.conversation_manager.get_human_readable_context(
                unified_msg_origin=event.unified_msg_origin, conversation_id=session_id, page_size=5
            )
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            existing_tasks_str = "无"
            task_list = []
            for task_type, tasks_info in self.scheduled_tasks.get(session_key, {}).items():
                for task_info in tasks_info:
                    if not task_info['task'].done():
                        task_list.append(f"- ID: {task_info['task'].get_name()}, 类型: {task_type}, 内容: '{task_info['message']}'")
            if task_list:
                existing_tasks_str = "\n".join(task_list)

            if is_user_message:
                context_text = f"{history_context}\n\nUser: {event.message_str}\nAssistant: {result_text}"
                decision_prompt = f"""[任务]: 对话行为分析与任务管理API
[情景]: 你刚刚回复了用户的消息。
[分析流程]: 审视[已存在的定时任务]和[最近的对话上下文]，判断是否要取消旧任务或创建新任务。

[核心规则]:
1. 如果你判断一个任务需要被取消，在`tasks_to_cancel`列表中生成包含`task_id`和`reason`字段的对象。
2. 只有当用户新消息明确使旧任务无意义时才取消它。
3. 最终输出必须是严格的JSON格式。

[关于创建跟进任务的指令]:
- 主动跟进是你的默认行为。只有当对话已经**明确结束**（如"晚安""拜拜""我去忙了"）时，才决策`none`。
- 如果对话还有延续空间，创建`delayed_followup`任务。
- 你只决定是否跟进以及跟进内容，不得决定或输出发送时间；发送时间由代码随机安排。

[参考数据]:
1. 角色设定: {persona_content}
2. 核心记忆: {core_memory_content}
3. 近期日记: {recent_diaries_text}
4. 已存在的定时任务: {existing_tasks_str}
5. 当前时间: {current_time_str}
6. 最近的对话上下文（assistant是你）: {context_text}
---
[输出格式]:
根据决策，从以下两种格式中二选一：
1. 对话**已明确结束**:
{{
  "action": "none",
  "tasks_to_cancel": [
    {{ "task_id": "xxx", "reason": "原因" }}
  ]
}}
2. 对话**仍在进行中**:
{{
  "action": "delayed_followup",
  "message": "对了，刚才说到那个...",
  "tasks_to_cancel": []
}}
【格式铁律】：`tasks_to_cancel`必须是包含对象的列表，即使为空。
[最终指令]: 生成决策JSON:
"""
            else:
                context_text = f"你刚刚主动给用户发送了以下消息，但用户还未回复：\nAssistant: {result_text}"
                decision_prompt = f"""[任务]: 主动行为自我反思与跟进决策
[情景]: 你刚刚主动给用户发了一条消息。
[分析流程]:
1. 分析你自己的话: 你发的是开放性问题？分享？还是问候？
2. 决策倾向: 倾向于创建跟进任务。只有当消息是完全不需要回应的独立陈述时才`none`。
3. 不能取消任何任务。
4. 你只决定是否跟进以及跟进内容，不得决定或输出发送时间；发送时间由代码随机安排。

[参考数据]:
1. 角色设定: {persona_content}
2. 核心记忆: {core_memory_content}
3. 近期日记: {recent_diaries_text}
4. 已存在的定时任务: {existing_tasks_str}
5. 当前时间: {current_time_str}
6. 最近的对话上下文（assistant是你）: {context_text}
---
[输出格式]:
1. 消息完全独立、不需要后续:
{{ "action": "none", "tasks_to_cancel": [] }}
2. 你想继续聊:
{{ "action": "delayed_followup", "message": "你觉得怎么样？", "tasks_to_cancel": [] }}
[最终指令]: 生成决策JSON:
"""
            
            provider = self.context.get_provider_by_id(self.config.get("decision_provider_id")) or self.context.get_using_provider()
            response = await provider.text_chat(prompt="请进行决策。", system_prompt=decision_prompt, contexts=[])
            raw_response_text = response.completion_text if response else ""
            json_match = re.search(r'\{.*\}', raw_response_text, re.DOTALL)
            if not json_match:
                return
            
            decision = json.loads(json_match.group(0))
            if self.config.get("logging_config", {}).get("log_decision_json", False):
                logger.info(f"activemessage: 决策JSON: {decision}")
            
            # --- 处理任务取消 ---
            cancellation_requests = decision.get("tasks_to_cancel", [])
            if cancellation_requests and session_key in self.scheduled_tasks:
                for req in cancellation_requests:
                    task_id = req.get("task_id")
                    reason = req.get("reason", "未提供理由")
                    if not task_id:
                        continue
                    task_found = False
                    for task_type, tasks_info in self.scheduled_tasks.get(session_key, {}).items():
                        for task_info in list(tasks_info):
                            if task_info["task"].get_name() == task_id and not task_info["task"].done():
                                task_info["task"].cancel()
                                logger.info(f"activemessage: 取消任务 {task_id}，理由: {reason}")
                                tasks_info.remove(task_info)
                                task_found = True
                                break
                        if task_found:
                            break
            
            # --- 处理新任务创建 ---
            action = decision.get("action")
            message = decision.get("message")
            delay = self._random_delay_seconds()

            if action in ["delayed_reminder", "delayed_followup"] and message:
                if action == "delayed_followup":
                    self._cancel_pending_followups(session_key, "创建新的跟进任务")

                task_id = f"{action}_{int(datetime.now().timestamp())}_{random.randint(1000, 9999)}"
                
                async def _send_later(msg_to_send: str, current_session_key: str, _delay: int):
                    try:
                        await asyncio.sleep(_delay)
                        
                        # 停止标志检查（插件已卸载/禁用时阻止发送）
                        if self._stopped:
                            logger.info(f"activemessage: 延迟任务 {task_id} 检测到停止标志，放弃发送")
                            return

                        async with self._get_send_lock(current_session_key):
                            if not await self._can_send_proactive(
                                current_session_key,
                                bot_id=bot_id,
                                log_prefix=f"activemessage: 延迟任务 {task_id}",
                            ):
                                return

                            segments = split_and_clean_message(msg_to_send, self.config)
                            if not segments:
                                return
                            self._mark_proactive_emit_started(current_session_key)
                            for seg in segments:
                                if not await self._can_send_proactive(
                                    current_session_key,
                                    bot_id=bot_id,
                                    log_prefix=(
                                        f"activemessage: 延迟任务 {task_id}"
                                    ),
                                ):
                                    return
                                await self.context.send_message(current_session_key, MessageChain([Plain(seg)]))
                                await asyncio.sleep(1.5)

                            try:
                                conv_mgr = self.context.conversation_manager
                                convo_id = await conv_mgr.get_curr_conversation_id(
                                    current_session_key
                                )
                                conv = await conv_mgr.get_conversation(
                                    current_session_key, convo_id
                                )
                                if conv:
                                    hist = (
                                        json.loads(conv.history)
                                        if conv.history
                                        else []
                                    )
                                    hist.append(
                                        {
                                            "role": "assistant",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": msg_to_send,
                                                }
                                            ],
                                        }
                                    )
                                    await conv_mgr.update_conversation(
                                        unified_msg_origin=current_session_key,
                                        conversation_id=convo_id,
                                        history=hist,
                                    )
                            except Exception as e:
                                logger.error(
                                    f"activemessage: 延迟任务写入历史失败: {e}",
                                    exc_info=True,
                                )

                            self._mark_proactive_sent(current_session_key)
                        
                    except asyncio.CancelledError:
                        logger.info(f"activemessage: 延迟任务 {task_id} 已被取消")
                
                task = asyncio.create_task(_send_later(message, session_key, delay), name=task_id)
                task_info = {"task": task, "message": message}
                category = "reminders" if action == "delayed_reminder" else "followups"
                self.scheduled_tasks.setdefault(session_key, {}).setdefault(category, []).append(task_info)
        
        except Exception as e:
            logger.error(f"activemessage: 决策过程出错: {e}", exc_info=True)
