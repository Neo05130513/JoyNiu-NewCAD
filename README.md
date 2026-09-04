# JoyNiu NewCAD

JoyNiu NewCAD 是一个面向机械设计与制造协作的浏览器 CAD 工作台。它复刻了
CurrentCAD 线程中验证过的交互，并在 v0.5.0 将“工作台 AI 对话上传 → 零件类型/参数候选 →
人工确认 → 白名单配方验证 → CadQuery/OCCT STEP + GLB → PDM/CAM 交付”接成可运行闭环。

## 当前已实现

前端工作台仍可离线演示，同时通过 `src/api.js` 连接 FastAPI：

- AI 参数化零件 Agent：解析中文描述、编辑参数、同步特征树和三维预览。
- 多模态 AI Copilot：在设计工作台对话框上传图片、PDF、DXF 或 DWG，携带当前模型状态进行
  多轮尺寸编辑；DWG 先在服务端转换为 DXF、提取矢量/尺寸并渲染高清图，原始 DWG 二进制
  不直接发送给中转站。服务端通过 GPTX Responses 兼容端点调用
  `gpt-5.6-sol` / `high`，并只把白名单参数补丁交给 CAD 编辑器。结果可携带
  `partType`、`recipeId` 和 `parameterEvidence`，工作台据此切换正确的参数面板、特征树与三维草稿。
- Three.js WebGL 三维查看器：优先加载 FastAPI 生成的真实 GLB 网格，支持 OrbitControls
  旋转/缩放、等轴/前/俯视相机、剖切平面和可见的参数化 fallback 状态。
- 三视图/2D 工程图、装配、标准件库、项目文件和导出交互。
- FastAPI 几何服务：图纸上传、人工确认、通用配方校验/生成、确定性网格预览，以及 STEP/GLB 导出。
- 参数配方注册表：当前支持兼容安装支架 `bracket_support_v1` 和开口夹紧座
  `split_clamp_support_v1`。新配方使用专用校验和建模器，不将开口夹紧座套入旧支架参数。
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
apps/api/app/model_recipes.py     零件类型/配方白名单与通用调度
apps/api/app/split_clamp_support.py 开口夹紧座建模、校验、STEP/GLB
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
| 通用几何校验/生成 | `POST /api/v1/models/validate`、`POST /api/v1/models/generate` |
| 旧支架兼容入口 | `POST /api/v1/brackets/validate`、`POST /api/v1/brackets/generate` |
| STEP/GLB 下载 | `GET /api/v1/artifacts/{id}.{format}` |
| 认证 | `POST /api/v1/auth/users`、`POST /api/v1/auth/login`、`GET /api/v1/auth/me` |
| AI Copilot | `GET /api/v1/ai/status`、`POST /api/v1/ai/conversation`、`POST /api/v1/ai/chat` |
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

打开 <http://localhost:5173/>。若要连接后端，在启动 Vite 前设置（下面以 8011/5175 为本地联调端口）：

```bash
VITE_API_BASE=http://127.0.0.1:8011/api/v1 npm run dev -- --port 5175
```

### FastAPI 服务（推荐验收配置）

```bash
cd apps/api
python3 -m pip install -e '.[dev,geometry,ocr,dwg]'
export JOYNIU_AUTH_SECRET='use-a-random-secret-of-at-least-24-bytes'
export JOYNIU_DB="$PWD/data/joyniu.sqlite3"
# AI key stays on the API process; point this at a restrictive local file or
# set JOYNIU_AI_API_KEY in the server environment (never VITE_*).
export JOYNIU_AI_API_KEY_FILE='/absolute/path/to/provider-key.md'
export JOYNIU_LLM_BASE_URL='https://gptx.shop/v1'
export JOYNIU_LLM_MODEL='gpt-5.6-sol'
export JOYNIU_LLM_REASONING_EFFORT='high'
# Local-only guest testing. Keep this 0 in a shared/production deployment.
export JOYNIU_AI_ALLOW_ANONYMOUS='1'
uvicorn app.main:app --reload --port 8011
```

