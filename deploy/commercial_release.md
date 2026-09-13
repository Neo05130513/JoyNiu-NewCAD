# 商业模块升级准备与恢复（2026-09-10）

最新后台运营增强版已发布，实际验收范围见 [后台运营增强与验收](../docs/admin-operations-v2-2026-09-10.md)。首版后台见 [管理员后台验收记录](../docs/admin-acceptance-2026-09-10.md)，首次禁收费版本见 `docs/commercial-acceptance-2026-09-10.md`。本文件保留各次发布快照与恢复流程，历史镜像和检查时间不能替代实时状态。

## 2026-09-12 线下积分与运行保障发布

当前API/Web镜像为 `offline-credits-20260912`，短ID分别 `bed56d489452`、`bfbdd4088a9d`；完整ID、计费规则快照、生产浏览器与恢复证据见 [本轮交付记录](../docs/commercial-delivery-2026-09-12.md)。先前 `admin-login-20260911` 保留。源码包未包含生产数据或密钥；本轮没有生成Git提交。

Compose增加 `deploy/compose.operations.yaml` 的只读监控报告挂载，宿主部署定时监控与协调停写备份；AI服务、登录、出口未变更。线上支付关闭，线下登记充值可用；保存倍率1和固定结算汇率7的官方价折算版本，实际扣分因正式条款尚待真实业务资料仍关闭。

发布停写备份1010文件/2库；首份协调备份1039文件/2库，并已恢复到全新隔离目录校验。首次恢复探针路径错误已经修正并增加真实HTTP回归，保留原错误记录。运行保障后续脚本更新无需更换应用镜像，但须同步 `deploy/commercial_operations.py`，不能用旧打包脚本覆盖修复。

回滚优先切回上一版应用镜像并保留当前数据；不得用发布前快照覆盖新增订单或客户操作。回滚前协调现有定时器/准入门禁与镜像能力，之后再确认业务健康和收费开关。

## 2026-09-11 管理员登录账号更新

当前 API/Web 为 `admin-login-20260911`，镜像短 ID 分别为 `d5994b1d0822`、`101674698cdb`，基于上一版 `admin-v2-20260910` 构建并保留回滚镜像。别名表为兼容增加，原用户 ID、邮箱、项目归属与角色保留。登录页接受账号名或邮箱；用户指定的登录资料通过服务器受控方法设置，不作为任何安装的默认凭据，也不写入仓库。

切换前确认无活跃任务并停止 API；备份 `/opt/joyniu-backups/pre-admin-login-20260911` 已验证 1008 文件、2 库。隔离启动与别名认证通过后更新服务。修改登录资料时撤销现有管理员会话，前端真实公网浏览器完成登录、后台访问、退出及退出后的 401 验证。API healthy，14 项公开检查通过；模型仍为 `codex / gpt-6-astra / high`，收费保持关闭。测试包括 7 项前端、31 项既有账号和角色回归、9 项别名专项。仅更改目标管理员登录资料，没有新增客户账号。

回滚应用优先切回上一版镜像并保留当前数据；上一版只支持原邮箱登录，不认识新别名。密码重置已作用于原账号，切回代码不会自动恢复旧密码，不得为此覆盖整个客户数据库。

## 2026-09-10 管理后台运营增强发布

当前 Compose 组合保持 `compose.yaml:deploy/compose.codex.yaml:deploy/compose.commercial.yaml`，仅更新 API/Web 应用镜像：

| 服务 | 当前镜像 | 短 ID | 保留的回滚镜像 |
| --- | --- | --- | --- |
| API | `joyniu-cad-api-codex:admin-v2-20260910` | `0f7e32891044` | `joyniu-cad-api-codex:admin-20260910`（`083f5617b04d`） |
| Web | `joyniu-cad-web:admin-v2-20260910` | `fea6a11ce130` | `joyniu-cad-web:admin-20260910`（`a12071bb896e`） |

发布包排除配置、认证、数据和 macOS 资源叉；新镜像先以禁网、临时数据库方式启动，验证平台服务、CAD、CRM、工单内部备注及积分流水路由存在。活跃、排队及计量操作为 0 后停止 API，完整备份 `/opt/joyniu-backups/pre-admin-v2-20260910`，已校验 998 个文件、2 个 SQLite；验证成功后解包并切换。出口容器、模型和登录挂载没有调整。

