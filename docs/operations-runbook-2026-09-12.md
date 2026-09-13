# 宿主监控、协调备份与恢复

本轮增加可安装的运行保障：宿主定时检查、后台安全投影、空闲时协调停写备份、校验、崩溃恢复及受限备份保留。默认配置不启用自动备份，不删除任何备份或客户资料。测试在临时目录与模拟服务中完成，没有用测试停止生产服务。

## 本次生产安装结果

2026-09-12 已随 `offline-credits-20260912` 发布安装。5分钟监控、15分钟检查空闲并按24小时最短间隔备份的两个systemd定时器已启用；配置位于 `/etc/joyniu-cad/operations.json`，监控报告位于 `/var/lib/joyniu-cad-operations/status.json`。外部通知和持续异地复制尚未配置。

首份 `/opt/joyniu-backups/scheduled/scheduled-20260912T110747Z-afb190f6` 含1039文件、2库，完整校验及恢复至全新目录 `/opt/joyniu-restores/offline-credits-20260912-check` 通过。实际恢复探针路径错误已修复，单独recover重新验证同次快照后状态为completed，原故障记录保留。API健康、维护门禁解除；没有覆盖生产数据。完整证据见 [发布记录](commercial-delivery-2026-09-12.md)。下列步骤仍供重新安装、演练和故障恢复使用。

修正后完整协调备份再次一次通过，最终快照 `scheduled-20260912T111611Z-708d62ea`（1039文件/2库），恢复到全新目录 `/opt/joyniu-restores/offline-credits-20260912-final` 通过。该快照已用下文工具创建用户Mac上的一次性加密副本并实际解密核验；没有改变24小时正式间隔。持续异地同步和站外告警仍未配置，不能将该次副本当作长期灾备完成。

## 边界与安装条件

- 适用于当前单机、一个 API 容器的 `api / web / cad-egress` 拓扑。多个 API、独立后台写进程或额外应用服务必须先重新核对所有写入者；脚本遇到未知运行服务会拒绝停机。
- 新镜像必须包含 `OperationsGateMiddleware` 并已注册；增加 `deploy/compose.operations.yaml` 后，由共享的 `/data/operations` 目录提供请求准入锁。直接访问 API 也受同一保护，不能只靠代理拦截。
- 备份目标、报告目标与生产目录必须分别独立，禁止备份写入 `data/`。备份含配置及认证资料，目标目录私有；后台只读聚合报告，不提供备份文件下载。
- 当前已知宿主磁盘占用约 83% 是历史快照。脚本每次重新测量空间，要求目标剩余空间大于本次来源大小的两倍加预留值（默认 5 GiB）。不足就拒绝；没有自动清理 Docker 镜像、缓存、历史人工备份或客户文件。完整源码、配置与数据都会占用备份空间，停机时长取决于实测大小。
- 单机报告无法在整个主机离线时送达告警。外部通知接收人、异地存储目标和凭据尚未提供；此版不向任何真实人员发送消息，也不声称异地复制已完成。后台对此分别标为未配置。域名、HTTPS、AI 接入及 OS 隔离沿用用户明确暂缓的范围。

## 安装（发布窗口内由运维执行）

以下默认路径对应当前部署；代码发布仍遵循 `deploy/commercial_release.md` 的备份、候选镜像与回滚流程。先确认没有运行或排队 CAD，不在客户任务中直接重建容器。

```sh
cd /opt/joyniu-cad
sudo install -d -m 0750 /etc/joyniu-cad
sudo install -d -o 10001 -g 10001 -m 0750 /opt/joyniu-cad/data/operations
sudo install -d -o root -g 10001 -m 2750 /var/lib/joyniu-cad-operations
sudo install -d -o root -g root -m 0700 /opt/joyniu-backups/scheduled
sudo install -m 0600 deploy/operations.example.json /etc/joyniu-cad/operations.json
sudo install -m 0644 deploy/systemd/joyniu-cad-*.service deploy/systemd/joyniu-cad-*.timer /etc/systemd/system/
```

在既有 `.env` 的 `COMPOSE_FILE` 末尾追加 `:deploy/compose.operations.yaml`，保留此前三个 Compose 文件和生产配置。覆盖层只增加准入保护与报告只读挂载；不更换模型、账户、出口或收费规则。部署新镜像后验证：

```sh
curl --fail http://127.0.0.1/api/v1/operations/readiness
sudo python3 deploy/commercial_operations.py monitor --config /etc/joyniu-cad/operations.json
sudo python3 deploy/commercial_operations.py backup --config /etc/joyniu-cad/operations.json
sudo systemctl daemon-reload
sudo systemctl enable --now joyniu-cad-recovery.service joyniu-cad-monitor.timer
```

准入接口应返回 `gateVersion=v1` 与 `maintenance=false`。监控首次发现无自动备份时输出严重告警并以退出码 2 结束，这是可观察的缺口。未带 `--execute` 的 backup 只写 planned 报告，不停服务。

核对可用空间、目标权限和旧备份后，把 `/etc/joyniu-cad/operations.json` 的 `backupEnabled` 改为 `true`，并验证一次空闲备份：

