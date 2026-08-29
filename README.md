# JoyNiu NewCAD

JoyNiu NewCAD 是一个面向机械设计与制造协作的浏览器 CAD 工作台。它复刻了
CurrentCAD 线程中验证过的交互，并在 v0.2.0 增加了“上传图纸 → 证据确认 →
参数化三维 → PDM/CAM 交付”的可运行服务边界。

## 当前已实现

前端工作台仍可离线演示，同时通过 `src/api.js` 连接 FastAPI：

- AI 参数化零件 Agent：解析中文描述、编辑参数、同步特征树和三维预览。
- Three.js WebGL 三维查看器：优先加载 FastAPI 生成的真实 GLB 网格，支持 OrbitControls
  旋转/缩放、等轴/前/俯视相机、剖切平面和可见的参数化 fallback 状态。
- 三视图/2D 工程图、装配、标准件库、项目文件和导出交互。
- FastAPI 几何服务：图纸上传、参数校验、确定性网格预览，以及 STEP/GLB 导出。
- CadQuery/OCCT 适配器：安装可选依赖时生成 OCCT B-Rep STEP；未安装时返回
  明确标注的 faceted fallback，`productionReady=false`，不冒充生产实体。
- OCR/图纸证据服务：保存原图 SHA-256、尺寸值、单位、来源视图、置信度、证据框和
  review 状态；附带本次验收图的确定性 fixture，可选接入本机 Tesseract。
- PDM：SQLite 项目/文件/不可变版本仓储、内容校验、乐观修订号、回收与审计记录。
- 账号与权限：PBKDF2 密码哈希、HMAC Bearer token、viewer/designer/reviewer/
  manufacturing/admin RBAC 及账号审计。
- CAM/NC：工序和刀具 IR、确定性仿真预检查、碰撞/干涉/包络门禁、审核者审批、
  独立放行者和 NC 下载；审批仅授予 reviewer/admin，放行仅授予
  manufacturing/admin；预检查结果明确声明不能替代机床级材料去除仿真。

## 目录与服务入口

```text
apps/api/app/main.py              FastAPI 几何入口（/api 与 /api/v1）
apps/api/app/geometry.py          CadQuery/OCCT + 可审计 fallback 导出
apps/api/app/recognition.py       上传图纸识别兼容入口
apps/api/app/platform.py          SQLite PDM、RBAC、审计
apps/api/app/ocr.py               证据模型、fixture、Tesseract 适配器
apps/api/app/cam.py               CAM/NC、仿真和放行门禁
apps/api/app/platform_api.py      可挂载的平台 FastAPI router
apps/api/app/fixtures/            验收图纸证据 fixture
apps/api/scripts/acceptance_check.py 端到端离线验收脚本
```

平台路由同时挂载在 `/api/*` 和 `/api/v1/*`。几何公开入口包括：

| 能力 | 路径 |
| --- | --- |
| 健康检查 | `GET /health`、`GET /api/health`、`GET /api/v1/health` |
| 图纸上传 | `POST /api/v1/drawings/recognize`（multipart `file`） |
| 几何校验/生成 | `POST /api/v1/brackets/validate`、`POST /api/v1/brackets/generate` |
| STEP/GLB 下载 | `GET /api/v1/artifacts/{id}.{format}` |
| 认证 | `POST /api/v1/auth/users`、`POST /api/v1/auth/login`、`GET /api/v1/auth/me` |
| PDM | `/api/v1/pdm/projects`、`/api/v1/pdm/documents/{id}/versions` |
| OCR 证据 | `/api/v1/ocr/analyze`、`/api/v1/ocr/{recognitionId}/confirm` |
| CAM/NC | `/api/v1/cam/plans`、`/api/v1/cam/plans/{id}/simulate`/`approve`/`release`、`/api/v1/cam/nc/{id}` |

平台写操作使用 `Authorization: Bearer <token>`。首次创建账号是显式本地 bootstrap；
之后创建账号、角色变更和放行均需相应权限。

## 本地启动

### 浏览器工作台（离线/演示）

```bash
npm install
npm run dev
```

打开 <http://localhost:5173/>。若要连接后端，在启动 Vite 前设置：

```bash
VITE_API_BASE=http://localhost:8010/api/v1 npm run dev
```

