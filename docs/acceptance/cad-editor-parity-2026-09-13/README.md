# 三维编辑器验收材料

最终部署：2026-09-13，http://122.51.168.205/，API/Web 镜像后缀 r6。完整状态和能力边界见 [对齐验收记录](../../currentcad-editor-parity-2026-09-13.md)。以下资料来自合成模型和实际浏览器操作，不是 AI 生成成功率测试。

## 最终生产证据

- [发布状态与健康检查](production-final-release-verification.json)：completed、实际运行镜像、公网源快照、健康接口、维护关闭及合成账号停用。
- [候选镜像与校验和](release-r6-candidate-verification.json)：最终 API/Web/base/tools 镜像身份及 smoke 哈希。
- [Linux x86_64 候选检查](release-r6-linux-verification.json)：真实内核、维护探针、并发等待、数据副本和恢复验证。
- [最终公网浏览器验收](public-final-browser-verification.json)：快速完成、等待取消、无效圆角、r6 重开、双实体和窄屏。
- [浏览器真实提交响应](public-final-browser-commits.json)：包含先前 r4 部署暴露的拒绝请求和最终修复后记录，不能把其中旧失败误称为最终回归失败。
- [最终 STEP](public-final.step)：网页实际下载，独立读回有效、2 个实体，总体积约 7244.981334525807 mm³。
- [桌面实际截图](public-final-desktop.png)、[390 px 操作窗截图](public-final-narrow.png)。

## 可复核主流程

空草图绘 30×15 矩形并拉伸 6 → 点选顶面画 6×4 轮廓并添加高度 4 的凸台 → 鼠标选两边圆角 → 双击原草图将宽改 35 → 同文件新建第二实体 → 在预览运行中改圆角并立即完成 → 保存 r6 → 退出、重开、导出 STEP。等待时取消和无效圆角均不覆盖 r6。

最终公开验收账号已停用，临时凭证已清理。可从真实账号的模型结果点击“编辑模型”或“继续编辑”使用新工作区；验收文档不包含登录凭证。

## 本地和中间阶段证据

- `frontend-final-r5-verification.json`：最终源码 703 项前端回归及队列修复；r5 指源码验收阶段，线上镜像为使用相同源码的 r6。
- `preview-queue-browser-verification.json`：本地快速完成、取消等待、准确错误的真实浏览器验证。
- `frontend-transactions-step-verification.json`：前端操作转换到实际内核及 STEP 的事务验证。
- `rectangle-intent-verification.json`、`parametric-history-verification.json`、`browser-history-verification.json`：矩形约束、参数历史和早期 r1→r4 浏览器记录。
- `shell-mirror-browser-verification.json`、`shell-mirror.step`：选面抽壳与镜像。
- `entry-compound-browser-verification.json`、`entry-compound.step`：空草图入口、新建独立实体及后续编辑。
- `release-r4-local-verification.json`、`release-r5-local-verification.json` 和 `public-r4-*` 为历史阶段记录，不代替最终 r6 生产证据。

多轮廓草图、完整约束、任意扫掠/放样、完整曲面/PMI/PDM 等仍未完全对齐；仅有网格且无 CAD 历史的模型不冒称为可编辑参数实体。