```sh
sudo python3 deploy/commercial_operations.py backup --config /etc/joyniu-cad/operations.json --execute
sudo systemctl enable --now joyniu-cad-backup.timer
sudo systemctl list-timers 'joyniu-cad-*'
sudo python3 deploy/commercial_operations.py monitor --config /etc/joyniu-cad/operations.json
```

只有明确开启配置并使用 `--execute` 才能停/启现有 API。备份 timer 每 15 分钟重试一次；默认有效备份间隔为 24 小时，有近期验证通过的备份时跳过。运行、排队或正在取消的 CAD、辅助计费操作、未结束上游调用、在途 HTTP 请求存在时跳过，下一轮再试。跳过不会取消任何任务。监控 timer 约每 5 分钟运行一次。

## 停写与失败恢复顺序

1. 非阻塞取得宿主操作锁；要求只有一个 healthy API，并核验准入接口。读取任务库，只读统计登记任务、旧版运行修订、计费操作及未结束调用。缺表、坏 JSON、读库失败均拒绝备份。
2. 空闲且空间足够时先持久保存恢复日志，再创建本次维护标记。新业务 HTTP 返回 503 与 `Retry-After: 60`，专用只读准入状态接口仍可读取。
3. 尝试独占请求锁，不能取得就释放本次维护标记并跳过。有请求在第一遍检查之后登记任务时，第二遍任务核查也会跳过。不会用阻塞 flock 卡住 API 事件循环。
4. 只有独占锁且二次核查为空，才保存 `stopRequested`，停止现有 API；核查容器确实已退出并第三次核对记录，调用原有完整备份及 SHA-256/SQLite 校验工具。
5. `finally` 路径始终尝试重新启动原容器，移除本次标记，并等待 healthy API 与平台路由。没有 build/pull，也不修改旧镜像或客户数据库。失败时保留恢复日志和 critical 报告，不能宣称服务已恢复。
6. 正常 SIGTERM 执行清理；systemd `ExecStopPost` 处理进程被终止后的恢复，开机 recovery unit 处理主机重启。恢复只移除本次 token 对应的标记，不覆盖另一运维操作。Docker/磁盘本身故障时仍需负责人处理；任何脚本都不能保证底层故障中成功重启。

手动恢复与查看：

```sh
sudo python3 deploy/commercial_operations.py recover --config /etc/joyniu-cad/operations.json
sudo journalctl -u joyniu-cad-backup.service -u joyniu-cad-monitor.service -n 100 --no-pager
sudo cat /var/lib/joyniu-cad-operations/backup-latest.json
sudo cat /var/lib/joyniu-cad-operations/status.json
```

恢复命令幂等；无恢复日志时不启动/停止服务。另一个备份或恢复持有宿主操作锁时，CLI 返回退出码 0、`status=skipped`、`reason=operation_in_progress`，不抢锁、不修改恢复日志、不写伪失败报告；锁取得后的实际 I/O 失败仍返回退出码 1。遇到其他 token 的维护标记时拒绝删除。不要为“恢复监控绿色”手工删客户任务状态或伪造备份清单。

恢复等待检查容器 healthy 和真实 `/api/v1/auth/account-capabilities` 路由。单独 recover 成功后，仅同一次操作的快照通过清单、部署归属及完整校验时，才把先前仅因恢复等待失败的报告改为 completed，并保留原开始时间与故障原因；备份复制失败或快照损坏仍为 failed，只将 recovery 改为 complete。这样不会把“API 已恢复”误当成“备份成功”。

## 阈值与后台展示

默认数据/备份盘预警 80%、严重 90%，或剩余低于 5 GiB 判严重。队列 10 个、最长排队 30 分钟、运行状态 60 分钟未更新、上游调用 30 分钟未结束、最近一小时上游失败 3 次触发预警；无有效备份或备份超过 26 小时触发严重告警。它们是可调整的运营阈值，不是已测吞吐或承诺完成时间。不要仅凭超时告警直接终止客户任务。

后台系统页读取 `JOYNIU_OPERATIONS_REPORT` 指定的只读报告，先校验格式和时间，只返回已列明的数值、时间和枚举；原始路径、错误、客户、凭据及自定义消息不进入 API。超过 15 分钟的报告明确显示“报告过期”，保留的数值只表示历史观测。尚无报告、格式无效或无权限读取时显示缺口。

持久报告接口约定：

| 文件 | 用途 |
| --- | --- |
| `status.json` | `format=joyniu-operations-v1`、`generatedAt`、`status=ok/warning/critical`；`checks` 为服务、磁盘、任务聚合及最近备份校验；`alerts` 为 code/severity |
| `backup-latest.json` | 最近备份计划、完成、跳过、失败及恢复结果；包含脚本生成的备份集名，不含配置正文 |
| `backup-failure.json` | 发生在停写前的配置/空间/检查异常；监控按时间与 latest 报告比较 |
| `recovery.json` | 仅宿主可读的恢复状态与操作 token；不是后台展示源 |

