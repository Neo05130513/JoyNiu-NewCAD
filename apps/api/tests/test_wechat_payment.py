"""Real RSA/AES-GCM protocol fixtures. No network or merchant credentials."""
import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
import re

import pytest

pytest.importorskip("cryptography")
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.wechat_payment import (MAX_BODY, WechatPayConfig, WechatPayError,
                                WechatPayNativeAdapter, VerifiedRefundEvent)


NOW = 1_789_027_200
ORDER_ID = "ord_" + "a" * 32
REQUEST_ID = "req_" + "b" * 32


@pytest.fixture(scope="module")
def keys():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048), rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def config(keys):
    private, platform = keys
    return WechatPayConfig(
        mch_id="1900000100", app_id="wx0123456789abcdef", merchant_serial="AB12CD34",
        platform_public_key_id="PUB_KEY_ID_3000000001", notify_url="https://merchant.example/api/payment-notify",
        refund_notify_url="https://merchant.example/api/refund-notify", api_v3_key=b"0123456789abcdef0123456789abcdef",
        private_key_pem=private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        platform_public_key_pem=platform.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


@pytest.fixture
def order():
    return {"id": ORDER_ID, "ownerId": "user1", "package": {"name": "专业积分包", "amountFen": 99999},
            "amountFen": 1200, "creditUnits": 3000, "currency": "CNY", "provider": "wechatpay",
            "status": "paid", "transactionId": "420000000000000000000001",
            "createdAt": datetime.fromtimestamp(NOW, timezone.utc).isoformat()}


@pytest.fixture
def refund_request():
    return {"id": REQUEST_ID, "orderId": ORDER_ID, "ownerId": "user1", "kind": "refund",
            "status": "approved", "data": {"amountFen": 1200, "reason": "Private customer reason"}}


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def sign(body, keys, config, *, stamp=NOW, nonce="fixture-random-nonce"):
    raw = str(stamp).encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    return {"Wechatpay-Timestamp": str(stamp), "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": config.platform_public_key_id,
            "Wechatpay-Signature": base64.b64encode(keys[1].sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()}


def paid(config, order):
    return {"appid": config.app_id, "mchid": config.mch_id, "out_trade_no": order["id"][4:],
            "attach": order["id"], "transaction_id": order["transactionId"], "trade_type": "NATIVE",
            "trade_state": "SUCCESS", "amount": {"total": 1200, "currency": "CNY"}}


def refunded(order, refund_request, *, status="PROCESSING"):
    return {"out_trade_no": order["id"][4:], "out_refund_no": refund_request["id"][4:],
            "transaction_id": order["transactionId"], "refund_id": "503000000000000000000001",
            "status": status, "amount": {"total": 1200, "refund": 1200, "currency": "CNY"}}


def callback(data, keys, config, *, event_type="TRANSACTION.SUCCESS", original_type="transaction", event_id="event-1"):
    nonce, aad = b"nonce1234567", b"transaction" if original_type == "transaction" else b""
    cipher = AESGCM(config.api_v3_key).encrypt(nonce, encode(data), aad)
    body = encode({"id": event_id, "event_type": event_type, "resource_type": "encrypt-resource",
                   "resource": {"algorithm": "AEAD_AES_256_GCM", "original_type": original_type,
                                "nonce": nonce.decode(), "associated_data": aad.decode(),
                                "ciphertext": base64.b64encode(cipher).decode()}})
    return body, sign(body, keys, config)


def adapter(config, keys, response=None, status=200):
    calls = []
    def transport(method, path, headers, body):
        calls.append((method, path, headers, body))
        data = response(method, path, headers, body) if callable(response) else response
        payload = encode(data)
        return status, sign(payload, keys, config), payload
    instance = WechatPayNativeAdapter(config, transport=transport, clock=lambda: NOW)
    instance.calls = calls
    return instance


def test_default_and_missing_configuration_fail_closed(tmp_path):
    default = WechatPayNativeAdapter()
    assert not default.configured and not default.refunds_configured
    # A missing mandatory setting prevents even reading the supplied key path.
    result = WechatPayNativeAdapter.from_env({"JOYNIU_WECHAT_PRIVATE_KEY_FILE": str(tmp_path / "missing-secret")})
    assert not result.configured
    with pytest.raises(WechatPayError, match="尚未配置"):
        result.create_order({})


@pytest.mark.parametrize("field,value", [("api_v3_key", b"short"), ("notify_url", "http://merchant.example/callback"),
    ("notify_url", "https://attacker@merchant.example/callback"), ("notify_url", "https://merchant.example/callback?secret=a"),
    ("merchant_serial", 'ABC"bad'), ("platform_public_key_id", "unknown-cert"), ("private_key_pem", b"invalid"),
    ("refund_notify_url", "http://merchant.example/refund")])
def test_invalid_configuration_never_looks_available(config, field, value):
    result = WechatPayNativeAdapter(replace(config, **{field: value}))
    assert not result.configured
    assert "invalid" not in result.configuration_error and "http" not in result.configuration_error
    assert "0123456789abcdef0123456789abcdef" not in repr(config)


def test_env_key_files_are_local_and_errors_are_redacted(tmp_path, config):
    private, public, key = tmp_path / "private", tmp_path / "platform", tmp_path / "api-key"
    private.write_bytes(config.private_key_pem)
    public.write_bytes(config.platform_public_key_pem)
    key.write_bytes(config.api_v3_key + b"\n")
    env = {"JOYNIU_WECHAT_MCH_ID": config.mch_id, "JOYNIU_WECHAT_APP_ID": config.app_id,
           "JOYNIU_WECHAT_MERCHANT_SERIAL": config.merchant_serial,
           "JOYNIU_WECHAT_PLATFORM_PUBLIC_KEY_ID": config.platform_public_key_id,
           "JOYNIU_WECHAT_NOTIFY_URL": config.notify_url, "JOYNIU_WECHAT_PRIVATE_KEY_FILE": str(private),
           "JOYNIU_WECHAT_PLATFORM_PUBLIC_KEY_FILE": str(public), "JOYNIU_WECHAT_API_V3_KEY_FILE": str(key)}
    result = WechatPayNativeAdapter.from_env(env)
    assert result.configured and not result.refunds_configured
    private.unlink()
    result = WechatPayNativeAdapter.from_env(env)
    assert not result.configured and str(tmp_path) not in result.configuration_error


def test_create_uses_persisted_amount_and_verifiable_request_signature(config, keys, order):
    result = adapter(config, keys, {"code_url": "weixin://wxpay/bizpayurl?pr=fixture"})
    checkout = result.create_order(order)
    assert checkout == {"codeUrl": "weixin://wxpay/bizpayurl?pr=fixture", "expiresAt": datetime.fromtimestamp(NOW + 1800, timezone.utc).isoformat(timespec="seconds")}
    method, path, headers, body = result.calls[0]
    payload = json.loads(body)
    assert payload["amount"] == {"total": 1200, "currency": "CNY"}  # not the package's 99999 or client amount
    assert payload["out_trade_no"] == "a" * 32 and payload["attach"] == ORDER_ID
    assert payload["notify_url"] == config.notify_url and payload["mchid"] == config.mch_id
    assert headers["Wechatpay-Serial"] == config.platform_public_key_id
    auth = dict(re.findall(r'(\w+)="([^"]+)"', headers["Authorization"]))
    signed = f'{method}\n{path}\n{auth["timestamp"]}\n{auth["nonce_str"]}\n'.encode() + body + b"\n"
    keys[0].public_key().verify(base64.b64decode(auth["signature"]), signed, padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(InvalidSignature):
        keys[0].public_key().verify(base64.b64decode(auth["signature"]), signed + b"x", padding.PKCS1v15(), hashes.SHA256())


@pytest.mark.parametrize("code_url", ["https://qr.example/private-payment", "weixin://evil/path", "weixin://wxpay/bizpayurl?pr=x#fragment"])
def test_checkout_rejects_external_or_invalid_qr(config, keys, order, code_url):
    with pytest.raises(WechatPayError):
        adapter(config, keys, {"code_url": code_url}).create_order(order)


@pytest.mark.parametrize("change", [{"amountFen": True}, {"amountFen": 0}, {"amountFen": 12.5},
    {"currency": "USD"}, {"id": "../other"}, {"createdAt": "2020-01-01T00:00:00+00:00"}])
def test_invalid_or_expired_order_never_sends(config, keys, order, change):
    result = adapter(config, keys)
    with pytest.raises(WechatPayError):
        result.create_order({**order, **change})
    assert result.calls == []


def test_verified_payment_decrypts_and_maps_back_without_runtime_lookup(config, keys, order):
    body, headers = callback(paid(config, order), keys, config)
    event = adapter(config, keys).verify_event(body, headers)
    assert (event.provider, event.order_id, event.event_id, event.amount_fen, event.status) == ("wechatpay", ORDER_ID, "event-1", 1200, "paid")
    assert event.transaction_id == order["transactionId"]


@pytest.mark.parametrize("attack", ["body", "signature", "serial", "probe", "old", "future", "nonce", "algorithm", "duplicate-header", "missing-signature"])
def test_signature_tampering_and_replay_window_rejected(config, keys, order, attack):
    body, headers = callback(paid(config, order), keys, config)
    if attack == "body": body += b" "
    if attack == "signature": headers["Wechatpay-Signature"] = "not-base64"
    if attack == "serial": headers["Wechatpay-Serial"] = "PUB_KEY_ID_9999999999"
    if attack == "probe": headers["Wechatpay-Signature"] = "WECHATPAY/SIGNTEST/garbage"
    if attack == "old": headers = sign(body, keys, config, stamp=NOW - 301)
    if attack == "future": headers = sign(body, keys, config, stamp=NOW + 301)
    if attack == "nonce": headers["Wechatpay-Nonce"] += "\n"
    if attack == "algorithm": headers["Wechatpay-Signature-Type"] = "HMAC-SHA256"
    if attack == "duplicate-header": headers["wechatpay-serial"] = headers["Wechatpay-Serial"]
    if attack == "missing-signature": del headers["Wechatpay-Signature"]
    with pytest.raises(WechatPayError, match="签名校验"):
        adapter(config, keys).verify_event(body, headers)


@pytest.mark.parametrize("change", [{"mchid": "different"}, {"appid": "otherapp"}, {"trade_type": "JSAPI"},
    {"trade_state": "NOTPAY"}, {"out_trade_no": "../x"}, {"attach": "ord_" + "c" * 32},
    {"amount": {"total": -1, "currency": "CNY"}}, {"amount": {"total": True, "currency": "CNY"}},
    {"amount": {"total": 1200, "currency": "USD"}}])
def test_correct_signature_cannot_bypass_payment_business_identity(config, keys, order, change):
    body, headers = callback({**paid(config, order), **change}, keys, config)
    with pytest.raises(WechatPayError):
        adapter(config, keys).verify_event(body, headers)


def test_aes_tag_and_duplicate_json_keys_fail_closed(config, keys, order):
    body, _ = callback(paid(config, order), keys, config)
    envelope = json.loads(body)
    envelope["resource"]["ciphertext"] = base64.b64encode(b"broken ciphertext").decode()
    bad = encode(envelope)
    with pytest.raises(WechatPayError, match="解密校验"):
        adapter(config, keys).verify_event(bad, sign(bad, keys, config))
    bad = b'{"id":"one","id":"two"}'
    with pytest.raises(WechatPayError):
        adapter(config, keys).verify_event(bad, sign(bad, keys, config))


def test_query_is_signed_with_exact_query_string_and_validates_snapshot(config, keys, order):
    result = adapter(config, keys, paid(config, order))
    response = result.query_order(order)
    assert response["status"] == "paid" and response["event"].order_id == ORDER_ID
    assert result.calls[0][1] == "/v3/pay/transactions/out-trade-no/" + "a" * 32 + "?mchid=" + config.mch_id
    assert result.calls[0][3] == b""
    with pytest.raises(WechatPayError):
        result.query_order({**order, "amountFen": 1201})


@pytest.mark.parametrize("state,expected", [("NOTPAY", "pending_payment"), ("USERPAYING", "pending_payment"),
    ("CLOSED", "closed"), ("REFUND", "refund"), ("PAYERROR", "failed")])
def test_unpaid_query_never_produces_credit_event(config, keys, order, state, expected):
    response = adapter(config, keys, {**paid(config, order), "trade_state": state}).query_order(order)
    assert response == {"status": expected, "tradeState": state}


def test_pending_query_allows_payment_details_not_yet_populated(config, keys, order):
    response = paid(config, order)
    response["trade_state"] = "NOTPAY"
    del response["amount"], response["trade_type"], response["transaction_id"]
    assert adapter(config, keys, response).query_order(order) == {"status": "pending_payment", "tradeState": "NOTPAY"}


def test_refund_submission_waits_for_independent_query_or_notification(config, keys, order, refund_request):
    event = adapter(config, keys, refunded(order, refund_request, status="SUCCESS")).create_refund(order, refund_request)
    assert event.status == "processing"


def test_signed_not_found_is_distinct_from_unverified_or_transient_errors(config, keys, order, refund_request):
    result = adapter(config, keys, {"code": "RESOURCE_NOT_EXISTS"}, status=404)
    with pytest.raises(WechatPayError) as error:
        result.query_refund(order, refund_request)
    assert error.value.code == "payment_resource_not_exists"


def test_unsigned_gateway_error_and_timeout_never_return_checkout(config, keys, order):
    result = adapter(config, keys)
    result._transport = lambda *args: (502, {}, b"merchant private error")
    with pytest.raises(WechatPayError) as error:
        result.create_order(order)
    assert "private" not in str(error.value)
    def timeout(*args): raise TimeoutError("path/to/secret")
    result._transport = timeout
    with pytest.raises(WechatPayError, match="暂未返回明确结果") as error:
        result.create_order(order)
    assert "secret" not in str(error.value)


def test_signed_http_rejection_is_not_success(config, keys, order):
    with pytest.raises(WechatPayError, match="未受理") as error:
        adapter(config, keys, {"message": "sensitive request body"}, status=400).create_order(order)
    assert "sensitive" not in str(error.value)


def test_refund_submission_is_processing_and_uses_server_snapshot(config, keys, order, refund_request):
    result = adapter(config, keys, refunded(order, refund_request))
    event = result.create_refund(order, refund_request)
    assert isinstance(event, VerifiedRefundEvent) and event.status == "processing"
    method, path, _, body = result.calls[0]
    assert (method, path) == ("POST", "/v3/refund/domestic/refunds")
    payload = json.loads(body)
    assert payload["amount"] == {"refund": 1200, "total": 1200, "currency": "CNY"}
    assert payload["out_refund_no"] == "b" * 32 and payload["out_trade_no"] == "a" * 32
    assert "Private customer" not in body.decode() and "bank" not in body.decode()


@pytest.mark.parametrize("change", [{"status": "requested"}, {"orderId": "ord_" + "c" * 32},
    {"ownerId": "other"}, {"data": {"amountFen": 1201}}])
def test_refund_invalid_or_unapproved_never_sends(config, keys, order, refund_request, change):
    result = adapter(config, keys)
    with pytest.raises(WechatPayError):
        result.create_refund(order, {**refund_request, **change})
    assert not result.calls


@pytest.mark.parametrize("state,expected", [("SUCCESS", "succeeded"), ("PROCESSING", "processing"), ("CLOSED", "closed"), ("ABNORMAL", "abnormal")])
def test_refund_query_checks_real_state_and_original_transaction(config, keys, order, refund_request, state, expected):
    result = adapter(config, keys, refunded(order, refund_request, status=state))
    event = result.query_refund(order, refund_request)
    assert event.status == expected and event.amount_fen == 1200
    assert result.calls[0][1] == "/v3/refund/domestic/refunds/" + "b" * 32
    with pytest.raises(WechatPayError):
        result.query_refund({**order, "transactionId": "different"}, refund_request)


@pytest.mark.parametrize("state,expected", [("SUCCESS", "succeeded"), ("CLOSED", "closed"), ("ABNORMAL", "abnormal")])
def test_refund_callback_accepts_official_currency_omission(config, keys, order, refund_request, state, expected):
    data = refunded(order, refund_request, status=state)
    data["mchid"], data["refund_status"] = config.mch_id, data.pop("status")
    del data["amount"]["currency"]
    body, headers = callback(data, keys, config, original_type="refund", event_type="REFUND." + state)
    event = adapter(config, keys).verify_refund_event(body, headers)
    assert event.status == expected and event.currency == "CNY" and event.refund_request_id == REQUEST_ID


def test_refund_callback_merchant_state_and_currency_must_match(config, keys, order, refund_request):
    original = refunded(order, refund_request, status="SUCCESS")
    original.update(mchid=config.mch_id, refund_status="SUCCESS")
    for change in [{"mchid": "other"}, {"refund_status": "PROCESSING"}, {"amount": {"total": 1200, "refund": 1200, "currency": "USD"}}]:
        body, headers = callback({**original, **change}, keys, config, original_type="refund", event_type="REFUND.SUCCESS")
        with pytest.raises(WechatPayError):
            adapter(config, keys).verify_refund_event(body, headers)


def test_transport_uses_fixed_tls_host_timeout_and_never_redirects(monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, **kwargs): calls.append((host, kwargs))
        def request(self, *args, **kwargs): calls.append((args, kwargs))
        def getresponse(self): return self
        status = 302
        def read(self, size): return b"redirect body"
        def getheaders(self): return [("Location", "https://attacker.example")]
        def close(self): calls.append("closed")
    monkeypatch.setattr("app.wechat_payment.http.client.HTTPSConnection", Connection)
    status, headers, _ = WechatPayNativeAdapter._https_request("GET", "/v3/example", {}, b"")
    assert status == 302 and headers["Location"].startswith("https://attacker")
    assert calls[0][0] == "api.mch.weixin.qq.com" and calls[0][1]["timeout"] == 10
    assert calls[0][1]["context"].check_hostname and calls[-1] == "closed"
    assert len(calls) == 3


def test_oversized_body_fails_signature_validation(config, keys):
    with pytest.raises(WechatPayError):
        adapter(config, keys).verify_event(b"x" * (MAX_BODY + 1), {})
