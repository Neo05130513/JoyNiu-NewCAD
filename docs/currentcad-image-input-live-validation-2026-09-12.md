# 带图片输入真实 AI 阶段验收（2026-09-12）

结果：阶段集成通过，实际执行耗时 63.219 秒，两次真实 Codex CLI 调用，没有重试。输入为已有 OCCT 齿轮的合成俯视图 960×720；不是实拍照片，不能据此声称实拍识别准确率。

## 实际链路与边界

生产附件预处理 → `CadSourceReader.read` → `CodexCadProvider` → `CadAgentService._call` / 原 Agent 协议和 `_parse_action` → `validate_plan` → `execute_cad_plan` 隔离 OCCT worker → STEP 读回。前两次调用均真的附图；没有使用视觉响应夹具或手工填写返回计划。

为满足最多两次真实调用，本次是同一生产组件的阶段集成，未执行完整 `CadAgentService.run` 的多轮观测、辅助空间解释、独立图纸审核和交付流程。规划阶段已明确提供精确用户尺寸并要求输出完整最小 gear 计划。不能把本报告写成完整照片自动交付验收。

仅脚本当前进程设置 `JOYNIU_CAD_PROVIDER=codex`。输出位于独立 `/tmp/joyniu-image-live-20260912`；未调用计费或账户数据库，没有本地积分扣款，也未创建测试收费策略。真实模型调用会消耗本机登录账户的供应商额度。未改持久环境、未重启 8016/8012、未改变生产确认门禁。

## 图片真实进入视觉 Provider 的证据

- 原始 PNG SHA256：`93a2a55483500670b1a3ed75d6f4d8f7d10419f34ff01324b4805b17f4da4d14`。
- 两次预处理后 JPEG 均为 39026 字节，SHA256：`d365ebe9acb285590a20f39a67cca562873d4252863b9c2c4e9fcdbbfaac1d2e`。
- 审计实际 `_argv`：两次均含图片参数，`imageCount=1`、`usesImageFlag=true`；请求内容各含一个 `input_image`。
- 模型 `gpt-6-astra`；独立读取 effort=medium，规划 effort=low。
- 读取返回无尺寸标注，保存为 `candidateEvidence=true, verified=false`，疑问没有伪装成已知尺寸。SourceReader 对无尺寸照片不会必然报错，因此本次无需修改产品代码。

## 用户尺度与真实实体

| 项目 | 用户给定 | AI 计划/内核值 |
| --- | --- | --- |
| 模数 | 2 mm | 2 mm |
| 齿数 | 24 | 24；实体齿顶圆柱面24个、齿根圆柱面24个 |
| 压力角 | 20° | 20° |
| 齿宽 | 15 mm | 15 mm |
| 中心通孔 | 12 mm | 圆柱面直径12 mm |
| 变位/侧隙 | 0 / 0 | 0 / 0 |

所有精确参数 `source.type=user` 并引用用户原文。标定点 (100,100)→(300,100)、长度20mm仅作为用户提供的参考平面信息；AI 没有按图片像素比例篡改明确给定的齿轮尺寸。该点线在此合成图片中不是经过物理标尺验证的实拍标定，不构成标定精度证明。

实际实体：有效封闭 solid=1，147面、435边；包络52×52×15 mm（数值容差约2e-7 mm）；体积 24934.832344048 mm³。隔离 worker 4112 ms、40秒超时、4096MB限制，禁止任意代码。STEP 独立读回如下：

```json
{
  "valid": true,
  "solidCount": 1,
  "volumeMm3": 24934.832344047245,
  "bbox": {
    "min": [
      -26.000000099999998,
      -26.000000099999937,
      -1e-07
    ],
    "max": [
      26.000000099999998,
      26.0000001,
      15.0000001
    ],
    "size": [
      52.000000199999995,
      52.00000019999994,
      15.000000199999999
    ]
  }
}
```

STEP SHA256：`873ccbd56d769d09a4b15af6abf1bde5d16934cc3590d96bebebefaa27b30935`。STEP/GLB 和投影保存在 `/tmp/joyniu-image-live-20260912/kernel/`。`drawingAgreement=not_checked`、`productionReady=false`；没有伪造原图核对或人工确认。

## 实际用量

```json
[
  {
    "number": 1,
    "inputImageCount": 1,
    "timeoutSeconds": 64.9847641660017,
    "startedSeconds": 0.606,
    "elapsedSeconds": 20.982,
    "usage": {
      "input_tokens": 5689,
      "output_tokens": 11,
      "cached_tokens": 2816,
      "reasoning_tokens": 0
    }
  },
  {
    "number": 2,
    "inputImageCount": 1,
    "timeoutSeconds": 65,
    "startedSeconds": 21.607,
    "elapsedSeconds": 37.493,
    "usage": {
      "input_tokens": 10062,
      "output_tokens": 652,
      "cached_tokens": 0,
      "reasoning_tokens": 0
    }
  }
]
```

未把未知供应商价格或未核算成本写为零。

## 可复核证据

本报告旁的 `docs/evidence/currentcad-image-live-2026-09-12/` 保存原始合成 PNG、完整原文、真实两次响应、AI action/计划、转录记录、内核执行结果、非秘密审计数据和验收脚本。临时目录保留真实 STEP/GLB。

所有检查：

```json
{
  "twoActualCalls": true,
  "bothCallsIncludeImages": true,
  "knownDimensionsExact": true,
  "kernelValid": true,
  "sourceOnlyCandidate": true,
  "stepReadbackValid": true,
  "tipFaceCount24": true,
  "bore12": true,
  "sourceAllKnownDimensionsUser": true
}
```