切换后 API healthy、Web running，两个库 `quick_check=ok`；17 个历史任务、2 个用户、1 个云工作区及既有业务记录行数与快照一致，699 个数据文件哈希不变。`cad_job_attempts` 与 `cad_provider_calls` 仍为 0。新 CRM、人工处理和客服协作表已创建。14 项公开检查及 57 项管理员只读检查通过，验收会话已撤销；具体范围见本轮验收文档。Codex 登录检查通过，实际配置保持 `codex / gpt-6-astra / high`。

本地前端 403 项与相关后端 454 项回归通过；真实浏览器验证客户档案及跟进、人工任务处理、七个财务页、客服分派和内部备注，390 px 导航遮挡已修复。线上没有执行这些客户业务写操作，也没有模型调用或付款、退款、扣积分、对外消息。收费和商户能力仍未启用。

新增表采用兼容迁移，旧应用镜像保留。需要回滚时切回上述上一版 API/Web 镜像并保留现有数据；不得直接用停写快照覆盖发布后的客户操作。完整恢复遵循后文交易核对与隔离恢复流程。

## 2026-09-10 独立管理员后台发布

### 首版部署快照与备份

本次使用既有 Compose 组合和数据目录，`deploy/compose.commercial.yaml` 固定以下新镜像：

| 服务 | 镜像标签 | 镜像短 ID |
| --- | --- | --- |
| API | `joyniu-cad-api-codex:admin-20260910` | `083f5617b04d` |
| Web | `joyniu-cad-web:admin-20260910` | `a12071bb896e` |

最终停写备份为 `/opt/joyniu-backups/pre-admin-20260910-final`，已验证 **1188 个文件、2 个数据库**。备份覆盖既有数据与配置，未清空用户、项目或旧任务。上一版 `:usability-20260910` 镜像保留供代码回滚；恢复当前数据库仍须遵守下文交易核对与隔离恢复要求，不以旧快照覆盖切换后的客户动作。

### 功能与兼容边界

- `/admin` 独立登录和导航覆盖概览、客户、跨客户任务诊断、积分订单、工单、费率、账号权限、条款、系统状态及统一审计。客户工作台保留单一后台入口；前进/后退和地址直达保持页面状态，返回工作台保留模型、聊天和任务状态。
- 新增运营、财务、客服和审计角色。运营可以诊断/停止任务；财务可以核账、退款和调账；客服可以管理工单与读取安全任务摘要；审计只有读取能力。费率、套餐价格、条款与账号角色由全权管理员管理。权限在接口与服务层执行，前端不提供越权操作按钮。
- 任务列表同时读取历史 `cad_runs` 与新的任务尝试记录，保留旧修订。旧任务仍可诊断，没有可取消运行登记的历史任务不假报停止。后台不返回客户图纸、建模对话、模型代码、原始错误或签名下载链接；财务数据还需单独的读取权限。客户/任务详情访问与管理员停止操作均留审计。
- 购买支付、实际扣积分和真实退款执行仍未启用。没有发起 AI 任务，未变更模型、Codex 登录或网络出口；不增加预估费用、积分冻结、预算上限暂停流程。

### 首次切换故障与重新发布

首次切换时，macOS 打包带入了 AppleDouble `._*.json` 资源叉文件。Linux OCR 样例加载器的 `*.json` 扫描将其当作真正的样例，读取二进制时触发 `UnicodeDecodeError`。平台服务初始化异常被原来的可选模块边界捕获，核心几何健康接口仍能应答，但平台、CAD Agent 与管理员路由未加载。因此单独 `/api/v1/health` 返回成功不能证明业务可用。这不是用户数据库损坏或新业务角色与旧角色冲突。

处置顺序：先切回旧镜像恢复服务，保留当前数据；随后修复发布包与应用镜像，在隔离环境验证镜像能够完整启动，再重新发布当前镜像。已采取三层防御：

1. macOS 打包使用 `COPYFILE_DISABLE=1 tar --no-xattrs --no-acls`，发布清单仍排除本地数据和密钥。
2. `.dockerignore` 排除 AppleDouble 元数据；Docker 发布层清理应用源码目录内的 `._*` 文件，避免继承层中的残留。清理范围是发布源码，不触及生产 `data/`、认证挂载或备份。
3. OCR fixture loader 忽略名称以 `.` 开头的隐藏元数据。真实非隐藏样例的 JSON/UTF-8 损坏仍报错；不通过吞掉所有异常隐藏发布错误。已有账号 SQLite 与污染样例目录的启动回归已覆盖。

