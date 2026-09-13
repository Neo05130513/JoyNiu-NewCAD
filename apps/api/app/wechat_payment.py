"""WeChat Pay API v3 Native adapter for ordinary domestic merchants.

No credentials, certificates or merchant endpoints are discovered remotely.
The configured platform public key is pinned, not selected by an untrusted URL.
Billing must persist and compare verified events against its own order/refund
snapshots before granting credits or recording a refund as completed.

Protocol references (checked 2026-09-10):
https://pay.wechatpay.cn/doc/v3/merchant/4012791877
https://pay.wechatpay.cn/doc/v3/merchant/4012791880
https://pay.wechatpay.cn/doc/v3/merchant/4013053249
https://pay.wechatpay.cn/doc/v3/merchant/4012791906
https://github.com/wechatpay-apiv3/wechatpay-go/tree/main/services/refunddomestic
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .billing import BillingUnavailable, VerifiedPaymentEvent, VerifiedRefundEvent


API_HOST = "api.mch.weixin.qq.com"
MAX_BODY = 1024 * 1024
SIGNATURE_TYPE = "WECHATPAY2-SHA256-RSA2048"
REFUND_STATES = {"PROCESSING": "processing", "SUCCESS": "succeeded",
                 "CLOSED": "closed", "ABNORMAL": "abnormal"}


class WechatPayError(BillingUnavailable):
    """Safe public error; never include HTTP bodies, keys or local file paths."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _invalid():
    return WechatPayError("invalid_payment_data", "微信支付数据校验未通过，订单状态未变更。")


def _string(value, *, maximum=128, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not value and not empty):
        raise _invalid()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _invalid()
    return value


def _money(value):
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise _invalid()
    return value


def _gateway_id(value, prefix):
    # Billing IDs are 36 characters; Native out_trade_no allows at most 32.
    # This bijection survives restarts and never needs an in-memory lookup.
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[0-9a-f]{32}", value):
        raise _invalid()
    return value[len(prefix) + 1:]


def _local_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise _invalid()
    return prefix + "_" + value


