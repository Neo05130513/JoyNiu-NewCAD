# JoyNiu CAD 单机部署

运行结构：Nginx 对外提供 80 端口，前端与 `/api/v1` 同源；API 仅在 Docker 网络监听 8010。API 使用单进程、UID 10001，限制 6 GiB 内存及 3.5 CPU。部署目标为至少 4 核、8 GB 内存的单机，SQLite 与原图/实体保存在宿主机 `data/`。默认关闭匿名 AI，需先初始化管理员。

## 构建与启动

1. 在开发机运行 `npm ci`，然后 `VITE_API_BASE=/api/v1 npm run build`。
2. 上传源码、`dist/`、`compose.yaml`、`.dockerignore` 和 `deploy/`。排除 `.env.local`、本地数据、依赖目录及密钥。
3. 将服务器专用配置写入 `deploy/.env.production`，权限设为 `600`。设置新的 `JOYNIU_AUTH_SECRET`；通用模型 API 的地址、名称和密钥仍独立配置。CAD 引擎另由 `JOYNIU_CAD_PROVIDER` 选择：中转模式用 `relay`，已授权的服务器 Codex 按下节配置。不要把个人登录文件当作普通部署素材批量上传。
4. 创建数据目录：`sudo mkdir -p data/cad-agent && sudo chown -R 10001:10001 data`。
5. 中转模式运行 `docker compose -f compose.yaml build`；先启动 API，由受保护的服务器运维流程初始化管理员，再启动 Web。生产环境的匿名 `POST /api/v1/auth/users` 即使空库也拒绝初始化；已有部署保留数据库，无需重新创建账号。Codex 模式使用下节的构建顺序。

可通过环境变量 `PYTHON_IMAGE`、`NGINX_IMAGE`、`PIP_INDEX_URL`、`DEBIAN_MIRROR`、`GNU_MIRROR` 选择镜像源。此次服务器将这些非敏感构建配置保存在项目根目录 `.env`，使用已缓存的 `docker.m.daocloud.io/library/python:3.12-slim`、`docker.m.daocloud.io/library/nginx:alpine` 与腾讯云下载镜像。默认配置支持从公开 Docker Hub / PyPI / Debian / GNU 构建。运行时密钥单独保存在 `deploy/.env.production`。

