import asyncio
import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .compat import PLUGIN_ID, load_compatible_state, migrate_plugin_config
from .core import ActiveMessageCore


class InitiativeMessagePlugin(Star):
    """
    主动消息插件（改造版）
    改造点：白名单→黑名单、合并短期/长期、概率触发、话题策略、修复追发bug
    """
    _active_core: ActiveMessageCore | None = None

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_file = None

        migrated_fields, imported_config_paths = migrate_plugin_config(self.config)
        if imported_config_paths:
            logger.info(
                "主动消息插件：已检查旧配置文件: %s",
                ", ".join(str(path) for path in imported_config_paths),
            )
        if migrated_fields:
            logger.info(
                "主动消息插件：已迁移旧配置字段: %s",
                ", ".join(migrated_fields),
            )

        # 清理旧核心实例
        if InitiativeMessagePlugin._active_core is not None:
            logger.info("主动消息插件：检测到旧的核心实例，正在清理...")
            InitiativeMessagePlugin._active_core.stop()
            InitiativeMessagePlugin._active_core = None

        self.core = ActiveMessageCore(self)
        InitiativeMessagePlugin._active_core = self.core
        logger.info("主动消息插件：核心已创建，开始后台初始化...")
        
        asyncio.create_task(self._initialize_plugin())

    async def _initialize_plugin(self):
        try:
            # AstrBot 4.24 的 get_data_dir(name) 已返回插件专属目录，
            # 不能再拼一次插件名。
            plugin_data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
            plugin_data_dir.mkdir(parents=True, exist_ok=True)
            self.data_file = str(plugin_data_dir / "state.json")
            logger.info(f"主动消息插件状态文件: {self.data_file}")
            
            self._load_data()
            self.core.start()
            logger.info("主动消息插件：初始化完成。")
        except Exception as e:
            logger.error(f"主动消息插件初始化失败: {e}", exc_info=True)

    async def on_stop(self):
        logger.info("主动消息插件：正在卸载...")
        if hasattr(self, 'core'):
            self.core.stop()
        if InitiativeMessagePlugin._active_core is self.core:
            InitiativeMessagePlugin._active_core = None
        self._save_data()
        logger.info("主动消息插件：清理完成。")

    def _load_data(self):
        if self.data_file:
            try:
                data, source_paths = load_compatible_state(Path(self.data_file))
                if not source_paths:
                    return
                self.core.set_data(data)
                logger.info(
                    "主动消息插件：已加载并合并 %d 份历史状态: %s",
                    len(source_paths),
                    ", ".join(str(path) for path in source_paths),
                )
                # 统一回写到 AstrBot 4.24 的标准位置，旧文件保留作安全备份。
                self._save_data()
            except Exception as e:
                logger.error(f"主动消息插件：加载数据失败: {e}", exc_info=True)

    def _save_data(self):
        if not self.data_file:
            return
        try:
            data = self.core.get_data()
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            logger.info("主动消息插件：数据已保存。")
        except Exception as e:
            logger.error(f"主动消息插件：保存数据失败: {e}", exc_info=True)

    @filter.after_message_sent(priority=1)
    async def decide_initiative_action(self, event: AstrMessageEvent, *args, **kwargs):
        if hasattr(self, 'core'):
            await self.core.decide_initiative_action(event, *args, **kwargs)

    @filter.command("主动消息")
    async def test_initiative_message(self, event: AstrMessageEvent):
        """手动触发主动消息测试"""
        try:
            session_key = event.unified_msg_origin
            session_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    session_key
                )
            )

            if not session_id:
                await event.send(event.plain_result("无法获取当前会话ID，测试失败。"))
                return

            await event.send(
                event.plain_result(
                    f"正在为当前会话 (ID: {session_id}) 手动触发一次主动消息测试..."
                )
            )
            bot_id = str(event.get_self_id() or "").strip()
            if bot_id:
                self.core.session_bot_ids[session_key] = bot_id
            await self.core.trigger_idle_message(
                session_key,
                session_id,
                bot_id=bot_id,
            )
            
        except Exception as e:
            logger.error(f"手动测试主动消息时出错: {e}", exc_info=True)
            await event.send(event.plain_result(f"测试失败: {e}"))