### 后续发布必须验证的业务就绪状态

候选镜像先在独立实例以隔离恢复数据启动，确认平台服务构造成功且 `_platform_error` 未设置，再进行切换。切换后除核心健康检查外，还必须核查：

- 平台：`/api/v1/auth/account-capabilities` 可读；登录及受保护账号读取按权限工作（2026-09-12修正接口路径）。
- CAD：`/api/v1/cad-agent/capabilities` 可读，供应商/模型配置符合本次要求；受保护任务列表和历史任务读取可用。
- 后台：管理员请求 `/api/v1/admin/overview`、`/api/v1/admin/tasks` 等路由成功；普通客户及无权业务角色得到预期拒绝。单独收到 404、核心 health 成功或静态 `/admin` 页面可打开，均不能作为后台服务通过。
- 支付/收费/退款关闭状态、旧项目和任务、浏览器前进/后退、退出登录与客户数据隔离按发布范围复核；无需为验证路由而新建 AI 任务。

### 最终验收结果

- 前端 **380 项**、后端 **269 项**全部通过；后端退出码为 **0**，数量已经 JUnit XML 核对。
- 线上 `verify.py` **14 项公开检查**通过；额外 **14 项管理员 API 检查**通过，覆盖 5 类管理入口未登录返回 401、17 个旧任务可读、客户/任务关联和审计、安全 DTO 不含敏感内容、筛选分页、运行模型配置和收费关闭状态。验收使用的登录会话已经撤销。
- API healthy、Web 正常运行；Codex 登录检查为 `Logged in using ChatGPT`，当前配置为 `codex / gpt-6-astra / high`。该状态记录只验证运行接入，不替代商业授权核实。
- 两个数据库 `PRAGMA quick_check` 均为 `ok`；`cad_runs` 保留 17 个任务、`cad_job_attempts=0`。镜像应用代码树内 `._*` 文件数为 **0**，本轮没有发起 AI 任务。
- 线上内置浏览器打开 `/admin`，显示正确的独立后台登录页且没有注册入口；已登录后的后台操作、页面切换与权限交互在本地浏览器完成验证。没有将本地交互验收描述为线上浏览器全部流程验收。
- 购买支付、实际收费、真实退款仍未开启；本次检查不包含商户实付、退款到账、付费模型生成或长期容量/告警验证。完整证据范围见 [管理员后台验收记录](../docs/admin-acceptance-2026-09-10.md)。

## 首次商业模块升级前的只读核查结果

服务器 `122.51.168.205`，目录 `/opt/joyniu-cad`，实际 Compose 组合为 `compose.yaml:deploy/compose.codex.yaml`。2026-09-10 18:32–18:35（Asia/Shanghai）观测：

- API 健康；Web 和独立出口运行。API 内仅 `docker-init` 和 Python 主服务进程，没有 Codex/几何子进程。
- `cad_runs` 共 17 个最新修订：7 个 failed、5 个 needs_input、5 个 review_required，没有 running。新 `cad_job_attempts`/`cad_provider_calls` 表尚不存在。这只是检查时快照，切换前必须再次检查。
- 两个 SQLite 的 `PRAGMA quick_check` 为 `ok`。`data/joyniu.sqlite3` 主文件仅 4096 字节，不能据此认为无数据，必须包含 WAL 中事务。
- API 约 522 MiB/6 GiB，CPU 约 0.13%；磁盘 59 GiB 已用 47 GiB，仅余约 11 GiB。Docker 报告镜像 35.48 GB、构建缓存 20.52 GB，统计可能共享层，不能相加当成独立磁盘占用。新建完整几何镜像前需要空间规划。
- Nginx `server_name _`，只监听 80；本机 443 未开放。当前仍是 IP+HTTP，无已配置域名证书。

该次升级前 API 镜像 ID 为 `sha256:510c430f8519f4e755acd946f98a9027d751eac96371f5a6a3894f005590082f`；Web 镜像 ID 为 `sha256:be3d71d40869a1bed429adc1135052882cc1ef550c1960b3d34d2e5e2e947e2a`。切换前重新采集实际 ID，不能把这份历史记录当成当时的运行状态。

## 迁移范围和依赖

