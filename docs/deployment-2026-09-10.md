# 2026-09-10 服务器 Codex 部署补充

本记录补充 [2026-09-09 部署记录](deployment-2026-09-09.md)。用户已明确授权服务器安装 Codex，并登录其 ChatGPT 账号供 JoyNiu CAD 使用；此前“未迁移本机 Codex 登录”的描述属于首次部署时的状态。本文不包含账号、登录令牌、代理节点地址或订阅内容。

## 部署前后的真实差异

只读比对了本地 API 实际工作进程、服务器容器配置以及实际 Python 模块，而非仅比较 `.env` 模板：

| 项目 | 部署前本地实际运行 | 首次服务器部署 |
| --- | --- | --- |
| CAD 引擎 | `codex` / `codex-cli` | `relay` / Responses HTTP |
| CAD 模型 | `gpt-6-astra` | `gpt-5.6-sol` |
| 模型请求 | Codex CLI 子进程与 ChatGPT 登录 | 中转 Responses 请求与 API 密钥 |
| 网络出口 | Mac 上 FlClash 代理可用 | 无代理，直接出站 |
| 通用中转配置 | 与服务器相同的中转地址、模型和密钥 | 相同；凭证仅做相等校验，未披露 |

两端 38 个实际 Python 模块内容一致；Python 3.12.14、CadQuery 2.8.0 与主要几何/图像依赖相同。服务器资源检查未发现内存耗尽或 CPU 饱和。这些证据说明“本地和服务器使用同一模型”的原假设不成立；不能把识图、规划或服务稳定性差异只归因于 Linux 或代码丢失。

网络检查发现服务器对 OpenAI/ChatGPT 域名的直连解析及连接异常，本机经代理可访问；中转入口快速返回鉴权错误只证明该入口可达，不证明请求能完成或具备同等模型。中转模型列表未包含 `gpt-6-astra`。因此仅修改中转模型名称或增大整轮超时，不能复现本地的实际调用路径。

## 已部署的服务器配置

- Linux x64 Codex CLI 固定为 `0.153.4`，与本机版本一致；安装包校验固定 SHA-512，登录凭证独立于镜像。
- CAD 覆盖配置选择 `codex/gpt-6-astra`。通用中转 `gpt-5.6-sol` 配置保留，但不再代表 CAD 所选引擎。
- 服务器专用 `deploy/.secrets/codex/` 以 UID/GID 10001 挂载到 `/run/joyniu-codex`，目录权限 `700`、`auth.json` 权限 `600`，允许 CLI 持久刷新登录状态。状态接口仅报告 `authentication=cli-managed`，不公开账号信息、不执行隐式认证探测。
- 经授权从现有 FlClash 配置选取当前 LA 单节点，写入受保护的 `deploy/.secrets/egress/config.yaml`。独立 `cad-egress` 容器运行 Mihomo `v1.19.30`，只在 Docker 私网提供代理，未发布宿主端口；不把完整订阅或其他节点写入仓库。
- API 使用显式 `JOYNIU_CAD_CODEX_PROXY_URL=http://cad-egress:7890`，适配器只向 Codex 子进程传递必要代理变量。客户浏览器不需要安装 Codex，也不承担服务器模型出口。
- 持久化方案是在项目根 `.env` 配置 `COMPOSE_FILE=compose.yaml:deploy/compose.codex.yaml`，使常规 Compose 操作持续使用 Codex 与出口服务，避免下一次更新无意退回中转。临时 Mac SSH 隧道已断开，部署主线确认服务器使用独立出口；应用完整建模验证结果见下文。

前后端运行状态支持 `deployment=server`，显示“服务器 Codex”。开始、进度、恢复、结束记录绑定实际选中的引擎、模型与部署位置，后续默认配置变化不会改写历史任务。可公开的 `binaryAvailable`、`proxyConfigured` 仅为布尔值；不公开可执行路径、代理 URL 或凭证。

## 当前验证范围

独立服务器 `cad-egress` 出口配合 Codex 完成了一次实际图纸识别，请求约 **19.913 秒**成功。这验证了该次请求的独立网络出口、登录及视觉模型调用路径；不等同于完成整个生产 CAD 工作流。

应用镜像已构建并切换，9.jpg 的正常服务器建模、已记录尺寸检查、独立图纸复核和网页预览均已完成，结果如下。其他图纸未据此视作逐张验收通过，STEP 交付仍按产品流程等待客户确认。

部署完成后的只读验收命令：

```sh
python3 deploy/verify.py --base-url http://127.0.0.1 \
  --expected-cad-provider codex --expected-cad-model gpt-6-astra
docker compose exec -T api codex --version
docker compose exec -T api codex login status
```