The relay configuration follows the [GPTX integration guide](https://gptx.shop/docs/).
The API accepts a plain key file, JSON auth file, or a Markdown code block and extracts only
the credential token; the file contents are never returned to the browser.
图纸分析请求使用 Responses SSE 流式协议：首轮以 `high` 细节读取原图，兼容重试以
`low` 细节读取同一原图。上传轮次只有远程模型 `parameterPatch` 会进入候选参数；
OCR/几何结果仅作为人工复核证据，模型未返回的字段保持空白。

可选能力：

```bash
python3 -m pip install -e '.[geometry]'  # CadQuery/OCCT 原生 B-Rep STEP
python3 -m pip install -e '.[ocr]'       # Pillow + pytesseract；还需 tesseract binary
python3 -m pip install -e '.[dwg]'       # ezdxf + Matplotlib + Pillow；还需系统级 DWG 转换器
```

首次运行没有 CadQuery 时仍可生成可审计的 GLB/三角面 STEP；响应中的 `engine`、
`warnings` 和 `productionReady` 会说明降级状态。需要生产 B-Rep 交付时应使用
`requireCadQuery=true`，OCCT 不可用会明确返回 503，而不是静默降级。

### DWG 原生矢量预处理

`dwg` extra 只负责读取转换后的 DXF、提取 `DIMENSION`/基础矢量图元并生成高清预览；
`ezdxf` 本身不能直接解码 DWG。因此部署机器还必须安装以下任一种转换器：

- GNU LibreDWG：确保 `dwgread`（优先）或 `dwg2dxf` 在 `PATH`。macOS/Homebrew
  通常可用 `brew install libredwg`；Linux 包名依发行版而定，常见为
  `libredwg-tools`，也可从 GNU LibreDWG 源码构建。
- ODA File Converter / Drawings SDK：按 ODA 授权安装，并提供一个由管理员维护的包装器，
  接收“输入 DWG 文件、输出 DXF 文件”两个参数；应用不会拼接或执行 shell 字符串。

默认会依次发现 `dwgread`、`dwg2dxf`。需要指定固定版本或 ODA 包装器时，配置 JSON argv
数组；仅支持 `{input_dwg}` 和 `{output_dxf}` 占位符：

```bash
# GNU LibreDWG
export JOYNIU_DWG_CONVERTER_COMMAND_JSON='["/usr/local/bin/dwgread","-O","DXF","-o","{output_dxf}","{input_dwg}"]'

# ODA 包装器（必须生成 {output_dxf}）
export JOYNIU_DWG_CONVERTER_COMMAND_JSON='["/opt/joyniu/bin/oda-dwg-to-dxf","{input_dwg}","{output_dxf}"]'
```

该变量必须是非空字符串数组并包含 `{input_dwg}`；生产环境建议使用转换器的绝对路径。
转换命令不经过 shell，在隔离临时目录中执行，并受超时、文件大小和进程资源限制。
原始 DWG 不会直接上传中转站；远程 AI 只接收高清渲染图和受大小/字段白名单限制的结构化摘要。

DWG 矢量层可以提供两类可追溯数据：原生 `DIMENSION` measurement，以及根据端点、圆心、
半径计算的几何量测。但二维尺寸无法唯一决定任意三维拓扑：视图对应与特征语义仍须由 AI
或人工映射到已审核的配方/特征树，经人工确认后才能进入 CadQuery/OCCT 实体生成。

可以不经过 AI，先单独验证本机 DWG 读取链路：

```bash
curl -F 'file=@/absolute/path/drawing.dwg;type=application/acad' \
  http://127.0.0.1:8011/api/v1/dwg/inspect
```

响应包含 `source.signature/version`、`units`、`sourceEntityCount`、`dimensionCount`、
`vectorSummary`、派生 DXF/PNG 哈希以及 `rawDwgSentToAI=false`。

用户提供的 `1(1).dwg` 已有专用白名单配方
`stepped_tapered_nozzle_with_insert_v1`：左侧 98 mm 主件与右侧 Ø39.4×40 M12 镶件保持为
两个独立实体；M12 因缺少螺距/公差只显示名义直孔，不会伪造真实牙型。

## 图纸验收：安装支架（`bracket_support_v1` 兼容配方）

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

## 图纸验收：开口夹紧座（`9.jpg` / `split_clamp_support_v1`）

此图不沿用旧 `bracket_support_v1` 字段，而是识别为
`partType=split_clamp_support`、`recipeId=split_clamp_support_v1`。坐标约定为
X=底板长度，Y=后端至前端，Z=高度。标准验收参数为：

| 特征 | 参数 |
| --- | --- |
| 底板 | `baseLength=125`、`baseWidth=95`、`baseThickness=15`、`baseMainDepth=80`、`frontTongueWidth=80` |
| 底板圆角 | `outerCornerRadius=8`、`neckConcaveRadius=5`、`neckConvexRadius=8` |
| 圆筒座 | `pedestalOuterRadius=33`、`pedestalCenterFromRear=35`、`pedestalHeight=40`、`rearClampRise=20`、`totalHeight=75` |
| 中央盲孔 / 开缝 | `boreDiameter=36`、`boreFloorZ=40`、`splitWidth=12` |
| 底板安装孔 | `mountHoleCount=2`、`mountHoleDiameter=12`、`mountHoleCenterDistance=96`、`mountHoleCenterFromRear=40` |
| 横向孔 | `crossHoleDiameter=12`、`crossHoleCenterZ=55` |
| 加强筋 / 后桥 | `ribHeight=20`、`ribThickness=10`、`rearBridgeWidth=86` |

其中三个容易混淆的基准已固定：安装孔中心线距后端是 40 mm，不是圆筒轴心的
35 mm；横向孔中心在下座顶面 Z=`15+40=55` mm，不是 65 mm；底板缩颈的凹角是
R5，前舌与主板前侧外凸角是 R8，主板后侧两角保持方角。下座俯视轮廓为前半
R33、后半直边延伸至后缘的 D 形，后高墙为矩形直边后半体，两块筋板位于后缘
10 mm 带。

可在无图纸交接的调试场景中直接调用通用校验端点；产生交付物时，推荐使用客户已确认的
`sourceDrawingId` 和严格 OCCT 门禁：

```json
{
  "partType": "split_clamp_support",
  "recipeId": "split_clamp_support_v1",
  "parameters": {
    "baseLength": 125,
    "baseWidth": 95,
    "baseThickness": 15,
    "baseMainDepth": 80,
    "frontTongueWidth": 80,
    "rearBridgeWidth": 86,
    "totalHeight": 75,
    "pedestalOuterRadius": 33,
    "pedestalCenterFromRear": 35,
    "pedestalHeight": 40,
    "rearClampRise": 20,
    "boreDiameter": 36,
    "boreFloorZ": 40,
    "splitWidth": 12,
    "mountHoleCount": 2,
    "mountHoleDiameter": 12,
    "mountHoleCenterDistance": 96,
    "mountHoleCenterFromRear": 40,
    "crossHoleDiameter": 12,
    "crossHoleCenterZ": 55,
    "ribHeight": 20,
    "ribThickness": 10,
    "outerCornerRadius": 8,
    "neckConcaveRadius": 5,
    "neckConvexRadius": 8,
    "material": "45# 钢",
    "units": "mm"
  },
  "formats": ["step", "glb"],
  "sourceDrawingId": "<confirmed-recognition-id>",
  "requireCadQuery": true
}
```

生产验收要求 `engine=cadquery-occt`、`validation.valid=true`、
`validation.productionReady=true`；形体是一个有效实体，包络为 `125×95×75 mm`，并通过 D 形下座、
矩形后墙、中央盲孔、两安装孔、贯穿横孔、后缘筋板、开缝、方形后角与前侧底板圆角审计。STEP 重新导入 CadQuery/OCCT 后必须仍为
单一有效实体、包络不变；GLB 必须为版本 2 的有效 `glTF` 二进制文件。

### 工作台 AI 对话验收

进入首页点击“上传图纸开始分析”（或在 3D 工作台点击“上传图纸”），选择文件后会先进入
“待处理图纸”队列；确认文件/补充意图，再点击“开始 AI 分析”。AI 返回完整的候选参数、
来源视图、置信度、特征假设和待确认问题；客户可以在参数面板逐项编辑，然后点击独立的
“确认数据”，最后点击“生成 3D”。工作台顶部会按
“输入/上传 → AI 分析 → 确认数据 → 生成 3D → 二次修改 → 导出交付”显示进度。生成成功后
可继续输入“把底板长度改为 90 mm”等自然语言，系统会调用同一会话并重新导出 STEP/GLB。
安装支架验收图应显示包络 `100 × 50 × 40`；开口夹紧座 `9.jpg` 应显示包络
`125 × 95 × 75`。两者都必须显示真实 WebGL/GLB、`cadquery-occt`、实体/面统计，且消息明确写出
“通过 OCCT 拓扑检查”。

未知图纸仍会显示 `needs_review`，但这只表示“候选数据待确认”，不会停在只有 reviewer
入口的页面：AI 会把可识别字段和不确定项展示出来，客户/设计师可补全 `parameterOverrides`
后确认，再进入 3D 生成。PDF/DXF 已纳入远程文件输入协议；DWG 则先进行本地原生转换、
矢量提取与高清渲染，原始二进制不直接发送给中转站。当前版本仍不承诺从任意二维 DWG
自动重建唯一三维拓扑，未知零件需要建立或选择已审核配方/特征树。

## 测试与文件化开发

```bash
cd apps/api
python3 -m pytest

# 只跑开口夹紧座的配方/API/OCCT 回读闭环
python3 -m pytest tests/test_split_clamp_support.py
```

核心服务测试覆盖 PDM 不可变版本/陈旧写入、密码与 token、RBAC、确定性 OCR 证据、
CAM 碰撞门禁和审核者/放行者分离；开口夹紧座测试额外覆盖配方错配/未知字段拒绝、
`125×95×75` 包络、关键圆柱/开缝/圆角拓扑、STEP 回读、GLB 文件结构、严格 OCCT 禁止降级，以及
AI 候选 → 客户确认 → 通用几何生成的交接。API 集成测试在安装 FastAPI 开发依赖后自动运行；
CadQuery/OCCT 可选依赖不存在时相关实体回读用例会跳过，不能据此声称已通过生产验收。

- [Product-Spec.md](Product-Spec.md)：产品定位、图纸验收约束、能力矩阵和版本路线。
- [docs/CHANGELOG.md](docs/CHANGELOG.md)：版本变更、已知限制和升级说明。
- [apps/api/README_PLATFORM.md](apps/api/README_PLATFORM.md)：平台服务 API 与安全策略。

## 生产边界

当前仓库已经把 FastAPI、参数配方注册表、CadQuery/OCCT、证据优先 OCR、SQLite PDM、账号/RBAC 和
CAM/NC 工作流接入到同一条可复现链路：候选必须先显式人工确认，通用生成端点只执行服务端白名单配方；
OCCT 模式会做实体拓扑、包络、体积、关键圆柱轴线检查，并重新打开 STEP 做一致性复核；平台工作流会把原图、
AI/OCR 证据、已确认参数、验证报告和模型版本关联保存。缺少 OCCT 时的 fallback 仍只能审阅，
不能标记为生产 B-Rep；`requireCadQuery=true` 时必须失败而不是降级。

要部署为真正的制造生产系统，还需要把本地 SQLite/进程缓存替换为组织级数据库和对象
存储、接入集群任务队列与多租户审计、采用经过认证的机床后处理器，并运行机床专用材料
去除/碰撞仿真。未知图纸会保持 `needs_review` 直到明确的客户/设计师确认，但不会阻断候选
数据编辑；确认也不等同于 PDM 发布或 CAM/NC 放行。
