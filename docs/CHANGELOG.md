# 变更记录

## v0.4.0 · 2026-09-02 · 可持续交互的 CAD Copilot

### Added

- AI 设计助手升级为真正的多轮聊天线程：显式携带历史消息，不依赖中转站响应存储即可理解后续追问。
- 新增浏览器 SSE 对话通道，支持阶段状态、AI 文本增量、最终结构化 CAD 结果和安全错误事件。
- 聊天输入支持 Enter 发送、Shift+Enter 换行、随消息附加图纸、停止当前生成和新建对话。

### Changed

- AI 回复先在同一消息气泡中流式呈现，最终结果到达后再一次性应用白名单参数补丁；取消或失败不会提交半成品 CAD 修改。
- 新图纸继续进入人工确认，纯文字二次修改继续沿用参数校验、OCCT 重建与生产导出门禁。

### Verification

- 覆盖多轮历史输入、SSE 事件顺序、最终结果单次提交、取消不应用补丁以及浏览器连续对话交互。

## v0.3.3 · 2026-09-02 · 大模型唯一候选源与 Responses SSE 流式分析

### Changed

- 中转站调用统一发送 `stream=true` 并按 Responses SSE 事件消费结果；服务状态增加 `streaming=true`，中转站后台不再把图纸分析记录为非流式请求。
- 图纸重试改为两次真实的多模态模型调用：首次以 `high` 细节读取原图，兼容重试以 `low` 细节读取同一原图；每次拥有独立的网络超时，不再出现未实际发起第二次调用却显示“已重试”。
- 上传图纸的自动候选只接受远程模型 `parameterPatch`。OCR、哈希校准和几何解析继续保留证据，但不再合并、补齐或覆盖 `candidateParameters`。
- 移除图纸轮次的本地中文参数解析和校准配方替代路径；远程模型未返回的字段保持空白，由客户人工补全，不再使用模板默认值冒充 AI 数据。
- 视觉默认分辨率从 `1200/low` 调整为最大边 `4096/high`；兼容重试仍显式使用 `low`。

### Verification

- 新增 Responses SSE `delta/completed` 解析、上游 `stream=true`、高清→低细节同图重试和独立超时测试。
- 新增“本地校准完整但远程 patch 为空”“OCR 明确识别 999 但 AI patch 为空”回归测试，确认 API 候选保持为空。
- AI 代理测试 28 项通过，前端生产构建通过。

## v0.3.2 · 2026-09-02 · 候选参数默认值回填

### Added

- AI/OCR 只返回部分尺寸或没有可靠尺寸时，支架的 14 个必填字段会用完整、可编辑的模板默认候选补齐，不再显示一整页空白输入框。
- 参数候选增加字段级来源状态：图纸识别、AI 候选、模板默认和人工修改；分析摘要同步显示明确候选与模板默认的数量。

### Changed

- “确认数据”现在确认页面中可见的完整参数快照；模板默认值无需客户逐项重新输入，但仍必须经过一次显式确认和服务端几何校验。
- 模板默认候选只用于当前受支持的零件模板，不计入识别证据或置信度，也不会在确认前触发实体生成或生产导出。
- 远程 AI 空响应、不完整响应和瞬态 4xx/5xx 会在同一总超时内自动进行一次无历史、无存储的兼容重试；失败提示不再误称整个中转站不可用。
- 中转请求不再发送 `max_output_tokens`，也不再限制消息或模型状态的输入长度；输入上下文与输出长度完全交给所选模型和中转站。
- 本次远程分析降级时保留原始附件，工作台会显示“重新尝试 AI 分析”，并明确区分“中转站在线”和“本次请求已降级”。

### Verification

- 覆盖空 AI patch、部分 AI patch、人工覆盖默认值、确认前阻断以及完整快照确认五类回归场景。
- 覆盖空结构响应、不完整响应、503 重试、401 不重试、八位流图片 MIME 修复以及降级文案回归。

## v0.3.1 · 2026-08-31 · AI 候选分析与客户确认闭环

### Added

- 新增独立的 `POST /api/v1/drawings/{drawingId}/accept`（及兼容路径）：客户/设计师可提交当前编辑后的 `parameterOverrides`，服务端完成严格参数/几何校验并记录 `confirmationType`、`confirmedBy`、`confirmedAt`。
- AI 图纸响应增加 `candidateParameters` 候选包；未知或低置信图纸也会返回可编辑候选、来源、置信度、假设、特征和问题，不再只有“复核”状态。
- 设计工作台增加“AI 分析摘要”和明确的六阶段路径：上传 → AI 分析 → 确认数据 → 生成 3D → 二次修改 → 导出。

