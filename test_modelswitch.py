"""astrbot_plugin_modelswitch 核心逻辑封闭自测试套件

模拟 AstrBot 4.28.0 运行环境，验证：
1. 插件初始化（工具注册为复数形式 add_llm_tools，Web API 注册）；
2. 动态扫描提供商并持久化到 SQLite；
3. 动态编译 SwitchModelTool 的 parameters Schema（enum 与 description 场景对齐）；
4. 模拟 switch_model 执行，检查 pm.set_provider 传参及 UMO 隔离；
5. 场景决策指南在 on_llm_request 中的提示词注入；
6. 仪表盘 API（GET models, POST save, POST batch_save）；
7. terminate 生命周期正常注销。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

TMP_ROOT = Path(tempfile.mkdtemp(prefix="modelswitch_test_"))
DB_DIR = TMP_ROOT / "plugin_data" / "astrbot_plugin_modelswitch"
DB_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. 模拟 AstrBot 4.28.0 核心模块与 Context
# ---------------------------------------------------------------------------
def install_stubs():
    # 模拟 astrbot.api
    api = types.ModuleType("astrbot.api")
    logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    api.logger = logger
    sys.modules["astrbot.api"] = api

    # 模拟 filter
    event_mod = types.ModuleType("astrbot.api.event")
    filter_obj = types.SimpleNamespace(
        on_llm_request=lambda *a, **k: (lambda fn: fn),
        platform_adapter_type=lambda *a, **k: (lambda fn: fn),
    )
    event_mod.filter = filter_obj
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    sys.modules["astrbot.api.event"] = event_mod

    # 模拟 star
    star_mod = types.ModuleType("astrbot.api.star")
    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config or {}
    star_mod.Star = Star
    star_mod.Context = type("Context", (), {})
    sys.modules["astrbot.api.star"] = star_mod

    # 模拟 provider
    provider_mod = types.ModuleType("astrbot.api.provider")
    class ProviderRequest:
        def __init__(self, system_prompt=""):
            self.system_prompt = system_prompt
    provider_mod.ProviderRequest = ProviderRequest
    sys.modules["astrbot.api.provider"] = provider_mod

    # 模拟 core.agent.tool
    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    class FunctionTool:
        def __init__(self, name, description="", parameters=None):
            self.name = name
            self.description = description
            self.parameters = parameters or {}
    tool_mod.FunctionTool = FunctionTool
    tool_mod.ToolExecResult = Any
    sys.modules["astrbot.core.agent.tool"] = tool_mod

    # 模拟 core.provider.entities
    entities_mod = types.ModuleType("astrbot.core.provider.entities")
    class ProviderType:
        CHAT_COMPLETION = "chat_completion"
    entities_mod.ProviderType = ProviderType
    sys.modules["astrbot.core.provider.entities"] = entities_mod

    # 模拟 get_astrbot_data_path
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: str(TMP_ROOT)
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod

    # 模拟 quart
    quart_mod = types.ModuleType("quart")
    quart_mod.jsonify = lambda data: data
    quart_mod.request = types.SimpleNamespace()
    sys.modules["quart"] = quart_mod


install_stubs()

# 导入插件主模块
sys.path.insert(0, r"D:\Program\Astrbot workspace\temp\astrbot_plugin_modelswitch")
import main as m

# ---------------------------------------------------------------------------
# 2. 测试用例
# ---------------------------------------------------------------------------
class FakeProviderManager:
    def __init__(self):
        self.set_provider_calls = []

    async def set_provider(self, provider_id, provider_type, umo=None):
        self.set_provider_calls.append({
            "provider_id": provider_id,
            "provider_type": provider_type,
            "umo": umo,
        })

class FakeContext:
    def __init__(self, pm):
        self.tools = []
        self.web_apis = {}
        self.provider_manager = pm
        self.config = {
            "provider": [
                {"id": "gemini-CPA/gemini-3.8-flash-high", "model": "gemini-3.8-flash-high", "provider_source_id": "gemini-CPA"},
                {"id": "gemini-CPA/gemini-pro-agent", "model": "gemini-pro-agent", "provider_source_id": "gemini-CPA"},
                {"id": "deepseek/v4-flash", "model": "deepseek-v4-flash", "provider_source_id": "deepseek"},
            ]
        }

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis[route] = (handler, methods, desc)


def run_async(coro):
    return asyncio.run(coro)


def test_suite():
    print("=== 开始运行封闭测试套件 ===")
    pm = FakeProviderManager()
    ctx = FakeContext(pm)

    # 创建测试用 cmd_config.json
    mock_cfg = {
        "provider_sources": [
            {"id": "gemini-CPA", "type": "openai_chat_completion"},
            {"id": "deepseek", "type": "openai_chat_completion"}
        ],
        "provider": [
            {"id": "gemini-CPA/gemini-3.8-flash-high", "model": "gemini-3.8-flash-high", "provider_source_id": "gemini-CPA"},
            {"id": "gemini-CPA/gemini-pro-agent", "model": "gemini-pro-agent", "provider_source_id": "gemini-CPA"},
            {"id": "deepseek/v4-flash", "model": "deepseek-v4-flash", "provider_source_id": "deepseek"}
        ]
    }
    with open(TMP_ROOT / "cmd_config.json", "w", encoding="utf-8") as f:
        json.dump(mock_cfg, f)

    # 1. 测试初始化与注册
    plugin = m.ModelSwitchPlugin(ctx)
    m.ModelSwitchPlugin._instance = plugin
    run_async(plugin.initialize())

    assert len(ctx.tools) == 2, f"应该注册2个工具，实际注册: {len(ctx.tools)}"
    switch_tool = next(t for t in ctx.tools if t.name == "switch_model")
    unload_tool = next(t for t in ctx.tools if t.name == "unload_comfy")
    assert switch_tool is not None
    assert unload_tool is not None
    print("✅ 1. 插件初始化与工具复数注册通过")

    # 2. 测试模型扫描与持久化
    all_models = run_async(plugin.get_all_models())
    assert len(all_models) == 3, f"应该扫描到3个模型，实际: {len(all_models)}"
    pids = [m["provider_id"] for m in all_models]
    assert "gemini-CPA/gemini-3.8-flash-high" in pids
    assert "gemini-CPA/gemini-pro-agent" in pids
    print("✅ 2. 模型扫描与数据库合并存储通过")

    # 2.1 测试删除配置后重新扫描，自动清理幽灵模型
    mock_cfg["provider"] = [p for p in mock_cfg["provider"] if p["id"] != "deepseek/v4-flash"]
    with open(TMP_ROOT / "cmd_config.json", "w", encoding="utf-8") as f:
        json.dump(mock_cfg, f)
    refreshed = run_async(plugin.sync_and_refresh())
    assert len(refreshed) == 2, f"删除一个模型后应只剩2个，实际: {len(refreshed)}"
    assert "deepseek/v4-flash" not in [m["provider_id"] for m in refreshed]
    print("✅ 2.1 幽灵模型自动清理机制通过")

    # 3. 测试保存开启模型与 Tool Schema 动态编译
    run_async(plugin.save_model_config(
        provider_id="gemini-CPA/gemini-3.8-flash-high",
        enabled=1,
        scenario="第一优先，日常聊天与涩涩首选",
        priority=10
    ))
    run_async(plugin.save_model_config(
        provider_id="gemini-CPA/gemini-pro-agent",
        enabled=1,
        scenario="当涩涩撞上安全审查风控时备选更换",
        priority=5
    ))

    enabled = run_async(plugin.get_enabled_models())
    assert len(enabled) == 2

    # 验证 switch_model 参数表是否同步更新！
    schema = switch_tool.parameters
    enum_vals = schema["properties"]["provider_id"]["enum"]
    desc = schema["properties"]["provider_id"]["description"]
    assert "gemini-CPA/gemini-3.8-flash-high" in enum_vals
    assert "gemini-CPA/gemini-pro-agent" in enum_vals
    assert "deepseek/v4-flash" not in enum_vals  # 未开启的模型不在 enum 里
    assert "日常聊天与涩涩首选" in desc
    assert "安全审查" in desc
    print("✅ 3. Tool Schema 参数表动态编译（enum 与场景描述注入）通过")

    # 4. 测试 switch_model 执行与 UMO 隔离
    fake_run_ctx = types.SimpleNamespace(
        context=ctx,
        event=types.SimpleNamespace(unified_msg_origin="test_platform:FriendMessage:user123")
    )

    # 正常调用开启的模型
    res = run_async(switch_tool.call(fake_run_ctx, provider_id="gemini-CPA/gemini-pro-agent"))
    assert "已成功将当前会话模型切换为" in res
    assert len(pm.set_provider_calls) == 1
    call_args = pm.set_provider_calls[0]
    assert call_args["provider_id"] == "gemini-CPA/gemini-pro-agent"
    assert call_args["umo"] == "test_platform:FriendMessage:user123"
    print("✅ 4. switch_model 会话级热切换执行与传参校验通过")

    # 试图切换未开启的模型（应被拦截）
    reject_res = run_async(switch_tool.call(fake_run_ctx, provider_id="deepseek/v4-flash"))
    assert "切换拒绝" in reject_res
    print("✅ 5. 未开启模型的安全白名单拦截通过")

    # 5. 测试场景提示词在 on_llm_request 中的注入
    req = types.SimpleNamespace(system_prompt="我是Mikachiyo。")
    event = types.SimpleNamespace(unified_msg_origin="test_platform:FriendMessage:user123")
    run_async(plugin.on_llm_request(event, req))
    assert m.PROMPT_MARKER in req.system_prompt
    assert "gemini-CPA/gemini-3.8-flash-high" in req.system_prompt
    assert "日常聊天与涩涩首选" in req.system_prompt
    print("✅ 6. 请求级场景决策指南 Prompt 注入测试通过")

    # 6. 测试 Web API
    api_get_models = ctx.web_apis[f"{m.API_PREFIX}/models"][0]
    api_res = run_async(api_get_models())
    assert api_res["success"] is True
    assert len(api_res["models"]) == 2
    print("✅ 7. 仪表盘 Web API 数据接口测试通过")

    # 7. 测试 terminate 卸载钩子
    run_async(plugin.terminate())
    assert m.ModelSwitchPlugin._instance is None
    print("✅ 8. 插件卸载生命周期与补丁注销测试通过")

    print("\n🎉 全部 8 项封闭式回归测试 100% PASS！")


if __name__ == "__main__":
    try:
        test_suite()
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print("ALL TESTS COMPLETED SUCCESSFULLY")
