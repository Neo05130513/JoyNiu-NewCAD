# 变更记录

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