### Changed

- 上传图纸（包括哈希校准图）只完成分析，不再隐式生成实体；“确认数据”和“生成 3D”是两个独立按钮。
- `needs_review` 的产品语义改为“候选数据等待人工确认”，不再作为 reviewer-only 的终点；客户可在参数面板补全/修改后继续。
- 上传新图纸会立即重置流程轨和参数证据状态：上一版本只能作为明确标记的预览，不会让新图纸误显示为已分析、已确认或已生成。
- 多模态代理提示词要求模型分析所有可见视图和标注，返回能证明的完整候选字段；不确定项必须列入问题，候选不会被冒充为生产真值。

### Verification

- 后端全量测试、严格图纸验收和前端生产构建通过；unknown candidate → explicit accept → geometry hand-off 有回归覆盖。

### Known limitations

- 任意未知图纸仍需要人工确认其候选拓扑；如果模型/ OCR 无法形成足够字段，界面会要求补全，而不是猜测并生成错误实体。
- PDM 正式发布、CAM 审批和 NC 放行仍由 reviewer/manufacturing RBAC 控制；客户确认只解锁当前设计实体生成。

## v0.3.0 · 2026-08-30 · 客户工作流与 CurrentCAD 风格工作台

### Changed

- 将客户主路径收敛为单一六阶段工作流：输入/上传 → 识别尺寸 → 复核证据 → 生成实体 → 二次修改 → 导出交付；3D 工作台顶部固定显示当前阶段和下一步主操作。
- 首页增加首屏“上传图纸开始分析”主入口；3D 工作台上传改为先进入待处理队列，客户可补充意图后点击“开始 AI 分析”，避免选择文件后隐式覆盖对话内容。
- 证据摘要、模型上下文和交付状态在同一页呈现；未确认图纸、过期参数和非 OCCT 预览均使用明确的阻断/提示文案。
- 重复上传时旧实体会自动降级为“上一版本预览”，不会继续显示为当前可交付结果；未知/低置信识别的数值标为候选值，必须经 reviewer 对照原图确认。
- 合并导出入口为右上角“导出交付”菜单，集中提供 STEP、GLB、DXF 和参数 JSON，避免视口、检查器和项目卡重复出现同一下载动作。
- 重排导航层级：设计入口优先，高级 PDM / 账号 / CAM / NC 放入“高级”分组；侧栏和顶部导航在窄屏保持可用，不再显示竖排文字或隐藏全部工作区标签。

### Verification

- 视觉验收覆盖 1280×720、1024×768 和约 489px 窄屏：上传、步骤条、3D 视口、检查器和主操作均可定位。
- 保留 v0.2.1 的真实图纸链路：验收支架图经 OCR/证据确认后仍生成 CadQuery/OCCT STEP + GLB，并支持后续尺寸对话修改。

### Known limitations

- 任意未知图纸仍需显式人工确认；v0.3.1 将确认动作前移到客户设计工作台，不再要求跳转 reviewer 登录页面。
- 2D/装配高级工作台仍是独立能力，后续将继续把其模型上下文与当前项目版本完全绑定。

## v0.2.1 · 2026-08-30 · 多模态 AI Copilot

### Added

- 新增服务端 AI Copilot 代理：`POST /api/v1/ai/conversation` 与兼容别名
  `/api/v1/ai/chat`，以及不暴露密钥的 `GET /api/v1/ai/status`。对话可携带当前模型状态、上一轮 `previousResponseId`和多个附件。
- 支持图片、PDF、DXF、DWG 多模态输入。图片以 Responses `input_image`
  发送，PDF/DXF/DWG 以 `input_file` 发送；最多 4 个附件，单件上限 20 MiB。
- 默认使用 `gpt-5.6-sol` 与 `reasoning.effort=high`，输出仅包含说明、问题、审查状态和参数白名单内的 `parameterPatch`。通过白名单、类型、有限值和正数边界校验后才能更新 CAD 模型。
- 对验收支架的已确认图纸或明确尺寸修改，三维工作台可在补丁通过几何校验后自动请求 STEP + GLB，并更新参数面板、特征树和预览。
- 远端 AI 不可用时增加可解释的本地路径：哈希校准的验收图和匹配明确中文参数可继续应用；未知图纸仍保持人工复核。