def _object(value):
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _json(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        if not isinstance(body, bytes) or not body or len(body) > MAX_BODY:
            raise ValueError()
        return _object(json.loads(body.decode("utf-8"), object_pairs_hook=unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None


def _https_url(value):
    try:
        _string(value, maximum=255)
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.fragment or url.query or url.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError()
        return value
    except (ValueError, WechatPayError):
        raise _invalid() from None


@dataclass(frozen=True)
class WechatPayConfig:
    mch_id: str
    app_id: str
    merchant_serial: str
    platform_public_key_id: str
    notify_url: str
    api_v3_key: bytes = field(repr=False)
    private_key_pem: bytes = field(repr=False)
    platform_public_key_pem: bytes = field(repr=False)
    refund_notify_url: str = ""


class WechatPayNativeAdapter:
    name = "wechatpay"

    def __init__(self, config: WechatPayConfig | None = None, *,
                 transport: Callable | None = None, clock: Callable = time.time):
        self.configured = False
        self.refunds_configured = False
        self.configuration_error = "微信支付尚未配置。"
        self._config = None
        self._clock = clock
        self._transport = transport or self._https_request
        if config is None:
            return
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            if (not re.fullmatch(r"[0-9]{6,32}", config.mch_id)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", config.app_id)
                    or not re.fullmatch(r"[0-9A-Fa-f]{1,64}", config.merchant_serial)
                    or not re.fullmatch(r"PUB_KEY_ID_[0-9]{1,64}", config.platform_public_key_id)
                    or not isinstance(config.api_v3_key, bytes) or len(config.api_v3_key) != 32):
                raise ValueError()
            _https_url(config.notify_url)
            if config.refund_notify_url:
                _https_url(config.refund_notify_url)
            private = serialization.load_pem_private_key(config.private_key_pem, password=None)
            public = serialization.load_pem_public_key(config.platform_public_key_pem)
            if (not isinstance(private, rsa.RSAPrivateKey) or private.key_size != 2048
                    or not isinstance(public, rsa.RSAPublicKey) or public.key_size != 2048):
                raise ValueError()
            self._private_key = private
            self._platform_key = public
            self._config = config
            self.configured = True
            self.refunds_configured = bool(config.refund_notify_url)
            self.configuration_error = ""
        except ImportError:
            self.configuration_error = "微信支付加密组件尚未安装。"
        except (ValueError, TypeError, WechatPayError):
            self.configuration_error = "微信支付配置校验未通过，请联系管理员。"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **kwargs):
        env = os.environ if environ is None else environ
        def setting(name):
            return env.get("JOYNIU_WECHAT_" + name, "").strip()
        # Do not read files at all until every mandatory setting is present.
        required = ("MCH_ID", "APP_ID", "MERCHANT_SERIAL", "PLATFORM_PUBLIC_KEY_ID",
                    "NOTIFY_URL", "PRIVATE_KEY_FILE", "PLATFORM_PUBLIC_KEY_FILE")
        if not all(setting(k) for k in required) or not (setting("API_V3_KEY") or setting("API_V3_KEY_FILE")):
            return cls(**kwargs)
        try:
            def read(name):
                with Path(setting(name)).expanduser().open("rb") as handle:
                    value = handle.read(65537)
                if len(value) > 65536:
                    raise ValueError()
                return value
            # APIv3 key files can end with a conventional line terminator.
            key = (read("API_V3_KEY_FILE").rstrip(b"\r\n") if setting("API_V3_KEY_FILE")
                   else setting("API_V3_KEY").encode("utf-8"))
            config = WechatPayConfig(
                mch_id=setting("MCH_ID"), app_id=setting("APP_ID"),
                merchant_serial=setting("MERCHANT_SERIAL"),
                platform_public_key_id=setting("PLATFORM_PUBLIC_KEY_ID"),
                notify_url=setting("NOTIFY_URL"), api_v3_key=key,
                private_key_pem=read("PRIVATE_KEY_FILE"),
                platform_public_key_pem=read("PLATFORM_PUBLIC_KEY_FILE"),
                refund_notify_url=setting("REFUND_NOTIFY_URL"))
            return cls(config, **kwargs)
        except (OSError, ValueError, TypeError):
            adapter = cls(**kwargs)
            adapter.configuration_error = "微信支付配置文件无法读取，请联系管理员。"
            return adapter

    def _require(self, *, refund=False):
        if not self.configured:
            raise WechatPayError("payment_unconfigured", self.configuration_error)
        if refund and not self.refunds_configured:
            raise WechatPayError("refund_unconfigured", "微信退款回调尚未配置，请联系管理员。")

    @staticmethod
    def _https_request(method, path, headers, body):
        # No redirects, environment proxies, configurable hosts, or automatic
        # retries of mutations. An uncertain request must be reconciled by ID.
        connection = http.client.HTTPSConnection(API_HOST, timeout=10, context=ssl.create_default_context())
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_BODY + 1)
            if len(payload) > MAX_BODY:
                raise _invalid()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def _verify_signature(self, body, headers):
        self._require()
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            if not isinstance(body, bytes) or len(body) > MAX_BODY:
                raise ValueError()
            values = {}
            for key, value in headers.items():
                key = key.lower()
                if key in values:
                    raise ValueError()
                values[key] = value
            timestamp = _string(values.get("wechatpay-timestamp"), maximum=12)
            nonce = _string(values.get("wechatpay-nonce"), maximum=128)
            signature = _string(values.get("wechatpay-signature"), maximum=512)
            if (values.get("wechatpay-serial") != self._config.platform_public_key_id
                    or values.get("wechatpay-signature-type", SIGNATURE_TYPE) != SIGNATURE_TYPE
                    or not re.fullmatch(r"[0-9]{1,12}", timestamp)
                    or abs(self._clock() - int(timestamp)) > 300):
                raise ValueError()
            signed = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
            self._platform_key.verify(base64.b64decode(signature, validate=True), signed,
                                      padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, TypeError, AttributeError, InvalidSignature, WechatPayError):
            raise WechatPayError("payment_signature_invalid", "微信支付签名校验未通过，订单状态未变更。") from None

    def _request(self, method, path, data=None):
        self._require()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        body = b"" if data is None else json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        timestamp, nonce = str(int(self._clock())), secrets.token_hex(16)
        signed = f"{method}\n{path}\n{timestamp}\n{nonce}\n".encode() + body + b"\n"
        signature = base64.b64encode(self._private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).decode()
        config = self._config
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "JoyNiu-CAD/WechatPayNative",
                   "Wechatpay-Serial": config.platform_public_key_id,
                   "Authorization": f'{SIGNATURE_TYPE} mchid="{config.mch_id}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{config.merchant_serial}",signature="{signature}"'}
        try:
            status, response_headers, response_body = self._transport(method, path, headers, body)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise WechatPayError("payment_response_unknown", "微信支付暂未返回明确结果，请查询当前订单状态，勿重复付款。") from None
        self._verify_signature(response_body, response_headers)
        if not 200 <= status < 300:
            # Provider messages may include request data; never expose them.
            error = _json(response_body)
            if method == "GET" and status == 404 and error.get("code") == "RESOURCE_NOT_EXISTS":
                raise WechatPayError("payment_resource_not_exists", "微信未查到此渠道订单，请核对原订单记录。")
            raise WechatPayError("payment_gateway_rejected", "微信支付未受理本次请求，请查询当前订单状态或联系管理员。")
        return _json(response_body)

    def _order(self, order):
        if order.get("currency") != "CNY" or order.get("provider") != self.name:
            raise _invalid()
        return _gateway_id(order.get("id"), "ord"), _money(order.get("amountFen"))

    def create_order(self, order: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require()
        out_trade_no, total = self._order(order)
        try:
            created = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError()
            expires = created + timedelta(minutes=30)
            # Keep expiry stable across retries of the same durable order.
            if not 60 <= expires.timestamp() - self._clock() <= 1800 + 300:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise WechatPayError("payment_order_expired", "支付订单已过期，请重新创建充值订单。") from None
        expires_at = expires.astimezone(timezone.utc).isoformat(timespec="seconds")
        package = _object(order.get("package"))
        description = _string(package.get("name"), maximum=100)
        payload = {"appid": self._config.app_id, "mchid": self._config.mch_id,
                   "description": "JoyNiu CAD · " + description, "out_trade_no": out_trade_no,
                   "attach": order["id"], "time_expire": expires_at,
                   "notify_url": self._config.notify_url,
                   "amount": {"total": total, "currency": "CNY"}}
        response = self._request("POST", "/v3/pay/transactions/native", payload)
        code_url = _string(response.get("code_url"), maximum=512)
        url = urlsplit(code_url)
        if (url.scheme != "weixin" or url.netloc != "wxpay" or url.path != "/bizpayurl"
                or not url.query or url.fragment):
            raise _invalid()
        return {"codeUrl": code_url, "expiresAt": expires_at}

    def _payment_identity(self, data, *, paid=False):
        if (data.get("mchid") != self._config.mch_id or data.get("appid") != self._config.app_id
                or (data.get("trade_type") != "NATIVE" and (paid or data.get("trade_type") is not None))):
            raise _invalid()
        order_id = _local_id(data.get("out_trade_no"), "ord")
        if "attach" in data and data["attach"] != order_id:
            raise _invalid()
        if not paid and "amount" not in data:
            return order_id, None
        amount = _object(data.get("amount"))
        if amount.get("currency") != "CNY":
            raise _invalid()
        return order_id, _money(amount.get("total"))

    def _payment_event(self, data, event_id):
        order_id, total = self._payment_identity(data, paid=True)
        if data.get("trade_state") != "SUCCESS":
            raise _invalid()
        return VerifiedPaymentEvent(self.name, _string(event_id), order_id,
                                    _string(data.get("transaction_id"), maximum=32), total)

    def query_order(self, order: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require()
        out_trade_no, total = self._order(order)
        data = self._request("GET", f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={self._config.mch_id}")
        order_id, received_total = self._payment_identity(data, paid=data.get("trade_state") == "SUCCESS")
        if order_id != order["id"] or (received_total is not None and received_total != total):
            raise _invalid()
        state = data.get("trade_state")
        states = {"SUCCESS": "paid", "REFUND": "refund", "NOTPAY": "pending_payment",
                  "CLOSED": "closed", "REVOKED": "closed", "USERPAYING": "pending_payment", "PAYERROR": "failed"}
        if state not in states:
            raise _invalid()
        result = {"status": states[state], "tradeState": state}
        if state == "SUCCESS":
            result["event"] = self._payment_event(data, "query:" + _string(data.get("transaction_id"), maximum=32) + ":SUCCESS")
        return result

    def _decrypt_event(self, body, headers, *, original_type):
        self._verify_signature(body, headers)
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        envelope = _json(body)
        resource = _object(envelope.get("resource"))
        if (envelope.get("resource_type") != "encrypt-resource"
                or resource.get("algorithm") != "AEAD_AES_256_GCM"
                or resource.get("original_type") != original_type):
            raise _invalid()
        try:
            nonce = _string(resource.get("nonce"), maximum=32).encode()
            aad = _string(resource.get("associated_data", ""), maximum=16, empty=True).encode()
            ciphertext = base64.b64decode(_string(resource.get("ciphertext"), maximum=MAX_BODY), validate=True)
            decrypted = AESGCM(self._config.api_v3_key).decrypt(nonce, ciphertext, aad)
        except (ValueError, TypeError, InvalidTag):
            raise WechatPayError("payment_decryption_invalid", "微信支付回调解密校验未通过，订单状态未变更。") from None
        return envelope, _json(decrypted)

    def verify_event(self, body: bytes, headers: Mapping[str, str]) -> VerifiedPaymentEvent:
        envelope, data = self._decrypt_event(body, headers, original_type="transaction")
        if envelope.get("event_type") != "TRANSACTION.SUCCESS":
            raise _invalid()
        return self._payment_event(data, envelope.get("id"))

    def _refund_request(self, order, request):
        out_trade_no, total = self._order(order)
        if (request.get("orderId") != order["id"] or request.get("kind") != "refund"
                or request.get("ownerId") != order.get("ownerId")):
            raise _invalid()
        out_refund_no = _gateway_id(request.get("id"), "req")
        refund = _money(_object(request.get("data")).get("amountFen"))
        if refund > total:
            raise _invalid()
        return out_trade_no, out_refund_no, total, refund

    def _refund_event(self, data, event_id, *, callback=False):
        # API replies identify the merchant by our signed fixed-host request;
        # encrypted callbacks must explicitly carry our merchant number.
        if (callback or "mchid" in data) and data.get("mchid") != self._config.mch_id:
            raise _invalid()
        if "appid" in data and data["appid"] != self._config.app_id:
            raise _invalid()
        amount = _object(data.get("amount"))
        # Domestic refund callbacks omit currency in the official schema.
        if amount.get("currency", "CNY" if callback else None) != "CNY":
            raise _invalid()
        status = data.get("refund_status" if callback else "status")
        if status not in REFUND_STATES:
            raise _invalid()
        total, refund = _money(amount.get("total")), _money(amount.get("refund"))
        if refund > total:
            raise _invalid()
        return VerifiedRefundEvent(self.name, _string(event_id),
            _local_id(data.get("out_trade_no"), "ord"), _local_id(data.get("out_refund_no"), "req"),
            _string(data.get("refund_id"), maximum=32), _string(data.get("transaction_id"), maximum=32),
            refund, total, status=REFUND_STATES[status])

    def _match_refund(self, data, order, request, source):
        _, _, total, refund = self._refund_request(order, request)
        event = self._refund_event(data, source + ":" + _string(data.get("refund_id"), maximum=32) + ":" + _string(data.get("status")))
        if (event.order_id != order["id"] or event.refund_request_id != request["id"]
                or event.amount_fen != refund or event.total_fen != total
                or event.transaction_id != order.get("transactionId")):
            raise _invalid()
        return event

    def create_refund(self, order: Mapping[str, Any], refund_request: Mapping[str, Any]) -> VerifiedRefundEvent:
        self._require(refund=True)
        out_trade_no, out_refund_no, total, refund = self._refund_request(order, refund_request)
        if (order.get("status") != "paid" or refund_request.get("status") != "approved"
                or not order.get("transactionId")):
            raise WechatPayError("refund_not_approved", "仅已付款且审核通过的退款申请可以提交微信退款。")
        payload = {"out_trade_no": out_trade_no, "out_refund_no": out_refund_no,
                   "notify_url": self._config.refund_notify_url,
                   "amount": {"refund": refund, "total": total, "currency": "CNY"}}
        # Keep customer data / bank account details out of the provider payload.
        payload["reason"] = "客户申请退还充值款"
        data = self._request("POST", "/v3/refund/domestic/refunds", payload)
        event = self._match_refund(data, order, refund_request, "refund-create")
        # Refund submission proves acceptance. Completion is confirmed by a
        # subsequent signed refund query or notification (official API rule).
        return replace(event, status="processing") if event.status == "succeeded" else event

    def query_refund(self, order: Mapping[str, Any], refund_request: Mapping[str, Any]) -> VerifiedRefundEvent:
        self._require()
        _, out_refund_no, _, _ = self._refund_request(order, refund_request)
        data = self._request("GET", f"/v3/refund/domestic/refunds/{out_refund_no}")
        return self._match_refund(data, order, refund_request, "refund-query")

    def verify_refund_event(self, body: bytes, headers: Mapping[str, str]) -> VerifiedRefundEvent:
        envelope, data = self._decrypt_event(body, headers, original_type="refund")
        state = data.get("refund_status")
        if state not in {"SUCCESS", "CLOSED", "ABNORMAL"} or envelope.get("event_type") != "REFUND." + state:
            raise _invalid()
        return self._refund_event(data, envelope.get("id"), callback=True)