首次升级在 `data/joyniu.sqlite3` 中无损增加账号会话/刷新令牌/限流、安全状态、云工作区及其审计、订单/钱包/账本/用量/退款/商业规则等表；保留既有用户、密码哈希、角色、PDM 和 CAM 数据。在 `data/cad-agent/runs.sqlite3` 中增加任务尝试和逐供应商调用记录，保留原 `cad_runs` 修订与原图、模型产物。迁移由新服务构造器执行，不需要清空库或重新初始化管理员。

商业规则模块已完成，使用最终分支全部构造器通过禁网隔离迁移演练；备份整个数据库而不是挑表，可自动涵盖后续新增计费政策表。

API 运行镜像需要安装 `.[geometry,ocr,dwg,pdf,image,payment]`，其中 `payment` 加密依赖固定为 `cryptography>=50.0.1,<51`。只复制应用源码不会安装新依赖。本次使用 `Dockerfile.commercial` 基于固定的已验收 Codex 镜像，读取 pyproject 的 payment 依赖并仅安装该依赖组，随后复制新 app，避免重建整套几何环境；腾讯镜像下载已验证。完整环境重建仍可按基础 API → Codex 覆盖层顺序。保持 LibreDWG 0.14 与既定 Codex 引擎，不在商业升级时顺便换模型。

新旧代码不能同时写同一生产库。迁移后先验收账号登录、旧用户权限、原任务、云项目同步、钱包空状态与回调拒绝边界。没有商户实付/退款验收、计费策略和域名 HTTPS 时，注册、公网购买、真实退款及实际扣积分保持关闭。

## 完整备份

完整恢复集应包括：

1. 全部 `data/`（两个 SQLite 以及 CAD 原图、导出模型、修订、PDM/CAM 文件）。云项目和钱包都在其中，不能只备份 `cad-agent`。
2. 项目根 `.env`、生产运行环境文件、`deploy/.secrets/` 的专用认证与出口配置，以及 Compose/Nginx/Dockerfile、运行源码和 `dist/`。
3. 旧 API/Web/出口的实际镜像 ID、不可变回滚标签，以及可在异机恢复的镜像归档。仅备份可变镜像名称或 Dockerfile 不能保证恢复同一版本。
4. 尚未云同步的客户浏览器本地项目：服务器备份不包含这些内容。首批账号需在升级前保留工作区导出，并验证首次云同步不会覆盖旧文件。

先禁止新建任务，等待运行和排队任务结束，再停 API。仅观察 CPU 低不足以判断任务已结束。生产实例目前只有一个 API worker；不要升级时直接增加 workers，以免改变已有调度假设。

停机后可以使用新增工具（以下命令未在生产执行）：

```sh
cd /opt/joyniu-cad
docker compose stop api
sudo python3 deploy/commercial_backup.py backup \
  --root /opt/joyniu-cad \
  --destination /opt/joyniu-backups/pre-commercial-YYYYMMDD-HHMM \
  --writers-stopped
sudo python3 deploy/commercial_backup.py verify \
  --backup /opt/joyniu-backups/pre-commercial-YYYYMMDD-HHMM
```

工具额外检查 Compose API 已停止，整个 `data/` 不做选择性过滤，使用 SQLite backup API 将已提交 WAL 事务吸收到独立快照。保护目录权限 700、清单 600，SHA-256 覆盖全部文件并记录所有表行数。清单缺失、文件不一致、符号链接、库完整性异常都会拒绝恢复。工具不停止或启动生产、不删除旧备份、不覆盖目标目录，不输出配置内容或命令错误中的密钥。

备份包含认证资料，须保存在受保护的加密异地存储；不要进入 Git、公开下载路径或普通工单附件。当前服务器余量不足以稳妥同时保存全部旧镜像归档和重构建缓存，镜像归档宜直接输送到受保护的异机存储。保留并校验回滚镜像之后，才由运维明确选择可删除缓存；本轮没有运行任何 prune。

## 构建和验收顺序

先保存旧镜像并完成备份，再上传经验证的源码和 `dist`，排除本地数据、`.env*` 和密钥，保留服务器专用配置。前端以 `VITE_API_BASE=/api/v1` 构建，不能带 localhost API 地址。基础 API 的 payment 扩展准备好后，遵循原 Codex 构建顺序：

```sh
docker compose -f compose.yaml build api
docker compose build api web
docker compose up -d --no-build api web
python3 deploy/verify.py --base-url http://127.0.0.1 \
  --expected-cad-provider codex --expected-cad-model gpt-6-astra
```