### Security

- AI 供应商凭证仅从 API 服务端的 `JOYNIU_AI_API_KEY` 或
  `JOYNIU_AI_API_KEY_FILE` 读取（`JOYNIU_LLM_*` 为兼容配置），不进入前端 bundle、localStorage 或响应体。
- AI 对话默认需要 Bearer token 和 `ai:chat` 权限，仅 designer/admin 可用；匿名演示必须由本地部署显式开启。供应商原始错误不转发给浏览器。
- 本地匿名 AI 入口进一步限制为 loopback 或显式 development/local 环境；未知图纸的复核状态会跨后续纯文字对话保留，未注册的兼容识别 ID 不会作为可生成来源返回。
- 附件会传至部署所配置的远程 AI 端点；生产部署必须在数据分类、传输、保留和供应商合规完成后才能上传保密图纸。

### Changed

- AI 对话改为“参数补丁 → 几何校验 → 实体生成”的闭环；参数变更会将旧 STEP/GLB 标记为过期，失败或待复核时不展示为生产交付。
- 产品规格升级为 v0.2.1；原 v0.2.0 图纸几何验收尺寸、OCCT 生产边界、PDM/RBAC 和 CAM/NC 门禁保持不变。

### Verification

- Copilot 接口覆盖多轮 `previousResponseId`、模型状态传递、附件类型/大小限制、响应白名单和无效补丁拒绝。
- 验收图的哈希证据可与对话补丁合并；在 CadQuery/OCCT 环境中，通过校验后自动产生 `productionReady=true` 的 STEP 与 GLB。

### Known limitations

- 多模态 AI 仅是参数化轴类/支架编辑助手，不是任意图纸的通用 CAD 重建器。未知图纸不会自动确认或生成虚构几何。
- PDF/DXF/DWG 当前以文件输入转发，不保证原生 DWG 实体解析、所有 DXF entity 拓扑重建或多页 PDF 视图对齐。
- 没有 CadQuery/OCCT 时的 faceted 文件只能审阅；任何 AI 回复、GLB 或 fallback STEP 都不能替代 OCCT 回读、PDM 版本和 CAM 审批/放行。
- 远程模型的延迟、限流、成本、保留策略和图纸合规风险需由部署方自行评估；关闭 response 存储会限制多轮对话。

## v0.2.0 · 2026-08-29 · 图纸转三维与平台服务

### Added

- 将前端 viewport 从静态 SVG 投影升级为 Three.js WebGL 实体查看器：优先载入后端 GLB，
  支持鼠标旋转/滚轮缩放、等轴/前/俯视相机、剖切和网格/灯光；GLB 暂不可用时显示明确
  标识的参数化网格 fallback，不再把 SVG 当作真实三维实体。
- 新增 `apps/api` FastAPI 服务（`/api/*` 与 `/api/v1/*`），包含图纸上传、参数校验、
  STEP/GLB 生成、artifact 下载和健康检查。
- 新增 CadQuery/OCCT 几何适配器：安装 `.[geometry]` 时走原生 B-Rep STEP；无 OCCT
  时走确定性 faceted fallback，并在响应中公开 `engine`、`warnings`、
  `productionReady=false`。
- 新增证据优先的 OCR 服务：尺寸值、单位、来源视图、置信度、bbox、特征和未决项均
  可追溯；可选 Tesseract provider；未知图纸保持 `needs_review`。
- 登记客户四视图支架验收 fixture `bracket_support_v1`（兼容别名
  `acceptance_bracket`），源图 SHA-256：
  `ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366`。
  fixture 参数为底板 `100×50×10`、上部全宽 `70×50×30`、总高 `40`、U 槽
  `40/R15`（沿 Y 贯穿50）、矩形顶槽长/宽/深 `30/10/10`，以及两处 `Ø20`
  竖向贯穿切孔（中心距 `70 mm`，侧边显示为半圆缺口）。
- 新增 SQLite PDM：项目/文件、不可变版本、内容 SHA-256、乐观 revision、回收/恢复、
  manifest 和审计事件。
- 新增本地账号与 RBAC：PBKDF2 密码哈希、HMAC bearer token、viewer/designer/
  reviewer/manufacturing/admin 角色及账号审计。