HTTP 验收分别验证实际 CAD 引擎、模型及程序可用性；CLI 登录列为 `unknown`，需要独立登录预检。完整验收还应核对真实任务保存的 provider、原图转录、有效计划、OCCT 实体、尺寸检查和独立图纸复核结果。登录续期、默认 Compose 配置、构建顺序与回滚步骤见 [部署说明](../deploy/README.md)。

## 应用切换与初步验收

2026-09-10 00:22（Asia/Shanghai）完成生产切换：

- 新 API 使用 `joyniu-cad-api-codex`，镜像摘要 `sha256:e53044113af5bfa49854e64959ed0bd13d4b70075dc1fc09e72a4efcd966539a`；前端脚本 `index-BR7IpjmP.js`。
- 项目根 `.env` 已持久化 `COMPOSE_FILE`；新 SSH 会话中的默认 `docker compose ps` 正确列出 Codex API、Web 和独立出口。
- 旧 API 保存为 `joyniu-cad-api:pre-codex-20260910`；旧前端静态文件及 Nginx 配置保存在服务器 `deploy/backups/pre-codex-20260910/`。未删除或重写客户运行记录。
- Codex CLI 登录预检真实返回 `Logged in using ChatGPT`；HTTP 部署检查全部通过，预期引擎 `codex`、模型 `gpt-6-astra`、程序可用性匹配。HTTP 报告中仍保留认证 `unknown` 的只读语义，由上述独立 CLI 预检及真实请求另行验证。
- 宿主机未监听 `7890` 或临时隧道 `17890`，出口仅在 Docker 网络内供 API 使用。
- 前端 238 项测试通过；本轮 Codex/引擎绑定/部署检查/相切边的 129 项针对性后端测试通过，几何相关回归另由 65 项测试覆盖。
- 浏览器刷新后显示“服务器 Codex / gpt-6-astra”，已有账号通过浏览器保存的凭证登录成功。

正常点击“重试建模”已为客户项目“新建项目 3”的 9.jpg 创建 `cad_12620b23ef4a4aeba374c5b5963ddcb1`（UTC 2026-09-09 16:23:14）。本次任务保存的 provider 明确为 `codex/gpt-6-astra`、`deployment=server`，未通过修改历史记录来伪造重试结果。

## 工程投影误报修复

同图隔离对照还发现：原先 SVG 工程投影把 G1 光滑相接处显示成棱线，导致右视图出现四处多余线段，被独立复核判为轮廓差异。投影现在对实体副本编码严格连续性，并只输出真正的棱线、外轮廓与应显示的隐藏线；不修改实体、不放宽几何容差。

对同一个已生成 STEP 重新导出投影，右视图不支持的线段比例从 16.17% 降为 0。标准独立 Codex 复核真实返回 `consistent`，53.659 秒、一次请求、无差异和补问。该隔离验证未改变任何客户 run 的状态；生产仍须由正常流程重新生成及完成自己的复核。

## 9.jpg 生产实测结果

`cad_12620b23ef4a4aeba374c5b5963ddcb1` 在 672.981 秒后正常进入 `review_required`，不是失败或超时。16 个特征；真实 OCCT 有效单实体，35 个面、99 条边，外包尺寸约 125 × 95 × 75 mm，体积 302126.340865 mm³。7 项已记录的几何验收均为 `passed`。

独立图纸复核为 `consistent`，62.844 秒、一次请求，无差异、补问或错误；随后正常 finish。像素配准保留不确定性：正视 supported，俯视 uncertain、侧视因另一侧可见性缺失为 uncertain；无可靠 mismatch。不能将独立 AI 复核等同于所有尺寸和全部像素逐一证明一致。

重新读入本次生产 STEP 做只读检查：中央 Ø36 竖孔、两侧 Ø12 安装孔的完整圆柱范围与实体的材料交集均为零，确认真实贯通；前槽内材料区间为 Z=0..40、槽侧为 Z=0..55，后横孔连通路径无材料。检查未修改计划或运行状态。

- GLB：272008 字节，正常 Nginx 下载路由 HTTP 200，`glTF`/版本2文件头及声明长度正确。
- STEP：150303 字节，`ISO-10303-21` 文件头正确；客户确认前正常路由返回409，未绕过交付门禁。
- Chrome 的“新建项目 3 / 零件 01”已显示实际 GLB，前视切换和缩放可用，状态为“待确认与交付”。未代替客户执行“确认数据并生成”。
- 原始产物目录：服务器 `data/cad-agent/cad_12620b23ef4a4aeba374c5b5963ddcb1/builds/iteration-9/`。

完整服务器流程已验证到客户最终确认入口：原图识别 → 分步建模 → 真实实体执行 → 已记录尺寸检查 → 独立图纸复核 → 网页预览。该次任务始终使用服务器独立出口及 Codex/astra；没有依赖已断开的 Mac 隧道。