出口和登录目录继续使用既有挂载；不执行 logout、不替换代理、不另建管理员。额外核查 SQLite 完整性/旧表行数、新表存在、客户登录刷新及登出、旧项目原图可读、任务中心历史/恢复、页面和 API 版本一致、未开通商业能力均返回明确不可用。开发阶段还应验证旧 AI 路由在商业启用时 409，防未计量通道绕过任务中心。

域名和 HTTPS 完成后还需核对：DNS 指向；443 入站和证书自动续期；HTTP 跳转 HTTPS；Nginx 到 API 的可信 `X-Forwarded-Proto`；同源 Cookie、Secure/HttpOnly/SameSite、CSRF 和精确 Origin；微信支付与退款 HTTPS 回调 URL。若 TLS 在上游终止，不能让现有 `$scheme` 把 HTTPS 覆盖成 HTTP。只通过端口健康检查不代表支付回调、Cookie 刷新或真实收费已验收。

## 回滚与恢复

代码回滚优先恢复已保存的旧 API/Web 镜像与同一 Compose 组合，保留当前 `data/`，以免丢失切换后客户动作。旧版本不认识新会话/云项目/账本，不应继续对外提供商业服务；回滚前停止新任务/收款，核查是否已有真实支付或退款事件。

只有在明确需要数据回退并完成新旧事务核对后，才恢复数据库快照。应先另存当前完整数据与支付回调记录，不能用旧快照抹掉升级后到账和退款。必须协调微信主动查单/对账，避免客户实际支付已发生、钱包却回到了付款前。

工具只恢复到不存在的隔离目录：

```sh
sudo python3 deploy/commercial_backup.py restore \
  --backup /opt/joyniu-backups/pre-commercial-YYYYMMDD-HHMM \
  --destination /opt/joyniu-restores/pre-commercial-YYYYMMDD-HHMM
```

核验快照和目标两库完整性、各表行数、原图哈希后，在服务停止状态下由运维完成目录切换。恢复原来的属主/权限：数据及 Codex/出口专用挂载由 UID/GID 10001 访问，认证目录 700、认证文件 600；Compose 环境文件保持原运维账号可读。切换过程不删除当前目录，先改名保留，检查失败即可立即切回。随后恢复旧镜像，执行只读健康、登录和原项目验收。

本地恢复演练使用两套临时 SQLite，故意保留未 checkpoint 的 WAL，包含测试用户/会话、云工作区、积分账本、退款事件、计价政策、任务和供应商调用表，并保存测试原图与假配置文件。11 项测试已通过：WAL 数据完整恢复、全表行数/哈希一致、现有目标禁止覆盖、篡改及越界符号链接拒绝；没有真实 AI 或商户调用。

## 首次商业模块实际切换与后续 Compose

当前 `.env` 的 `COMPOSE_FILE` 为 `compose.yaml:deploy/compose.codex.yaml:deploy/compose.commercial.yaml`。后者固定本次发布镜像、关闭注册/退款执行及兼容旧永久链接。收费真实启停仍需检查数据库策略状态，不能只看环境变量。

最终备份为 `/opt/joyniu-backups/pre-commercial-20260910-final`。因 Codex 临时链接拒绝而产生的无清单目录仍保留，不把它当作有效备份；仅使用已通过 verify 的完整目录。备份工具精确排除 Codex 临时目录并记录清单，登录配置、历史会话、图纸和其他目录均保留。恢复只进入不存在的隔离目录，不覆盖生产。

该次 API 镜像 `ad27b3540b3e`，Web 镜像 `12e323deae8f`。需要回滚时先停止商业新任务/收款，按本文核对交易，然后切回原两个 Compose 文件和保留的旧镜像；不直接回退数据库抹掉发布后的客户操作。

## 2026-09-10 非支付客户验收修复发布

该次 API/Web 镜像更新为 `:usability-20260910`，由 `deploy/compose.commercial.yaml` 指定；上一版 `:commercial-20260910` 镜像保留。API ID `ca7c0ae9e44e`，Web ID `7732552f93c4`。发布前备份 `/opt/joyniu-backups/pre-usability-20260910`（980 个文件、两个 SQLite）已校验。没有修改模型、Codex 登录、出口或收费策略。实际验收与范围见 `docs/nonpayment-user-acceptance-2026-09-10.md`。回滚优先切回上一版镜像并保留当前数据，不用备份覆盖后续客户变更。