- 新增 CAM/NC 服务：刀具和工序 IR、确定性仿真预检查、碰撞/干涉/包络门禁、审核者
  审批、独立制造角色放行和 NC 下载。
- 收紧 CAM RBAC：`reviewer/admin` 才能审批，`manufacturing/admin` 才能放行；CAM
  服务复用账号权限校验，且同一账号不能同时完成审核与放行。
- 新增 `apps/api/scripts/acceptance_check.py`：无外部依赖即可跑 PDM/RBAC/OCR/CAM
  核心验收；安装 API/geometry extra 后自动追加 FastAPI、几何校验和 STEP/GLB 检查。
- 新增服务层单元测试和 FastAPI 集成测试；新增 `README_PLATFORM.md` 平台使用说明。

### Changed

- 修正四视图支架验收配方的几何语义：上部实体为全宽 `70×50×30`（不再把右视图
  `30` 误作上部宽度）；`30` 表示沿 Y 的槽长，矩形顶槽为宽 X=`10`、深=`10`；
  R15 鞍形切口沿 Y 贯穿 `50`；Ø20 圆为 Z 向贯穿切孔/侧边半圆缺口。旧
  `boss*` 参数名保留为兼容别名，feature type 改为 `side_notch_cut_pair` /
  `vertical_through_hole_pair`。
- 产品规格升级为 v0.2.0，记录客户图纸验收尺寸、SHA-256 和六阶段审核流程。
- 前端 API client 增加健康检查、图纸识别、几何生成、认证、PDM、CAM 调用边界；离线
  localStorage 演示仍可独立运行。
- FastAPI 默认使用 `apps/api/data/joyniu.sqlite3` 保存本地 PDM/RBAC，`JOYNIU_DB` 可
  覆盖；`JOYNIU_AUTH_SECRET` 用于 token 签名。

### Verification

- 仓库虚拟环境执行验收脚本：12 项通过、0 项跳过、0 项失败；仅安装核心依赖时仍会
  将 FastAPI/CadQuery 标记为可选跳过，`--strict-optional` 可作为发布门禁。
- 精确客户图片上传命中 fixture，OCR 结果 `status=confirmed`、16 条尺寸证据和 5 个
  结构特征（含矩形顶槽与侧边切孔）；PDM 原图/参数版本关联、CAM 仿真、reviewer →
  manufacturing 放行链路通过。
- 安装依赖后使用 `cd apps/api && python -m pytest` 执行几何/API/平台测试；发布门禁使用
  `python scripts/acceptance_check.py --strict-optional --drawing <file>`。

### Known limitations

- 没有 `engine=cadquery-occt` 和 `productionReady=true` 的 STEP 只能用于审阅，不能
  作为生产 B-Rep 交付；CI 应安装 `[dev,geometry]` 并启用严格验收。
- OCR fixture 是可审计的校准 profile，不是通用视觉模型；非 fixture 图纸需要安装
  Tesseract 并由人工确认，复杂视图/公差/标题栏尚未自动建模。
- CAM `deterministic-precheck` 是流程门禁，不是机床级材料去除、刀具负载或碰撞仿真；
  生成的 NC 是设计草案，仍需组织批准的仿真和 postprocessor。
- PDM、账号和 CAM/NC 快照在配置 `JOYNIU_DB` 后可跨 API 重启恢复；直接几何 artifact
  下载缓存和 OCR 识别 hand-off map 仍是进程内的短期缓存，drawing-to-model 会把审计
  证据与生成物写入 PDM。对象存储、集群队列、组织级多租户 ACL、完整通用拓扑规则、
  机床级材料去除仿真和多人协作计划仍列入 v0.3/v1.0。

## v0.1.0 · 2026-08-29 · 浏览器工作台原型

### Added

- 创建 `JoyNiu-NewCAD` 独立开发分支，固化 CurrentCAD 深度体验证据与产品规格。
- 初始化 React + Vite 单页工作台，实现 AI 参数解析、轴类零件参数编辑、特征树、三维
  SVG 预览、2D 工程图、装配、标准件库、项目管理和导出交互。
- 使用 browser `localStorage` 保存当前项目与操作历史。

### Known limitations

- 几何由浏览器演示内核生成，STEP/DXF 为流程联调样例，不替代服务端 OCCT/CadQuery。
- 当时尚未接入服务端 OCR、账号/PDM、权限和 CAM；这些能力在 v0.2.0 增加了可运行的
  本地服务边界，但生产化仍需按上方限制部署。