外部接入者应只读 `status.json`，以 generatedAt 检测失联，对 code 严重级别变化、恢复、持续失败或报告超过 15 分钟通知负责人，并做重复告警抑制。此版 `notifications.configured=false`、`offsiteBackup.configured=false` 是真实边界，不接受修改这两个字段来伪造完成。

## 备份保留与隔离恢复演练

`retentionKeep=0` 默认不产生删除候选。设置为正整数可计算候选，但仍必须另带 `--apply-retention` 才删除；timer 不带此标志。`retentionMinAgeDays` 至少一天，默认七天。至少保留最新一个脚本备份，且只处理本工具命名、带部署绑定标记、清单哈希相符、完整校验通过的直接子目录。有额外文件、篡改、符号链接或不属于本工具的旧人工备份均不自动删除。所有候选先验完才开始删除；生产 `data/` 不在任何清理范围。

```sh
sudo python3 deploy/commercial_operations.py retention --config /etc/joyniu-cad/operations.json
# 已人工核对上条列出的候选、配置已设置 retentionKeep 后才执行：
sudo python3 deploy/commercial_operations.py retention --config /etc/joyniu-cad/operations.json --apply-retention

# 将 SET 换成本次报告中确切的 scheduled-... 目录名，TARGET 必须不存在。
sudo python3 deploy/commercial_backup.py verify --backup /opt/joyniu-backups/scheduled/SET
sudo python3 deploy/commercial_backup.py restore --backup /opt/joyniu-backups/scheduled/SET --destination /opt/joyniu-restores/TARGET
```

恢复后核对两库 quick_check、清单表行数、原图/模型哈希及账号/钱包/云项目读取；仅在隔离副本演练。需要生产数据回退时，仍必须先保留现状并核对发布后的充值、冲正、账本与客户操作，不能拿旧库覆盖新业务。两个协调备份运行文件 `data/operations/admission.lock`、`maintenance.json` 不进入快照，并记录排除原因；同目录其他文件继续完整保存。认证、模型、图纸及客户资料无额外过滤。

客户资料删除、保留期限、法定/业务审计和异地副本删除需要单独的客户范围及执行记录，本脚本不执行客户数据清除。尚未同步至服务器的浏览器本地项目不在宿主备份内。异机恢复所需固定镜像归档、受保护异地复制及实际恢复验证仍须在目标提供后完成。

## 一次性加密异地副本工具

`deploy/pull_encrypted_backup.py` 只拉取指定的既有完整快照，不发起生产停机或创建定时任务。远端先验证停写清单、全部哈希及应用数据库，再以SSH流式传输；本地再次验证后用AES-256-GCM加密。定时备份管理标记必须与快照名和清单哈希一致，标记本身不进入密文。

密文和随机密钥分别存到两个当前用户拥有的0700目录，文件0600，拒绝覆盖。密钥不上传服务器、不输出日志、不进入仓库。`verify` 在临时私有目录解密并校验，正常退出后清理明文；`restore` 只允许全新隔离目录。36项工具及原备份测试覆盖篡改、错误密钥、目录越界、符号链接、大小/超时、管理标记及重复路径。

示例（自行使用实际SSH密钥和已经验证的完整快照路径）：

```sh
apps/api/.venv/bin/python deploy/pull_encrypted_backup.py \
  --host ubuntu@122.51.168.205 --identity /private/ssh/server.pem \
  --backup /opt/joyniu-backups/scheduled/完整快照名 --sudo \
  --destination /private/backups/new.enc \
  --key-file /private/keys/new.key
apps/api/.venv/bin/python deploy/pull_encrypted_backup.py verify \
  --source /private/backups/new.enc --key-file /private/keys/new.key
```

该工具与实际一次性复制不能替代持续异地同步、异机镜像归档或站外告警。后台持续异地监控未配置时仍显示未配置。

## 本地验证命令

```sh
apps/api/.venv/bin/python -m pytest apps/api/tests/test_commercial_backup.py apps/api/tests/test_commercial_operations.py apps/api/tests/test_operations_gate.py apps/api/tests/test_operations_report.py apps/api/tests/test_operations_real_routes.py apps/api/tests/test_admin_operations.py apps/api/tests/test_admin_operations_v2.py -q
node --test --test-concurrency=1 src/adminOperationsReport.test.js src/adminOperationsRender.test.js
```

当前后端 132 项、前端 13 项通过；其中恢复专项 42 项包含真实 account router + 本地 Uvicorn HTTP 路由验证，以及独立子进程的 CLI 锁占用回归。只模拟 Docker 状态，不模拟恢复探针的 HTTP 响应；旧 `/api/v1/auth/capabilities` 路由的真实 404 也被确认。

专项覆盖：WAL 和两库恢复、原图/假配置完整性、运行/排队/取消/辅助计费/上游/旧任务阻断、第一二次检查竞态、在途 HTTP、停机/复制/校验失败后恢复、重启失败留痕、崩溃日志重放、维护所有权、空间与未知写入者拒绝、默认保留、受限删除、后台字段脱敏与过期报告。未执行真实 AI、收款、对外通知或生产停机测试。Linux systemd 实际安装和一次真实空闲窗口备份须由发布流程验证后才可记为线上启用。
