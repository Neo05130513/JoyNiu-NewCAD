# 恢复副本校验修复：脱敏证据

2026-09-13。r7 新库完整 smoke 通过，恢复副本完整 smoke 在最终“原数据库行保持”阶段失败；21 项几何检查当时全部通过。此目录仅记录诊断、检查器修复及 r8 冻结核对，不把它当作 r8 最终生产激活结果。

## 实际根因与隔离复现

使用冻结 r7 API 镜像启动独立一次性容器，只读挂载已验证恢复源和冻结脚本，将数据复制到容器私有 tmpfs 后执行。没有挂载 live data、用户凭据或 Docker socket；无模型调用。按实际 AuthService 创建新合成账号、真实 OCCT 长方体预览、启动真实 uvicorn 并进行本地 HTTP 登录，逐阶段比较全部原数据库表的行哈希。

- 创建合成账号：全部原行保留。
- 真正预览：cad-feature-workspace/features.sqlite3 的 manual_feature_previews 删除 8 条已在基线前超过 1800 秒的缓存；新添 1 条合成预览。数据库内保存版本和提交记录不变。
- API 启动：没有额外原行变化。
- 真正 HTTP 登录：joyniu.sqlite3 的 auth_rate_limits 删除 1 条基线前已过期记录；该身份未重新插入。新添 2 条合成登录限流记录。
- 原有 20 个会话和 20 个刷新令牌的父会话均未过期，未发生删除。所有其他原表无缺失行。
- 只读恢复源全部文件 SHA 保持不变；容器结束即删除。

完整脱敏阶段输出在 r7-isolated-restoration-diagnosis.jsonl，仅包含库/表名、计数和不可逆行哈希，不包含账号值或认证凭据。

## 最小修复范围

只改 deploy/currentcad_release_smoke.py 的恢复验收协议，应用业务源码不变。既有 auth_rate_limits 例外不扩大。新增例外仅对固定 cad-feature-workspace/features.sqlite3 路径的 manual_feature_previews 生效，要求合法 preview_32hex ID、有限创建时间严格早于 smoke 基线减 1800 秒，并匹配完整原始行哈希。

只允许这些精确过期缓存行被删除；同 ID 复用、payload/owner/created 修改、未过期或恰好位于基线边界的预览删除仍拒绝。manual_features、manual_feature_commits、会话、计费等业务表保持原逐行保护。报告分别输出两类缓存的 allowed/removed 数量、真实截止时间和明确例外说明；有缓存删除时 originalDatabaseRowsPreserved=false，requiredOriginalRowsPreserved 和 businessRowsPreserved 只有严格检查通过才为 true。

## 验证与冻结

- 最终唯一组合 167 项通过，本轮新增 22 项：13 项规则边界、1 项真实 OCCT/恢复/HTTP 链路、8 项发布器报告保护组合。
- 日志分两批：最终恢复+真实 HTTP+基础发布器 80 项；已有组合中的编辑器发布器 84 项、核心编辑器几何/接线 3 项。两份日志的基础发布器有重复，不能将日志总点数直接相加。final-test-collection.log 是最终唯一 167 项清单。
- 真实恢复回归验证原已保存 STEP 对应 3200 mm³ 实体仍保留，有效预览保留，过期预览删除，新账号无法读取原账号模型，源文件不变。未放松业务断言。
- 两条 multiprocessing fork deprecation warning 来自已有发布器故障恢复测试，不是测试失败。
- r8 检查器 SHA-256：0cf1cc7168137aa6b791c90fa320ceff6938ef270134db6d3a6043608d08cc97。
- r8 sourceAndBuildSha256：05952dab9b1898724f802b99a8f5ae64e126af48e3324a8cf3f3799c69a9c211。
- r8 tar SHA-256：35c3bb156678f25f71cb2a0ca5a7920516c2e99890f3ae8c1fda8900584206c9。
- 冻结清单 490 项逐项校验通过；tar 实际 492 文件与清单及两个生成标识完全一致。
- apps/api/app 92 件、src 246 件、dist/assets 4 件与 r7 逐 SHA 完全相同。清单仅检查器与两份文档变化。
- r8 冻结 gate 可接受实际 21 checks、16 STEP、2 ZIP、1 GLB 的完整证据；binder 动态绑定 r8 镜像、容器、源码和报告，兼容严格保存字段。

r7 失败证据未覆盖；r7/r8 冻结候选未修改。本目录在 r8 冻结后另行归档，不在该候选清单内。r8 完整恢复 smoke、备份与生产激活由主线程执行并单独记录。