### FastAPI 服务（推荐验收配置）

```bash
cd apps/api
python3 -m pip install -e '.[dev,geometry,ocr]'
export JOYNIU_AUTH_SECRET='use-a-random-secret-of-at-least-24-bytes'
export JOYNIU_DB="$PWD/data/joyniu.sqlite3"
uvicorn app.main:app --reload --port 8010
```

可选能力：

```bash
python3 -m pip install -e '.[geometry]'  # CadQuery/OCCT 原生 B-Rep STEP
python3 -m pip install -e '.[ocr]'       # Pillow + pytesseract；还需 tesseract binary
```

首次运行没有 CadQuery 时仍可生成可审计的 GLB/三角面 STEP；响应中的 `engine`、
`warnings` 和 `productionReady` 会说明降级状态。需要生产 B-Rep 交付时应使用
`requireCadQuery=true`，OCCT 不可用会明确返回 503，而不是静默降级。

## 图纸验收（本次客户图片）

验收 fixture `bracket_support_v1` 对应客户上传的四视图支架图，源图 SHA-256 为：

```text
ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366
```

脚本会核对 OCR 证据、参数配方、RBAC、PDM 原图/参数版本关联、CAM 仿真与 NC 放行，
并在安装了 API 依赖时继续核对几何导出：

```bash
# 直接使用上传文件（临时路径失效时可改为自己的 jpg/png）
python3 apps/api/scripts/acceptance_check.py \
  --drawing /path/to/codex-clipboard-6532c96b-cdb4-4481-8ffc-b1ad27ad26ac.jpg

# 没有图片文件时，使用显式 fixture 进行可重复 smoke check
python3 apps/api/scripts/acceptance_check.py --fixture bracket_support_v1

# 将可选依赖缺失视为失败（CI/发布门禁）
python3 apps/api/scripts/acceptance_check.py --strict-optional --drawing /path/to/drawing.jpg
```

验收尺寸为：底板 `100×50×10 mm`、上部实体 `70×50×30 mm`、总高 `40 mm`；右视图的
`30 mm` 是沿 Y 的矩形槽长，槽宽（X）`10 mm`、槽深 `10 mm`；R15 鞍形切口沿 Y
贯穿全宽 `50 mm`；两处竖直 `Ø20 mm` 是贯穿 Z=0..40 的切孔（与上部侧壁相切后
形成半圆侧缺口），中心距 `70 mm`、中心位于 `X=±35, Y=0`。`bossDiameter` /
`bossCenterDistance` 仅作为旧客户端兼容别名，不代表实体凸台。fixture 结果为
`status=confirmed`，但生产交付仍必须看
几何引擎的 `productionReady` 和实体校验报告。

## 测试与文件化开发

```bash
cd apps/api
python3 -m pytest
```

核心服务测试覆盖 PDM 不可变版本/陈旧写入、密码与 token、RBAC、确定性 OCR 证据、
CAM 碰撞门禁和审核者/放行者分离；API 集成测试在安装 FastAPI 开发依赖后自动运行。

- [Product-Spec.md](Product-Spec.md)：产品定位、图纸验收约束、能力矩阵和版本路线。
- [docs/CHANGELOG.md](docs/CHANGELOG.md)：版本变更、已知限制和升级说明。
- [apps/api/README_PLATFORM.md](apps/api/README_PLATFORM.md)：平台服务 API 与安全策略。

## 生产边界

当前仓库已经把 FastAPI、CadQuery/OCCT、证据优先 OCR、SQLite PDM、账号/RBAC 和
CAM/NC 工作流接入到同一条可复现链路：OCCT 模式会做实体拓扑、包络、体积、关键圆柱
轴线检查，并重新打开 STEP 做一致性复核；平台工作流会把原图、OCR 证据、参数、验证
报告和模型版本关联保存。缺少 OCCT 时的 fallback 仍只能审阅，不能标记为生产 B-Rep。

要部署为真正的制造生产系统，还需要把本地 SQLite/进程缓存替换为组织级数据库和对象
存储、接入集群任务队列与多租户审计、采用经过认证的机床后处理器，并运行机床专用材料
去除/碰撞仿真。未知图纸也会保持 `needs_review`，不会因为 OCR 猜测而自动放行。
