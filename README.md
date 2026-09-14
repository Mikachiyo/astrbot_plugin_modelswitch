# astrbot_plugin_modelswitch (LLM 模型场景热切换 v2.0.0)

根据对话场景（日常闲聊、涩涩调情、复杂代码分析、安全审查风控规避等）让大模型**自主选择并热切换**最合适的 LLM 提供商。

---

## 🌟 核心特性与设计亮点

### 1. 动态 Tool Schema 编译（Zero-Hallucination 零幻觉）
- 插件摒弃了旧版硬编码别名的落后做法，改为**动态编译工具参数定义**；
- 在 Dashboard 中开启模型并填写场景备注后，`switch_model` 的参数表会自动热更新：
  - `provider_id` 的 `enum` 严格限定为**已开启**的提供商真实 ID；
  - `description` 直接动态包含小羊在后台填写的**场景使用建议**；
- 大模型在调用工具时，能清晰、确凿地看到各模型的优劣与适用场景，绝不凭空捏造不存在的模型别名。

### 2. 场景决策指南注入
- 在 `@filter.on_llm_request` 中，当可用模型大于 1 个时，自动将已开启模型的场景建议组合为结构化提示词注入到对话前缀；
- 引导大模型在察觉到任务变迁（如遇到写长代码、或察觉到内容审核即将截断）时主动调用工具切换。

### 3. 会话级（UMO）独立隔离热切换
- 调用 AstrBot 内核的 `provider_manager.set_provider(provider_id, ProviderType.CHAT_COMPLETION, umo=umo)`；
- 切换**只对当前会话生效**，不污染全局配置，不影响其它群聊和私聊。

### 4. 现代化管理后台（Dashboard）
- 内置 `pages/admin/index.html`，由 AstrBot 仪表盘自动挂载；
- 全量接入官方 `window.AstrBotPluginPage` Bridge SDK，安全无跨域；
- 打开即自动扫描当前 AstrBot 实例中所有配置好的 `CHAT_COMPLETION` 提供商；
- 提供常用场景预设快捷标签（“日常与涩涩首选”、“撞审查备选”、“深度推理与代码”等），支持单条保存与一键批量保存。

### 5. 游戏显存一键释放
- 保留 `unload_comfy` 工具，方便小羊玩游戏时随时一键击杀后台 ComfyUI 释放 VRAM。

---

## 🛠️ 工具清单

| 工具名 | 入参 | 功能说明 |
| :--- | :--- | :--- |
| `switch_model` | `provider_id` (动态 enum) | 根据场景自主切换当前会话的模型提供商 |
| `unload_comfy` | 无 | 释放显卡显存用于玩游戏（杀掉 ComfyUI 进程） |

---

## 🗄️ 数据存储与兼容性
- 数据库位于：`data/plugin_data/astrbot_plugin_modelswitch/modelswitch.db`；
- 符合 AstrBot 4.28.0 规范（复数形式 `context.add_llm_tools`、完整 `terminate` 生命周期、Quart 兼容路由）。
