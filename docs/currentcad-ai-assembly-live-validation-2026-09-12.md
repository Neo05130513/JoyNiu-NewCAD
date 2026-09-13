# 真实 AI 装配开发验收（2026-09-12）

检查时间：2026-09-12T11:34:37.259160+00:00

结果：通过。任务状态 `review_required`，总耗时 25.873 秒。

调用现有 `CodexCadProvider`，使用本机 CLI 管理的登录；并非测试回复或模型夹具。仅在本次临时 Python 进程设置 codex 与 180 秒规划超时，使用独立临时鉴权、计费、任务数据库和工程文件目录。未修改持久环境，未重启 8016/8012。

Provider：`codex`；模型：`gpt-6-astra`；reasoning effort：`low`。实际调用次数 1，无供应商调用重试。规划耗时 20.1911 秒。

## 输入

创建一个由两个完全相同立方体组成的装配。每个立方体边长 20 mm，局部原点都是立方体最小角点，实体在局部坐标 X、Y、Z 的范围都是 0 到 20 mm。实例 A 的位置为 (0,0,0) mm，旋转为 (0,0,0) 度，固定。实例 B 的位置为 (30,0,0) mm，旋转为 (0,0,0) 度，也固定。严格按这两个指定位置生成装配，不增加任何孔、倒角、间隙、特征或额外配合，constraints 为空列表。相同零件可作为一个零件定义和两个实例。

## 用量与计费边界

走真实 `MeteredCadProvider`、`CadJobRegistry`、`BillingService` 和 `BillingPolicyService`。测试策略禁收费，没有费率、充值或用户积分扣款；本次实际模型调用仍会使用本机登录账户的供应商额度，不等于供应商成本为零。未知 usage 字段保留 null，未折算为零。

```json
[
  {
    "attemptId": "attempt_88c2dabdf4094e528899306680d6c1fa",
    "cachedInputTokens": 0,
    "callId": "call_d747562ff9f5482fa6739331b6448fbf",
    "costMicroUsd": null,
    "costSource": null,
    "inputTokens": 9819,
    "jobId": "job_7d6bb59a045c4ffcb18d357beb560705",
    "model": "gpt-6-astra",
    "outcome": "completed",
    "outputTokens": 181,
    "ownerId": "usr_c9bc377591a3402f8d3417869e8f1da2",
    "provider": "codex",
    "rateCardVersion": null,
    "reasoningOutputTokens": 0,
    "stage": "assembly_planner",
    "usageKnown": true,
    "usageScope": "aggregate",
    "createdAt": "2026-09-12T11:34:57.454441+00:00"
  }
]
```

## 实体核验

AI 生成了一个参数化立方体零件定义，参数 `L=20` 引用用户原文“每个立方体边长 20 mm”，再建立 A、B 两个实例。没有额外特征或配合。

| 核验项目 | STEP / 装配实际结果 |
| --- | --- |
| 单个零件 | 有效封闭实体 1 个，20×20×20 mm，体积 8000 mm³ |
| 装配 | 有效封闭实体 2 个，12 个面、24 条边 |
| A / B 位置 | (0,0,0) / (30,0,0) mm，均无旋转且固定 |
| 装配外包尺寸 | 50×20×20 mm，最小角点 (0,0,0) |
| 装配体积 | 15999.999999999993 mm³，与 16000 mm³ 在数值容差内相同 |
| BRep 干涉 | 0 mm³ |
| BOM | 立方体，数量 2 |
| 输出 | STEP、真实三角化 GLB、BOM CSV |

- statusReviewRequired: PASS
- twoInstances: PASS
- twoSolids: PASS
- validBRep: PASS
- volume16000: PASS
- bbox50x20x20: PASS
- minOrigin: PASS
- zeroInterference: PASS
- bomQuantity2: PASS
- glbHeader: PASS
- noExtraMates: PASS
- eachPartCube20: PASS
- oneActualProviderCall: PASS
- noLocalCreditDebit: PASS
- actualUsageRecorded: PASS
- productionReadyRemainsFalse: PASS

## 来源与结果

- 独立证据与实际工件目录：`/var/folders/7l/7gm7qtgj14d4gyp7f34q1yl40000gn/T/joyniu-ai-assembly-live-xzsob636`
- 完整证据：`/var/folders/7l/7gm7qtgj14d4gyp7f34q1yl40000gn/T/joyniu-ai-assembly-live-xzsob636/evidence.json`
- 任务：`assembly_387025e84ef64db5beac057f822255a9`
- 装配设计：`design_42933e1fc9a042419b35b4d886c66e4b`
- STEP SHA256：`f0cbf1a31f2d18d68927830c537cf71bee2f01a0b2dc4586c4f0fc5c666d5e3c`

保留原始需求、AI JSON 计划、逐零件执行结果、真实 STEP/GLB、配合与干涉/BOM 信息。几何有效性不等于生产交付确认；结果仍为 `review_required`、`productionReady=false`。

## 失败信息（如有）

```json
{
  "errors": [],
  "questions": [],
  "providerError": null,
  "diagnostics": {
    "protocol": "json",
    "eventCount": 4,
    "terminalEventCount": 1,
    "responseBytes": 981,
    "outputChars": 491,
    "terminalStatus": "completed",
    "firstByteSeconds": 0.4077,
    "codexEventType": "turn.completed",
    "elapsedSeconds": 20.1911,
    "codexItemType": "agent_message",
    "firstOutputTextSeconds": 16.6099,
    "usage": {
      "input_tokens": 9819,
      "output_tokens": 181,
      "cached_tokens": 0,
      "reasoning_tokens": 0
    },
    "streamEndReason": "terminal_event"
  }
}
```
