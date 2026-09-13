# 商用 AI 调用边界（2026-09-10）

现有 `AIProxy.converse` 的旧版兼容流程会在一次 HTTP 对话内直接执行多阶段供应商调用和重试，流式连接断开后底层线程也可能继续工作。因此不能把一次 HTTP 对话当成一次供应商调用计费，也不能根据累加的 trace 制造逐次用量记录。

商用启用后，旧版 `/ai/conversation`、`/ai/conversation/stream`、`/ai/chat` 统一关闭，返回 HTTP 409，明确指引客户使用任务中心或 2D 转 3D 工作台。`/api` 和 `/api/v1` 两套前缀均需保护。拒绝发生在身份/权限验证之后，读取图纸、预处理以及供应商调用之前。没有实际供应商调用时不创建虚假 usage 记录。

主应用用 `create_platform_router(services, billing_provider=lambda: getattr(app.state, "billing_service", None))` 进行延迟注入，两次挂载都应传入，兼容先建平台路由后初始化账本的现有顺序。

以下任何情况都关闭旧版入口：

- `JOYNIU_BILLING_ENABLED` 显式开启；即使组合层尚未注入账本也拒绝。
- 账本实例声明 `enabled=True`，包括尚未完成计价配置的商业模式。
- 动态持久计费策略声明已启用。
- 计费配置查询失败或格式不正确，避免退回未计量通道。

非商业模式保留旧版兼容行为；既有图纸、模型、登录和 PDM 接口不受此边界变更影响。`GET /ai/status` 提供 `legacyConversationAvailable` 与中文 `legacyConversationNotice`，供客户端显示真实能力。

计费策略通过 `BillingService.charging_status_provider` 注入每次动态读取的 `{"enabled": bool, "configured": bool}`，不依赖某个进程保存的过期开关。配置异常时停止新购积分与扣积分；已发起订单的支付/退款回调继续核对入账。回调不得递归调用 `BillingService.status()`。

商用实际调用统一经 `cad_job_registry` / `MeteredCadProvider` 的逐调用持久记录。不修改已有 CAD provider；未知 token 继续为 `null`。是否收费、费率版本和余额不足处理规则由独立计费策略配置，未确认规则不启用。
