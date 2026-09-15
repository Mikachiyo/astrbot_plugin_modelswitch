"""astrbot_plugin_modelswitch - 模型场景热切换插件 (v2.0.0)

根据对话场景（如日常水群、深度代码分析、涩涩调情、撞安全审查风控等）
自主热切换会话所使用的 LLM 模型。

核心特性：
1. 动态 Tool Schema：根据已开启的模型列表和场景备注，实时编译 switch_model 的 enum 和 description；
2. 场景指南注入：在 system prompt 中注入当前可用模型及各自适用场景，引导模型自主决策；
3. 会话隔离热切：通过 provider_manager.set_provider(umo=umo) 实现当前对话单会话隔离切换；
4. 优雅管理后台：提供 pages/admin 面板，一键扫描已有模型、开关白名单、定制场景备注。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.provider.entities import ProviderType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from quart import jsonify, request

PLUGIN_NAME = "astrbot_plugin_modelswitch"
API_PREFIX = f"/{PLUGIN_NAME}"
PROMPT_MARKER = "【模型自主切换场景指南】"


# ---------------------------------------------------------------------------
# LLM 工具类
# ---------------------------------------------------------------------------
class SwitchModelTool(FunctionTool):
    """switch_model: 根据场景自主切换当前会话的 LLM 模型。"""

    def __init__(self, plugin: ModelSwitchPlugin) -> None:
        self.plugin = plugin
        super().__init__(
            name="switch_model",
            description=(
                "切换当前会话所使用的LLM模型。请根据当前的对话场景、任务需求或是否触发安全审查，"
                "自主选择最合适的模型进行热切换。切换仅对当前会话生效。"
            ),
            parameters=self._build_default_parameters(),
        )

    def _build_default_parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "string",
                    "description": "目标模型提供商ID。",
                }
            },
            "required": ["provider_id"],
        }

    def update_schema(self, enabled_models: list[dict[str, Any]]) -> None:
        """根据已启用的模型列表动态更新工具的参数 Schema。"""
        if not enabled_models:
            self.parameters = self._build_default_parameters()
            return

        enum_list = [m["provider_id"] for m in enabled_models]
        desc_lines = ["目标模型提供商ID。请根据以下各模型的适用场景按需切换："]
        for m in enabled_models:
            pid = m["provider_id"]
            scenario = m.get("scenario") or "通用对话模型"
            model_name = m.get("model_name") or pid
            desc_lines.append(f"• `{pid}` ({model_name}): {scenario}")

        self.parameters = {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "string",
                    "enum": enum_list,
                    "description": "\n".join(desc_lines),
                }
            },
            "required": ["provider_id"],
        }
        logger.info(f"[模型切换] switch_model 参数表已热更新，当前可用模型数: {len(enum_list)}")

    async def call(self, context, **kwargs) -> ToolExecResult:
        provider_id = str(kwargs.get("provider_id") or kwargs.get("model_alias") or "").strip()
        if not provider_id:
            return "❌ 切换失败：必须提供有效的 provider_id。"

        plugin = ModelSwitchPlugin._instance
        if not plugin:
            return "❌ 切换失败：插件实例未就绪。"

        # 检查是否在启用列表中
        enabled = await plugin.get_enabled_models()
        valid_ids = {m["provider_id"] for m in enabled}
        if valid_ids and provider_id not in valid_ids:
            return f"❌ 切换拒绝：模型 `{provider_id}` 未在已开启的允许列表中。可选模型: {', '.join(valid_ids)}"

        try:
            # 获取 AstrBot 内核的 provider_manager
            # 兼容不同 context 嵌套层级
            ctx_obj = getattr(context, "context", context)
            if hasattr(ctx_obj, "context"):
                ctx_obj = ctx_obj.context
            pm = getattr(ctx_obj, "provider_manager", None)
            if not pm:
                return "❌ 切换失败：未找到 provider_manager。"

            # 获取当前消息事件的 UMO
            event = getattr(context, "event", None) or getattr(getattr(context, "context", None), "event", None)
            umo = event.unified_msg_origin if event else None

            await pm.set_provider(
                provider_id=provider_id,
                provider_type=ProviderType.CHAT_COMPLETION,
                umo=umo,
            )
            model_info = next((m for m in enabled if m["provider_id"] == provider_id), None)
            scenario = f"（适用: {model_info['scenario']}）" if model_info and model_info.get("scenario") else ""
            return f"✅ 已成功将当前会话模型切换为：`{provider_id}` {scenario}，下一轮对话起生效。"
        except Exception as exc:
            logger.error(f"[模型切换] 切换模型失败: {exc}", exc_info=True)
            return f"❌ 切换失败：{exc}"


# ---------------------------------------------------------------------------
# 插件主类 Star
# ---------------------------------------------------------------------------
class ModelSwitchPlugin(Star):
    """模型场景热切换插件。"""

    _instance: ModelSwitchPlugin | None = None

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        ModelSwitchPlugin._instance = self

        data_dir = Path(get_astrbot_data_path())
        plugin_dir = data_dir / "plugin_data" / PLUGIN_NAME
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = plugin_dir / "modelswitch.db"
        self._db_lock = asyncio.Lock()

        # 初始化数据库
        self._init_db()

        # 创建并注册工具
        self.switch_tool = SwitchModelTool(self)
        context.add_llm_tools(self.switch_tool)

        # 注册 Web API
        self._register_web_apis()

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    async def initialize(self) -> bool:
        """插件初始化，扫描已有模型并刷新 Tool Schema。"""
        try:
            await self.sync_and_refresh()
            logger.info("[模型切换] 插件初始化成功，模型列表与参数表已就绪")
            return True
        except Exception as exc:
            logger.error(f"[模型切换] 初始化异常: {exc}", exc_info=True)
            return True

    # -----------------------------------------------------------------------
    # SQLite 数据库存储
    # -----------------------------------------------------------------------
    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS model_configs (
                    provider_id TEXT PRIMARY KEY,
                    model_name TEXT,
                    source_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    scenario TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    async def get_all_models(self) -> list[dict[str, Any]]:
        async with self._db_lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT * FROM model_configs ORDER BY enabled DESC, priority DESC, provider_id ASC")
                return [dict(r) for r in cur.fetchall()]

    async def get_enabled_models(self) -> list[dict[str, Any]]:
        async with self._db_lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT * FROM model_configs WHERE enabled = 1 ORDER BY priority DESC, provider_id ASC")
                return [dict(r) for r in cur.fetchall()]

    async def save_model_config(self, provider_id: str, enabled: int, scenario: str, priority: int = 0) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._db_lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE model_configs 
                    SET enabled = ?, scenario = ?, priority = ?, updated_at = ?
                    WHERE provider_id = ?
                    """,
                    (enabled, scenario, priority, now, provider_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
        if ok:
            # 实时热更新工具 Schema
            enabled_models = await self.get_enabled_models()
            self.switch_tool.update_schema(enabled_models)
        return ok

    # -----------------------------------------------------------------------
    # 动态扫描 AstrBot 已有 Chat 模型
    # -----------------------------------------------------------------------
    def scan_configured_providers(self) -> list[dict[str, str]]:
        """扫描所有已配置的对话聊天模型，排除 embedding 和 reranker 模型。"""
        results: list[dict[str, str]] = []
        seen_ids = set()

        # 读取 cmd_config.json 全量提供商列表
        try:
            cmd_cfg_path = Path(get_astrbot_data_path()) / "cmd_config.json"
            if cmd_cfg_path.exists():
                with open(cmd_cfg_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)

                # 获取所有 chat completion 类型的 source id
                chat_sources = set()
                for s in data.get("provider_sources", []):
                    stype = s.get("type", "")
                    if "chat" in stype or "openai" in stype or "zhipu" in stype or "deepseek" in stype:
                        chat_sources.add(s.get("id"))

                for p in data.get("provider", []):
                    if not isinstance(p, dict):
                        continue
                    pid = p.get("id")
                    model = p.get("model")
                    source = p.get("provider_source_id") or ""

                    # 过滤非聊天模型（排除无 model、或明确为 embedding / reranker 的条目）
                    if not pid or pid in seen_ids:
                        continue
                    pid_lower = pid.lower()
                    if "rerank" in pid_lower or "bge" in pid_lower or "embedding" in pid_lower:
                        continue
                    if not model and source not in chat_sources:
                        continue

                    results.append({
                        "provider_id": pid,
                        "model_name": str(model or pid),
                        "source_id": str(source or "默认"),
                    })
                    seen_ids.add(pid)
        except Exception as e:
            logger.error(f"[模型切换] 扫描配置文件异常: {e}", exc_info=True)

        return results

    async def sync_and_refresh(self) -> list[dict[str, Any]]:
        """将扫描到的提供商与本地数据库合并同步，并自动清理已从配置中删除的过期模型。"""
        scanned = self.scan_configured_providers()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if scanned:
            scanned_pids = {item["provider_id"] for item in scanned}
            async with self._db_lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    cur = conn.cursor()
                    for item in scanned:
                        pid = item["provider_id"]
                        cur.execute("SELECT provider_id FROM model_configs WHERE provider_id = ?", (pid,))
                        if not cur.fetchone():
                            # 新模型：默认关闭，插入新记录
                            cur.execute(
                                """
                                INSERT INTO model_configs (provider_id, model_name, source_id, enabled, scenario, priority, updated_at)
                                VALUES (?, ?, ?, 0, '', 0, ?)
                                """,
                                (pid, item["model_name"], item["source_id"], now),
                            )
                        else:
                            # 已存在：更新 model_name
                            cur.execute(
                                "UPDATE model_configs SET model_name = ?, source_id = ? WHERE provider_id = ?",
                                (item["model_name"], item["source_id"], pid),
                            )

                    # 自动清理已在实际配置中删除的幽灵模型
                    placeholders = ",".join("?" for _ in scanned_pids)
                    cur.execute(
                        f"DELETE FROM model_configs WHERE provider_id NOT IN ({placeholders})",
                        tuple(scanned_pids),
                    )
                    conn.commit()

        # 更新工具 Schema
        enabled_models = await self.get_enabled_models()
        self.switch_tool.update_schema(enabled_models)
        return await self.get_all_models()

    # -----------------------------------------------------------------------
    # 请求拦截：注入场景决策指南
    # -----------------------------------------------------------------------
    @filter.on_llm_request(priority=30)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """在请求前根据已开启模型列表注入场景决策指南。"""
        if not self._cfg("enable", True) or not self._cfg("enable_prompt_injection", True):
            return

        enabled = await self.get_enabled_models()
        if len(enabled) <= 1:
            # 只有一个或零个模型可用时，无需注入决策指南
            return

        # 构造场景指南
        lines = [
            "",
            PROMPT_MARKER,
            "你可以根据当前对话的场景变化、任务类型或风控审查情况，自主调用 `switch_model` 工具切换到最合适的模型：",
        ]
        for m in enabled:
            pid = m["provider_id"]
            scenario = m.get("scenario") or "通用对话"
            lines.append(f"- `{pid}`: {scenario}")
        lines.append("请在判断需要更换模型时主动调用 `switch_model(provider_id=...)`。切换后对后续对话立即生效。")
        lines.append("")

        guide_text = "\n".join(lines)

        # 检查是否已包含
        if req.system_prompt and PROMPT_MARKER not in req.system_prompt:
            req.system_prompt += f"\n{guide_text}"

    # -----------------------------------------------------------------------
    # Web API (由仪表盘 /api/plug/<插件名>/... 路由分发)
    # -----------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[模型切换] 当前 AstrBot 版本不支持 register_web_api，跳过注册")
            return

        register = self.context.register_web_api
        register(f"{API_PREFIX}/models", self.api_get_models, ["GET"], "获取并同步模型列表")
        register(f"{API_PREFIX}/models/save", self.api_save_model, ["POST"], "保存单个模型设置")
        register(f"{API_PREFIX}/models/batch_save", self.api_batch_save, ["POST"], "批量保存模型设置")

    async def api_get_models(self):
        try:
            models = await self.sync_and_refresh()
            return jsonify({"success": True, "models": models})
        except Exception as exc:
            logger.error(f"[模型切换] api_get_models 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_save_model(self):
        try:
            data = await request.get_json() or {}
            provider_id = str(data.get("provider_id", "")).strip()
            if not provider_id:
                return jsonify({"success": False, "error": "provider_id 不能为空"}), 400
            enabled = 1 if data.get("enabled") else 0
            scenario = str(data.get("scenario", "")).strip()
            priority = int(data.get("priority", 0))

            ok = await self.save_model_config(provider_id, enabled, scenario, priority)
            return jsonify({"success": ok, "message": "保存成功" if ok else "未找到该模型记录"})
        except Exception as exc:
            logger.error(f"[模型切换] api_save_model 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_batch_save(self):
        try:
            data = await request.get_json() or {}
            models_data = data.get("models", [])
            for item in models_data:
                pid = str(item.get("provider_id", "")).strip()
                if pid:
                    enabled = 1 if item.get("enabled") else 0
                    scenario = str(item.get("scenario", "")).strip()
                    priority = int(item.get("priority", 0))
                    await self.save_model_config(pid, enabled, scenario, priority)
            return jsonify({"success": True, "message": "批量保存成功"})
        except Exception as exc:
            logger.error(f"[模型切换] api_batch_save 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def terminate(self) -> None:
        """插件卸载生命周期钩子。"""
        if ModelSwitchPlugin._instance is self:
            ModelSwitchPlugin._instance = None
        logger.info("[模型切换] 插件已安全卸载")
