# 微信 Native 支付接入（2026-09-10）

本模块包含真实微信接口适配代码和本地签名测试，尚未进行商户实付验收。未配置支付资料时保持关闭，不生成演示收款码；积分计费和真实退款还分别受服务端开关控制。本次没有读取商户密钥或发起真实支付/退款。

## 安装与配置

API 的 `payment` 可选依赖为 `cryptography>=50.0.1,<51`。在部署环境安装 `apps/api` 的 `.[payment]` 扩展。调用 `WechatPayNativeAdapter.from_env()` 构造适配器，应用层传给 `BillingService`。

环境变量均由服务器运维配置，不接受浏览器传值：

| 变量 | 用途 |
| --- | --- |
| `JOYNIU_WECHAT_MCH_ID` | 普通直连商户号 |
| `JOYNIU_WECHAT_APP_ID` | 与商户绑定的应用 ID |
| `JOYNIU_WECHAT_MERCHANT_SERIAL` | 商户 API 证书序列号 |
| `JOYNIU_WECHAT_PRIVATE_KEY_FILE` | 本地商户 RSA 2048 私钥 PEM 文件路径 |
| `JOYNIU_WECHAT_API_V3_KEY_FILE` | 本地 32 字节 APIv3 密钥文件路径，允许末尾换行 |
| `JOYNIU_WECHAT_API_V3_KEY` | 不用密钥文件时的环境变量替代；同时存在时文件优先 |
| `JOYNIU_WECHAT_PLATFORM_PUBLIC_KEY_ID` | 平台公钥 ID，例如 `PUB_KEY_ID_…` |
| `JOYNIU_WECHAT_PLATFORM_PUBLIC_KEY_FILE` | 对应微信平台 RSA 2048 公钥 PEM 路径 |
| `JOYNIU_WECHAT_NOTIFY_URL` | HTTPS `/api/v1/billing/payment-notifications/wechatpay` 外网地址 |
| `JOYNIU_WECHAT_REFUND_NOTIFY_URL` | HTTPS `/api/v1/billing/refund-notifications/wechatpay` 外网地址 |

回调 URL 禁止查询参数、片段和用户名密码。平台公钥 ID 与文件必须成对更新；不按来路请求自动下载公钥或降级跳过验签。使用公钥模式，不包含自动平台证书下载。仅具备配置不等于商户侧的产品权限已开通。

`BillingService(..., refunds_enabled=False)` 为默认状态。真实退款只有在显式启用并提供完整退款配置后才开放。商业计价规则和套餐价格由运营另行确定，本模块不设置生产价格。

## 支付和查询

只向 `https://api.mch.weixin.qq.com` 请求；系统 TLS 校验、10 秒超时、不跟随重定向。请求对原始 HTTP 方法、路径/查询串、时间戳、随机串和请求体进行 RSA-SHA256 签名；响应和回调先验证平台签名，再读取内容，时间偏差超过 5 分钟拒绝。回调用 APIv3 密钥做 AES-256-GCM 解密。

服务器先持久保存订单价格及积分快照，再请求 Native 收款码。36 字符内部订单号 `ord_<uuid>` 可逆转换为 32 字符 UUID，符合微信订单号长度；回调无需依赖进程内存映射。只返回微信签发的 `weixin://wxpay/bizpayurl`，前端自行在本地渲染二维码，不向外部二维码服务发送支付链接。

`POST /api/v1/billing/orders/{id}/reconcile` 请求体 `{}`。仅订单所有者或管理员可查询，返回 `{order, gatewayStatus}`。服务端同一订单每 15 秒一次、每账号每分钟 12 次，超限返回 429 和 `Retry-After`。刷新访问令牌不会重置这些持久限流记录。

只有验证完成的支付事件才能改变订单和积分；核对商户、AppID、订单号、支付类型、交易流水、币种和服务器金额。查询与回调走同一幂等记账逻辑，迟到/重复回调不会重复加积分，退款后也不会重新加回积分。

## 退款执行规则（默认关闭，待运营确认开通）

当前仅支持整单退款，部分退款保留申请和人工审核，因没有确定积分比例规则，不调用渠道也不假标成功。

1. 管理员审核只记为 `approved`，不表示钱已退。
2. 管理员调用 `POST /billing/admin/requests/{id}/refund`，请求体 `{}`。同一申请/订单只创建一个持久退款执行记录；请求重试、令牌更新、进程重启不会创建新退款号。
3. 必须已付款、审核通过、全额退款且当前积分足额回收。提交微信前以独立、幂等的退款账本收回该充值单全部积分；不足时不扣款、不发退款。该操作属于退款处理，不为 CAD 任务引入积分冻结。
4. 退款 API 返回受理不直接表示退款完成；状态先为处理中。只有后续验签查询/回调确认为成功，才记为 `refunded`。`ABNORMAL`/超时不归还已回收积分，需继续核账；明确 `CLOSED` 时幂等归还积分。
5. `POST /billing/admin/requests/{id}/refund/reconcile`，请求体 `{}`，可查询真实退款结果。若进程在扣回积分后、发送 HTTP 前中断，超过 60 秒且渠道验签查询明确 `RESOURCE_NOT_EXISTS` 时，仅用相同退款号及金额恢复提交，不二次扣积分。关闭退款开关后仍可接收已发退款的签名结果，但不恢复发送新 HTTP 退款。
6. 退款进入成功或关闭终态后，迟到的处理中事件不会倒退状态；矛盾终态拒绝处理，保留人工核账。一个订单只能执行一笔整单退款，累计金额不能超实付。

执行和查询返回 `{request, refund, order, message?}`。`refund.status` 为 `submitting/unknown/processing/succeeded/closed/abnormal`；面向申请列表的状态为 `refund_processing/refunded/refund_closed/refund_abnormal`。管理员审核接口不能覆盖已执行退款状态。

部署启用前仍需真实商户资料、微信 Native 产品权限、公开 HTTPS 回调，以及小额实付/到账/退款/退款关闭及断网恢复验收。代码测试使用临时本地 RSA 密钥和加密回调夹具，不能替代商户端实际验收。

协议依据：[Native 下单](https://pay.wechatpay.cn/doc/v3/merchant/4012791877)、[商户订单查询](https://pay.wechatpay.cn/doc/v3/merchant/4012791880)、[微信公钥验签](https://pay.wechatpay.cn/doc/v3/merchant/4013053249)、[官方 SDK 请求签名](https://github.com/wechatpay-apiv3/wechatpay-go/blob/main/core/auth/credentials/wechat_pay_credential.go)、[官方普通商户退款 SDK](https://github.com/wechatpay-apiv3/wechatpay-go/blob/main/services/refunddomestic/api_refunds.go)、[普通商户退款回调与解密](https://pay.wechatpay.cn/doc/v3/merchant/4012791906)。