后端安装 CadQuery/OCCT、PDF、OCR、DXF 和图像处理依赖。原生 DWG 转换器在独立构建阶段由 [GNU LibreDWG 0.14](https://ftp.gnu.org/gnu/libredwg/libredwg-0.14.tar.xz) 源码编译并校验固定 SHA-256，运行镜像只保留 `dwgread` 和 `dwg2dxf`。升级时版本与哈希必须同时更新，并对真实图纸执行完整的图块展开、尺寸提取和 PNG 预览验收，不能只检查命令存在或 DXF 文件生成成功。0.13.3 在部分 AC1032 图纸上会产生缺失的匿名图块引用；不得用跳过图块来伪造解析成功。这与模型理解图纸的准确性是不同的验收项。

CAD 规划默认以 `medium` 完成首份计划前的操作，后续使用原模型推理配置；可用 `JOYNIU_CAD_INITIAL_REASONING_EFFORT` 调整初始阶段。`JOYNIU_CAD_PLANNER_TIMEOUT_SECONDS` 限制单次规划动作，默认 180 秒、允许 30–180 秒；`JOYNIU_CAD_FINISH_RESERVE_SECONDS` 为执行与复核预留时间，默认 120 秒、允许 0–180 秒（短任务最多预留总预算的四分之一）。这些配置不会延长整轮任务期限。长超时且无完整动作时，续接会使用 `medium` 分批恢复，未完成的 JSON 不会执行。独立图纸复核使用 `JOYNIU_CAD_REVIEW_REASONING_EFFORT`，默认 `medium`。

## 服务器 Codex 与独立网络出口

2026-09-10 用户明确授权服务器安装 Codex，并使用其 ChatGPT 账号为该软件提供 CAD 服务。此次采用与本机一致的 Codex CLI `0.153.4`、CAD 模型 `gpt-6-astra`。这是单独的引擎配置；通用中转服务仍配置 `gpt-5.6-sol`，不能用中转健康状态代表 CAD 实际选中的引擎。配置差异与实测边界见 [2026-09-10 补充记录](../docs/deployment-2026-09-10.md)。

`compose.yaml` 加载 API/Web；`deploy/compose.codex.yaml` 覆盖 API 为 Codex 镜像，并加入 `cad-egress`。出口容器运行 Mihomo `v1.19.30`，使用经授权的 FlClash 当前选中 LA 单节点配置，通过 Docker 私网为 Codex 提供 HTTP 代理。节点地址、认证信息与订阅不写入仓库或文档，不发布代理端口到宿主机，也不使用 Mac 上的临时隧道作为正式运行依赖。

| 宿主机位置 | 容器用途 | 权限与持久化 |
| --- | --- | --- |
| `deploy/.secrets/codex/` | API 的 `CODEX_HOME=/run/joyniu-codex` | UID/GID `10001:10001`，目录 `700`，`auth.json` 为 `600`；可写挂载，保留 CLI 刷新后的登录状态 |
| `deploy/.secrets/egress/config.yaml` | `cad-egress` 的 Mihomo 配置 | UID/GID `10001:10001`，受保护父目录，文件 `600`；只读挂载 |
| `deploy/.env.production` | 应用密钥与通用中转配置 | 文件 `600`，不进入镜像 |
| 项目根目录 `.env` | Compose 文件组合与非敏感构建参数 | 保留现有配置，加入下述默认文件组合 |

在服务器项目根目录 `.env` 中保留这一行，使 SSH 重新登录、普通 `docker compose up -d` 和后续更新仍使用 Codex 配置：

```dotenv
COMPOSE_FILE=compose.yaml:deploy/compose.codex.yaml
```

这是 Linux 服务器的文件分隔符；不要只在某次 shell 中临时 `export` 后遗漏持久化，也不要覆盖 `.env` 中已有的镜像源配置。显式 `-f compose.yaml` 会选择基础配置，因此仅在构建基础镜像或明确回滚时使用。

构建顺序如下。基础 API 镜像必须先存在，Codex 与出口镜像再基于它构建。出口构建还需要运维单独准备被 Git 忽略的非敏感二进制缓存 `deploy/.cache/mihomo-v1.19.30.gz`，其 SHA-256 由 Dockerfile 校验；Codex 安装器校验固定版本官方 npm 包的 SHA-512，当前仅支持 Linux x64。首次构建需要能够访问 npm registry；尚未启动的运行时出口不能替代构建阶段的网络配置。

```sh
docker compose -f compose.yaml build api
docker compose build api cad-egress web
docker compose up -d
docker compose ps
```

覆盖配置固定 `JOYNIU_CAD_PROVIDER=codex`、`JOYNIU_CAD_CODEX_MODEL=gpt-6-astra` 与 `JOYNIU_CAD_CODEX_PROXY_URL=http://cad-egress:7890`。适配器仅把这个经过验证的显式代理传给 Codex 子进程，不继承任意宿主代理或 API 密钥；保持只读沙箱、工具禁用及忽略用户配置。`cad-egress` 无宿主端口映射，以 UID 10001、只读根文件系统运行，限制 128 MiB 内存与 0.5 CPU。

### 引擎验收与登录续期

从项目根目录执行，只读 HTTP 验证不会调用 AI：

```sh
python3 deploy/verify.py --base-url http://127.0.0.1 \
  --expected-cad-provider codex --expected-cad-model gpt-6-astra
docker compose exec -T api codex --version
docker compose exec -T api codex login status
```

验收检查 `/api/v1/ai/status` 中的 `cadProvider`，要求实际引擎、模型与显式预期一致，同时确认 configured/binaryAvailable。也可用 `JOYNIU_EXPECTED_CAD_PROVIDER` 与 `JOYNIU_EXPECTED_CAD_MODEL` 环境变量传入预期。未设置预期时保留基础验收行为。`authentication=cli-managed` 表示由 CLI 管理登录；HTTP 不读取登录文件或探测账号，因此脚本将认证列为 `unverifiedChecks` 中的 `unknown/cli_login_precheck_required`，不能解释为认证或实际模型请求已通过。

`codex login status` 用于检查已有登录状态，其成功也不能替代网络与实际模型验收。服务端登录失效时，在受保护运维终端中重新走官方设备登录：

```sh
docker compose exec \
  -e HTTPS_PROXY=http://cad-egress:7890 -e HTTP_PROXY=http://cad-egress:7890 \
  api codex -c 'cli_auth_credentials_store="file"' login --device-auth
docker compose exec -T api codex login status
```

CLI 登录命令不会经过应用的代理注入逻辑，故单独指定容器私网代理。登录由账号持有人在官方页面完成；一次性代码、登录缓存与日志不粘贴到工单或仓库。设备登录需账号允许该方式；不可用时按官方无界面登录指引完成，并把授权凭证保存在上述专用目录。CLI 可在使用中刷新登录状态，挂载目录应保持可写；不要在后台任务运行时替换缓存或执行 logout。参见 [OpenAI 登录与缓存文档](https://learn.chatgpt.com/docs/auth#login-on-headless-devices) 与 [CLI 登录状态命令](https://learn.chatgpt.com/docs/developer-commands?surface=cli#codex-login)。

### 回滚

先等待当前 CAD 任务结束，保存正在使用的镜像标签、Compose 配置及受保护配置备份。程序版本回滚应保持同一 `COMPOSE_FILE`、`data/` 与专用登录挂载，仅恢复已验证镜像。不要通过删除数据卷或登录目录回滚代码。

若明确回退中转 CAD 引擎，将项目根 `.env` 的 `COMPOSE_FILE` 改回 `compose.yaml`，并在受保护的 `deploy/.env.production` 设置 `JOYNIU_CAD_PROVIDER=relay`，确认原中转配置可用，再执行：

```sh
docker compose up -d --no-build --force-recreate api web
python3 deploy/verify.py --base-url http://127.0.0.1 \
  --expected-cad-provider relay --expected-cad-model gpt-5.6-sol
```

切换成功且无其他使用者后，可显式停止出口：`docker compose -f compose.yaml -f deploy/compose.codex.yaml stop cad-egress`。回退中转会改变实际建模模型与调用路径，不保证具有 Codex 的识图能力，也不会消除已观察到的上游超时。原图、计划、账号数据及历史运行绑定保持保留。

## 日常操作

- 状态：`docker compose ps`。
- 日志：`docker compose logs --tail 100 api web`。Nginx 访问日志不包含 URL 查询参数，避免原图访问令牌被记录。出口/登录诊断日志只在受保护运维终端查看，不上传包含节点或认证内容的原始日志。
- 基础验收：`python3 deploy/verify.py --base-url http://127.0.0.1`。添加 `--credentials-file /受保护目录/login.json` 可验证登录，JSON 包含 `email` 和 `password`；输出不会包含密码或令牌。
- 几何验收：`docker compose cp deploy/geometry_smoke.py api:/tmp/geometry_smoke.py`，然后 `docker compose exec -T api python /tmp/geometry_smoke.py`。此检查验证真实隔离子进程、STEP/GLB 和三视图，并自动清理测试实体，不调用 AI。
- 更新：重新构建并上传前端与源码；中转模式正常构建，Codex 模式按上节先基础、再覆盖镜像的顺序构建和启动。保留 `.env` 中的 `COMPOSE_FILE`、数据目录、生产环境文件和 `.secrets/`。
- 一致备份：先 `docker compose stop api`，备份 `data/` 与生产配置，再 `docker compose start api`。不要在写入时只复制 SQLite 主文件而遗漏 WAL。
- 重启策略为 `unless-stopped`；Docker 服务开机运行时，正常运行中的应用随服务器恢复。

IP 访问当前使用 HTTP，未配置域名或 HTTPS。前端项目记录属于浏览器本地存储；服务器账号/PDM及CAD原图和修订在 `data/` 持久保存。旧服务器项目不自动导入新应用。
