"""Durable credit accounts and payment records; charging is opt-in.

No estimates, reservations or automatic pricing live here. Supplier usage is
an immutable cost record, separate from customer credit adjustments. A real
payment adapter must verify provider messages before delivering an event.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .platform import AuthorizationError, ConflictError, NotFoundError, ValidationError


MAX_INTEGER = 10**12


def admin_record_filters(*, q='', status='', filter_owner_id='', date_from='', date_to='', columns=(), status_column=None, owner_column='owner_id', date_column='created_at'):
    """Bound admin searches to parameterized columns and Shanghai calendar dates."""
    filters, values = [], []
    for value, maximum in [(q, 128), (status, 64), (filter_owner_id, 160)]:
        if not isinstance(value, str) or len(value) > maximum:
            raise ValidationError('查询条件格式不正确')
    if filter_owner_id:
        filters.append(f'{owner_column}=?'); values.append(filter_owner_id)
    if status:
        if not status_column:
            raise ValidationError('此类记录不支持状态筛选')
        filters.append(f'{status_column}=?'); values.append(status)
    if q.strip():
        escaped = q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        filters.append('('+' OR '.join(f"{column} LIKE ? ESCAPE '\\'" for column in columns)+')')
        values.extend(['%'+escaped+'%']*len(columns))
    bounds=[]
    for raw, end in [(date_from, False), (date_to, True)]:
        if not isinstance(raw, str):
            raise ValidationError('日期格式须为 YYYY-MM-DD')
        if not raw:
            bounds.append(None); continue
        try:
            if len(raw) != 10:
                raise ValueError()
            parsed = datetime.strptime(raw, '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8)))
            if parsed.strftime('%Y-%m-%d') != raw:
                raise ValueError()
            bound = (parsed + timedelta(days=1) if end else parsed).astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise ValidationError('日期格式须为 YYYY-MM-DD') from None
        bounds.append(parsed)
        filters.append(f"julianday({date_column}) {'<' if end else '>='} julianday(?)")
        values.append(bound)
    if all(bounds) and bounds[0] > bounds[1]:
        raise ValidationError('开始日期不能晚于结束日期')
    return filters, values


class BillingUnavailable(ConflictError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(prefix):
    return f"{prefix}_{uuid4().hex}"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, name, *, maximum=1000, optional=False):
    if not isinstance(value, str) or (not value.strip() and not optional) or len(value) > maximum:
        raise ValidationError(f"{name} 格式不正确")
    return value.strip()


def _integer(value, name, *, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name} 必须是范围内的整数")
    return value


@dataclass(frozen=True)
class VerifiedPaymentEvent:
    """Only a configured adapter's verified callback/query may create this."""
    provider: str
    event_id: str
    order_id: str
    transaction_id: str
    amount_fen: int
    currency: str = "CNY"
    status: str = "paid"


@dataclass(frozen=True)
class VerifiedRefundEvent:
    provider: str
    event_id: str
    order_id: str
    refund_request_id: str
    refund_id: str
    transaction_id: str
    amount_fen: int
    total_fen: int
    currency: str = "CNY"
    status: str = "processing"


class PaymentAdapter(Protocol):
    name: str
    configured: bool

    def create_order(self, order: Mapping[str, Any]) -> Mapping[str, Any]:
        """Use order.id as the gateway idempotency key; return public checkout."""

    def verify_event(self, body: bytes, headers: Mapping[str, str]) -> VerifiedPaymentEvent:
        """Verify signature, merchant identity, timestamp and decrypted payload."""


class BillingService:
    supported_insufficient_balance_policies = frozenset({"deferred_due", "cap_at_balance"})
    def __init__(self, database=":memory:", *, auth, payment_adapter=None,
                 enabled=False, charging_configured=False, refunds_enabled=False,
                 charging_status_provider=None, terms_provider=None, online_payments_enabled=False):
        self.database = str(database)
        self.auth = auth
        self.payment_adapter = payment_adapter
        self.terms_provider = terms_provider
        # Server composition decides these, never an unauthenticated client.
        self.enabled = enabled is True
        self.charging_configured = charging_configured is True
        self.refunds_enabled = refunds_enabled is True
        self.online_payments_enabled = online_payments_enabled is True
        self.charging_status_provider = charging_status_provider
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.database, timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=15000")
        if self.database != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._create_schema()

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _create_schema(self):
        with self._lock, self._db:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS billing_accounts (
              owner_id TEXT PRIMARY KEY, credit_units INTEGER NOT NULL DEFAULT 0
              CHECK(typeof(credit_units)='integer' AND credit_units>=0), updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_packages (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, amount_fen INTEGER NOT NULL,
              credit_units INTEGER NOT NULL, active INTEGER NOT NULL, version INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_orders (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              request_hash TEXT NOT NULL, package_json TEXT NOT NULL, amount_fen INTEGER NOT NULL,
              credit_units INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
              provider TEXT NOT NULL, transaction_id TEXT, checkout_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(owner_id,idempotency_key), UNIQUE(provider,transaction_id));
            CREATE TABLE IF NOT EXISTS billing_ledger (
              id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
              owner_id TEXT NOT NULL, delta_units INTEGER NOT NULL, balance_after INTEGER NOT NULL,
              kind TEXT NOT NULL, reference_id TEXT NOT NULL, reason TEXT NOT NULL,
              actor_id TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS billing_ledger_owner ON billing_ledger(owner_id,created_at);
            CREATE TABLE IF NOT EXISTS billing_payment_events (
              provider TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
              order_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(provider,event_id));
            CREATE TABLE IF NOT EXISTS billing_requests (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, order_id TEXT NOT NULL,
              kind TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
              data_json TEXT NOT NULL, status TEXT NOT NULL, admin_note TEXT NOT NULL DEFAULT '',
              external_reference TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(owner_id,kind,idempotency_key));
            CREATE TABLE IF NOT EXISTS billing_refunds (
              request_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
              provider TEXT NOT NULL, refund_id TEXT, status TEXT NOT NULL,
              amount_fen INTEGER NOT NULL, credit_units INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(provider,refund_id));
            CREATE TABLE IF NOT EXISTS billing_refund_events (
              provider TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
              request_id TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(provider,event_id));
            CREATE TABLE IF NOT EXISTS billing_usage (
              call_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, job_id TEXT NOT NULL,
              data_json TEXT NOT NULL, payload_hash TEXT NOT NULL, usage_known INTEGER NOT NULL,
              cost_micro_usd INTEGER, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS billing_usage_job ON billing_usage(job_id);
            CREATE TABLE IF NOT EXISTS billing_usage_settlements (
              event_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
              owner_id TEXT NOT NULL, job_id TEXT NOT NULL, result_json TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_dues (
              id TEXT PRIMARY KEY, settlement_key TEXT NOT NULL UNIQUE,
              owner_id TEXT NOT NULL, credit_units INTEGER NOT NULL CHECK(credit_units>0),
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_due_payments (
              id TEXT PRIMARY KEY, due_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              credit_units INTEGER NOT NULL CHECK(credit_units>0),
              ledger_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS billing_dues_owner ON billing_dues(owner_id,created_at);
            CREATE INDEX IF NOT EXISTS billing_due_payments_due ON billing_due_payments(due_id);
            CREATE TABLE IF NOT EXISTS billing_audit (
              id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
              resource_id TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_offline_records (
              id TEXT PRIMARY KEY, order_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('recharge','refund')),
              amount_fen INTEGER NOT NULL CHECK(typeof(amount_fen)='integer' AND amount_fen>0),
              credit_units INTEGER NOT NULL CHECK(credit_units=amount_fen),
              external_reference TEXT NOT NULL, reference_key TEXT NOT NULL UNIQUE,
              reason TEXT NOT NULL, actor_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
              original_record_id TEXT UNIQUE, ledger_id TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL, UNIQUE(order_id,kind));
            """)
            order_columns = {row[1] for row in self._db.execute("PRAGMA table_info(billing_orders)")}
            if "terms_json" not in order_columns:
                self._db.execute("ALTER TABLE billing_orders ADD COLUMN terms_json TEXT NOT NULL DEFAULT '{}'")
            if "offline_json" not in order_columns:
                self._db.execute("ALTER TABLE billing_orders ADD COLUMN offline_json TEXT NOT NULL DEFAULT '{}'")
            self._db.execute("CREATE TRIGGER IF NOT EXISTS billing_order_offline_immutable BEFORE UPDATE ON billing_orders "
                             "WHEN NEW.offline_json!=OLD.offline_json BEGIN SELECT RAISE(ABORT, 'immutable offline receipt'); END")
            self._db.execute("CREATE TRIGGER IF NOT EXISTS billing_order_offline_identity_immutable BEFORE UPDATE ON billing_orders "
                             "WHEN OLD.provider='offline' AND (NEW.transaction_id IS NOT OLD.transaction_id OR NEW.created_at!=OLD.created_at) "
                             "BEGIN SELECT RAISE(ABORT, 'immutable offline receipt identity'); END")
            self._db.execute("CREATE TRIGGER IF NOT EXISTS billing_order_terms_immutable BEFORE UPDATE ON billing_orders "
                             "WHEN NEW.terms_json!=OLD.terms_json BEGIN SELECT RAISE(ABORT, 'immutable order terms'); END")
            for table in ("billing_ledger", "billing_usage", "billing_audit", "billing_payment_events", "billing_refund_events",
                          "billing_usage_settlements", "billing_dues", "billing_due_payments", "billing_offline_records"):
                for operation in ("UPDATE", "DELETE"):
                    self._db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                                     f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable billing record'); END")
            self._db.executescript("""
            CREATE TRIGGER IF NOT EXISTS billing_order_snapshot_immutable BEFORE UPDATE ON billing_orders
              WHEN NEW.id!=OLD.id OR NEW.owner_id!=OLD.owner_id OR NEW.package_json!=OLD.package_json
              OR NEW.amount_fen!=OLD.amount_fen OR NEW.credit_units!=OLD.credit_units OR NEW.currency!=OLD.currency
              OR NEW.provider!=OLD.provider OR NEW.idempotency_key!=OLD.idempotency_key OR NEW.request_hash!=OLD.request_hash
              BEGIN SELECT RAISE(ABORT, 'immutable order snapshot'); END;
            CREATE TRIGGER IF NOT EXISTS billing_order_no_delete BEFORE DELETE ON billing_orders
              BEGIN SELECT RAISE(ABORT, 'immutable order history'); END;
            CREATE TRIGGER IF NOT EXISTS billing_refund_snapshot_immutable BEFORE UPDATE ON billing_refunds
              WHEN NEW.request_id!=OLD.request_id OR NEW.order_id!=OLD.order_id
              OR NEW.provider!=OLD.provider OR NEW.amount_fen!=OLD.amount_fen OR NEW.credit_units!=OLD.credit_units
              BEGIN SELECT RAISE(ABORT, 'immutable refund snapshot'); END;
            CREATE TRIGGER IF NOT EXISTS billing_refund_no_delete BEFORE DELETE ON billing_refunds
              BEGIN SELECT RAISE(ABORT, 'immutable refund history'); END;
            CREATE TRIGGER IF NOT EXISTS billing_request_snapshot_immutable BEFORE UPDATE ON billing_requests
              WHEN NEW.id!=OLD.id OR NEW.order_id!=OLD.order_id OR NEW.owner_id!=OLD.owner_id
              OR NEW.kind!=OLD.kind OR NEW.data_json!=OLD.data_json OR NEW.idempotency_key!=OLD.idempotency_key
              OR NEW.request_hash!=OLD.request_hash
              BEGIN SELECT RAISE(ABORT, 'immutable request snapshot'); END;
            """)

    def _owner(self, owner_id):
        user = self.auth.get_user(_text(owner_id, "用户", maximum=128))
        if not user.active:
            raise AuthorizationError("账号已停用")
        return user

    def _admin(self, actor_id, permission="billing:policy"):
        user = self._owner(actor_id)
        if "*" not in user.permissions and permission not in user.permissions:
            raise AuthorizationError("当前账号没有此财务操作权限")
        return user

    def _audit(self, db, actor_id, action, resource_id, details):
        db.execute("INSERT INTO billing_audit VALUES (?,?,?,?,?,?)",
                   (_id("ba"), actor_id, action, resource_id, _json(details), _now()))

    def status(self):
        configured = bool(self.payment_adapter is not None and self.payment_adapter.configured is True)
        selected_enabled, charging_configured = self.enabled, self.charging_configured
        policy_available = True
        if callable(self.charging_status_provider):
            try:
                policy = self.charging_status_provider()
                policy_available = isinstance(policy, Mapping) and type(policy.get("enabled")) is bool and type(policy.get("configured")) is bool
                selected_enabled = isinstance(policy, Mapping) and policy.get("enabled") is True
                charging_configured = isinstance(policy, Mapping) and policy.get("configured") is True
            except Exception:
                # A missing/corrupt policy cannot silently keep a stale worker
                # selling or deducting credits. Existing callbacks still work.
                selected_enabled = charging_configured = False
                policy_available = False
        enabled = selected_enabled and charging_configured
        try:
            terms_ready = self.terms_provider is None or self.terms_provider.ready() is True
        except Exception:
            terms_ready = False
        return {"enabled": enabled, "chargingConfigured": charging_configured,
                "commercialMode": selected_enabled, "policyAvailable": policy_available,
                "paymentConfigured": configured, "onlinePaymentEnabled": self.online_payments_enabled,
                "offlineRechargeEnabled": True, "pointsPerCny": 100,
                "purchaseEnabled": self.online_payments_enabled and enabled and configured and terms_ready,
                "termsRequired": self.terms_provider is not None, "termsReady": terms_ready,
                "paymentQueryAvailable": configured and callable(getattr(self.payment_adapter, "query_order", None)),
                "refundExecutionEnabled": self.refunds_enabled and configured and getattr(self.payment_adapter, "refunds_configured", False) is True,
                "refundPolicy": "full_order_only",
                "currency": "CNY", "creditUnit": "积分", "creditUnitsPerPoint": 1,
                "paymentProvider": self.payment_adapter.name if configured else None,
                "message": "积分计费规则尚未启用，现有建模任务不会扣积分。" if not enabled else
                           "当前通过线下办理充值，请联系运营核对到账；10 元兑换 1000 积分。" if not self.online_payments_enabled else
                           "支付渠道尚未开通，暂不可购买积分。" if not configured else
                           "购买条款尚未发布，暂不可购买积分。" if not terms_ready else "积分服务已启用。"}

    def require_terms(self, owner_id):
        if self.terms_provider is None:
            return {}
        if self.terms_provider.ready() is not True:
            raise BillingUnavailable("购买与计费条款尚未发布，请稍后再试。")
        value = self.terms_provider.require_acceptance(owner_id)
        if not isinstance(value, Mapping) or not isinstance(value.get("versions"), Mapping):
            raise ValidationError("条款接受凭据不完整")
        return {"acceptanceId": _text(value.get("acceptanceId"), "条款接受编号", maximum=128),
                "versions": {key: _text(value["versions"].get(key), "条款版本", maximum=128) for key in ("service", "privacy", "credits")},
                "acceptedAt": _text(value.get("acceptedAt"), "条款接受时间", maximum=100)}

    @staticmethod
    def _package(row):
        return {"id": row["id"], "name": row["name"], "amountFen": row["amount_fen"],
                "creditUnits": row["credit_units"], "active": bool(row["active"]),
                "version": row["version"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def packages(self, *, actor_id=None, include_inactive=False):
        if include_inactive:
            self._admin(actor_id, "billing:read")
        with self._lock:
            rows = self._db.execute("SELECT * FROM billing_packages " + ("" if include_inactive else "WHERE active=1 ") + "ORDER BY amount_fen,id").fetchall()
        return {"items": [self._package(row) for row in rows]}

    def save_package(self, actor_id, values, *, package_id=None):
        self._admin(actor_id)
        if not isinstance(values, Mapping) or set(values) - {"name", "amountFen", "creditUnits", "active", "version"}:
            raise ValidationError("套餐字段不正确")
        with self._transaction() as db:
            old = db.execute("SELECT * FROM billing_packages WHERE id=?", (package_id,)).fetchone() if package_id else None
            if package_id and old is None:
                raise NotFoundError("套餐不存在")
            if old and (type(values.get("version")) is not int or values.get("version") != old["version"]):
                raise ConflictError("套餐已更新，请刷新后重试")
            merged = {**(self._package(old) if old else {}), **values}
            name = _text(merged.get("name"), "套餐名称", maximum=100)
            amount = _integer(merged.get("amountFen"), "金额（分）", minimum=1, maximum=100_000_000)
            units = _integer(merged.get("creditUnits"), "积分", minimum=1)
            active = merged.get("active", False)
            if type(active) is not bool:
                raise ValidationError("active 必须是布尔值")
            package_id, now = package_id or _id("pkg"), _now()
            if old:
                db.execute("UPDATE billing_packages SET name=?,amount_fen=?,credit_units=?,active=?,version=version+1,updated_at=? WHERE id=?",
                           (name, amount, units, int(active), now, package_id))
            else:
                db.execute("INSERT INTO billing_packages VALUES (?,?,?,?,?,?,?,?)", (package_id, name, amount, units, int(active), 1, now, now))
            result = self._package(db.execute("SELECT * FROM billing_packages WHERE id=?", (package_id,)).fetchone())
            self._audit(db, actor_id, "package.updated" if old else "package.created", package_id, {"before": self._package(old) if old else None, "after": result})
        return result

    def wallet(self, owner_id):
        self._owner(owner_id)
        with self._lock:
            row = self._db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (owner_id,)).fetchone()
            due = self._due_units(self._db, owner_id)
        return {"ownerId": owner_id, "creditUnits": row[0] if row else 0, "dueUnits": due,
                "canStartChargedWork": bool(row and row[0] > 0 and due == 0), "unit": "积分", **self.status()}

    @staticmethod
    def _due_units(db, owner_id):
        return db.execute("SELECT coalesce(sum(d.credit_units-(SELECT coalesce(sum(p.credit_units),0) FROM billing_due_payments p WHERE p.due_id=d.id)),0) FROM billing_dues d WHERE d.owner_id=?", (owner_id,)).fetchone()[0]

    def can_start_charged_work(self, owner_id):
        self._owner(owner_id)
        with self._lock:
            return self._due_units(self._db, owner_id) == 0

    def _apply_due_payments(self, db, owner_id, credit_entry_id):
        """Every newly posted positive credit first pays existing due, FIFO.

        Called in the same transaction as verified recharge or audited credit.
        Replayed positive ledger events return before here, so neither a second
        callback nor a later debt can reuse an old recharge event.
        """
        dues = db.execute("SELECT d.*,d.credit_units-(SELECT coalesce(sum(p.credit_units),0) FROM billing_due_payments p WHERE p.due_id=d.id) AS remaining FROM billing_dues d WHERE owner_id=? ORDER BY created_at,id", (owner_id,)).fetchall()
        for due in dues:
            balance = db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (owner_id,)).fetchone()[0]
            units = min(balance, due["remaining"])
            if units <= 0:
                continue
            entry = self._post(db, owner_id=owner_id, delta=-units, kind="due_payment", reference=due["id"],
                               reason="新增积分自动补缴已完成任务的待付积分", actor_id="billing-worker",
                               event_key=f"due-payment:{due['id']}:{credit_entry_id}")
            db.execute("INSERT INTO billing_due_payments VALUES (?,?,?,?,?,?)", (_id("dp"), due["id"], owner_id, units, entry["id"], _now()))
            self._audit(db, "billing-worker", "usage.due_paid", due["id"], {"creditUnits": units, "creditEntryId": credit_entry_id, "entryId": entry["id"]})

    @staticmethod
    def _ledger_entry(row):
        return {"id": row["id"], "ownerId": row["owner_id"], "deltaUnits": row["delta_units"],
                "balanceAfter": row["balance_after"], "kind": row["kind"], "referenceId": row["reference_id"],
                "reason": row["reason"], "createdAt": row["created_at"]}

    def _post(self, db, *, owner_id, delta, kind, reference, reason, actor_id, event_key):
        data = {"ownerId": owner_id, "delta": delta, "kind": kind, "reference": reference, "reason": reason, "actorId": actor_id}
        digest = _digest(data)
        prior = db.execute("SELECT * FROM billing_ledger WHERE event_key=?", (event_key,)).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                raise ConflictError("同一幂等键不能用于不同的积分变更")
            return self._ledger_entry(prior)
        now = _now()
        db.execute("INSERT OR IGNORE INTO billing_accounts VALUES (?,0,?)", (owner_id, now))
        balance = db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (owner_id,)).fetchone()[0] + delta
        if balance < 0:
            raise ConflictError("余额不足，未扣除积分；此情况需要核账，不会改变建模任务状态")
        if balance > MAX_INTEGER:
            raise ValidationError("积分余额超过存储上限")
        entry_id = _id("cr")
        db.execute("UPDATE billing_accounts SET credit_units=?,updated_at=? WHERE owner_id=?", (balance, now, owner_id))
        db.execute("INSERT INTO billing_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (entry_id, event_key, digest, owner_id, delta, balance, kind, reference, reason, actor_id, now))
        if delta > 0:
            self._apply_due_payments(db, owner_id, entry_id)
        return self._ledger_entry(db.execute("SELECT * FROM billing_ledger WHERE id=?", (entry_id,)).fetchone())

    def adjust(self, actor_id, *, owner_id, credit_units, reason, idempotency_key, category="adjustment"):
        self._admin(actor_id, "billing:adjust")
        self._owner(owner_id)
        delta = _integer(credit_units, "补偿积分", minimum=-MAX_INTEGER)
        if not delta:
            raise ValidationError("积分调整不能为零")
        if not isinstance(category, str) or category not in {"adjustment", "gift", "compensation"}:
            raise ValidationError("积分调整类型不正确")
        reason = _text(reason, "调整原因")
        key = _text(idempotency_key, "幂等键", maximum=128)
        with self._transaction() as db:
            entry = self._post(db, owner_id=owner_id, delta=delta, kind=category, reference="",
                               reason=reason, actor_id=actor_id, event_key=f"adjust:{actor_id}:{key}")
            if not db.execute("SELECT 1 FROM billing_audit WHERE resource_id=? AND action='credit.adjusted'", (entry["id"],)).fetchone():
                self._audit(db, actor_id, "credit.adjusted", entry["id"], entry)
            return entry

    @staticmethod
    def _order(row):
        return {"id": row["id"], "ownerId": row["owner_id"], "package": json.loads(row["package_json"]),
                "amountFen": row["amount_fen"], "creditUnits": row["credit_units"], "currency": row["currency"],
                "status": row["status"], "provider": row["provider"], "checkout": json.loads(row["checkout_json"]),
                "transactionId": row["transaction_id"], "termsAcceptance": json.loads(row["terms_json"]),
                "offlineReceipt": json.loads(row["offline_json"]),
                "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def _offline_replay(self, db, key, digest):
        prior = db.execute("SELECT * FROM billing_offline_records WHERE idempotency_key=?", (key,)).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                raise ConflictError("同一幂等键不能登记不同的线下收退款")
            return self._order(db.execute("SELECT * FROM billing_orders WHERE id=?", (prior["order_id"],)).fetchone())
        return None

    def _offline_reference(self, db, reference):
        # Receipt references are global across all operators and both directions.
        # Keep the original display value, normalize compatibility characters/case.
        import unicodedata
        normalized = unicodedata.normalize("NFKC", reference).casefold()
        if db.execute("SELECT 1 FROM billing_offline_records WHERE reference_key=?", (normalized,)).fetchone():
            raise ConflictError("此线下收退款凭证已登记，请查询原记录，不能重复入账")
        return normalized

    def offline_recharge(self, actor_id, *, owner_id, amount_fen, external_reference, reason,
                         receipt_confirmed, idempotency_key):
        """Record actual offline receipt and credit atomically; never initiate payment."""
        self._admin(actor_id, "billing:manage")
        owner = self._owner(owner_id)
        amount = _integer(amount_fen, "实收金额（分）", minimum=1, maximum=100_000_000)
        reference = _text(external_reference, "实际到账凭证", maximum=200)
        reason = _text(reason, "收款说明")
        key = _text(idempotency_key, "幂等键", maximum=128)
        if receipt_confirmed is not True:
            raise ValidationError("请先核实实际到账，再确认登记线下充值")
        data = {"kind": "recharge", "ownerId": owner_id, "amountFen": amount,
                "externalReference": reference, "reason": reason}
        digest = _digest(data)
        with self._transaction() as db:
            prior = self._offline_replay(db, key, digest)
            if prior:
                return prior
            reference_key = self._offline_reference(db, reference)
            # Receipt entry is the operator's statement of money received,
            # never a customer acceptance or permission to start charged work.
            # The customer personally accepts terms before check_start allows
            # charged modeling, including when funded before their first login.
            terms = {}
            now, order_id, record_id = _now(), _id("ord"), _id("offline")
            receipt = {"id": record_id, "externalReference": reference, "reason": reason,
                       "confirmedBy": actor_id, "confirmedAt": now, "customerName": owner.display_name,
                       "pointsPerCny": 100, "amountFen": amount, "creditUnits": amount}
            package = {"id": "offline-fixed-rate", "name": "线下充值", "amountFen": amount,
                       "creditUnits": amount, "pointsPerCny": 100}
            db.execute("INSERT INTO billing_orders (id,owner_id,idempotency_key,request_hash,package_json,amount_fen,credit_units,currency,status,provider,transaction_id,created_at,updated_at,terms_json,offline_json) VALUES (?,?,?,?,?,?,?,'CNY','paid','offline',?,?,?,?,?)",
                       (order_id, owner_id, f"offline:{record_id}", digest, _json(package), amount, amount,
                        reference, now, now, _json(terms), _json(receipt)))
            entry = self._post(db, owner_id=owner_id, delta=amount, kind="offline_recharge", reference=order_id,
                               reason=reason, actor_id=actor_id, event_key=f"offline:{record_id}")
            db.execute("INSERT INTO billing_offline_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (record_id, order_id, owner_id, "recharge", amount, amount, reference, reference_key,
                        reason, actor_id, key, digest, None, entry["id"], now))
            self._audit(db, actor_id, "offline.recharge_recorded", order_id, {**data, "recordId": record_id,
                        "creditUnits": amount, "ledgerId": entry["id"], "receiptConfirmed": True})
            return self._order(db.execute("SELECT * FROM billing_orders WHERE id=?", (order_id,)).fetchone())

    def offline_refund(self, actor_id, order_id, *, external_reference, reason, refund_confirmed,
                       idempotency_key, refund_request_id=None):
        """Record an actual full offline refund with immutable original linkage.

        This does not move money. Existing approved requests may be completed;
        direct operator refunds create the same customer-visible request trail.
        """
        self._admin(actor_id, "billing:manage")
        reference = _text(external_reference, "实际退款凭证", maximum=200)
        reason = _text(reason, "退款说明")
        key = _text(idempotency_key, "幂等键", maximum=128)
        if refund_request_id is not None:
            refund_request_id = _text(refund_request_id, "退款申请", maximum=128)
        if refund_confirmed is not True:
            raise ValidationError("请核实线下实际退款后确认登记；本操作不会自动转账")
        data = {"kind": "refund", "orderId": order_id, "externalReference": reference,
                "reason": reason, "refundRequestId": refund_request_id}
        digest = _digest(data)
        with self._transaction() as db:
            prior = self._offline_replay(db, key, digest)
            if prior:
                return prior
            order = db.execute("SELECT * FROM billing_orders WHERE id=? AND provider='offline'", (order_id,)).fetchone()
            if not order:
                raise NotFoundError("线下充值订单不存在")
            if order["status"] != "paid" or db.execute("SELECT 1 FROM billing_refunds WHERE order_id=?", (order_id,)).fetchone():
                raise ConflictError("此订单已退款或已有退款记录，不能重复冲销")
            original = db.execute("SELECT * FROM billing_offline_records WHERE order_id=? AND kind='recharge'", (order_id,)).fetchone()
            if not original:
                raise ConflictError("订单缺少原始线下收款记录，请先核账")
            reference_key = self._offline_reference(db, reference)
            request = db.execute("SELECT * FROM billing_requests WHERE id=? AND order_id=? AND kind='refund'", (refund_request_id, order_id)).fetchone() if refund_request_id else None
            if refund_request_id and not request:
                raise NotFoundError("退款申请与原充值单不匹配")
            if request and (request["status"] != "approved" or json.loads(request["data_json"])["amountFen"] != order["amount_fen"]):
                raise ConflictError("仅审核通过的整单退款申请可以登记线下退款")
            if not request and db.execute("SELECT 1 FROM billing_requests WHERE order_id=? AND kind='refund' AND status!='rejected'", (order_id,)).fetchone():
                raise ConflictError("此订单已有退款申请，请先审核并从原申请登记退款")
            now, record_id = _now(), _id("offline")
            request_id = refund_request_id or _id("req")
            entry = self._post(db, owner_id=order["owner_id"], delta=-order["credit_units"], kind="offline_refund",
                               reference=order_id, reason=reason, actor_id=actor_id, event_key=f"offline:{record_id}")
            if not request:
                request_data = {"reason": reason, "amountFen": order["amount_fen"], "provider": "offline"}
                db.execute("INSERT INTO billing_requests (id,owner_id,order_id,kind,idempotency_key,request_hash,data_json,status,admin_note,external_reference,created_at,updated_at) VALUES (?,?,?,'refund',?,?,?,'refunded',?,?,?,?)",
                           (request_id, order["owner_id"], order_id, f"offline:{record_id}", digest, _json(request_data), reason, reference, now, now))
            else:
                db.execute("UPDATE billing_requests SET status='refunded',admin_note=?,external_reference=?,updated_at=? WHERE id=?",
                           (reason, reference, now, request_id))
            db.execute("INSERT INTO billing_refunds VALUES (?,?,?,?,'succeeded',?,?,?,?)",
                       (request_id, order_id, "offline", reference, order["amount_fen"], order["credit_units"], now, now))
            db.execute("INSERT INTO billing_offline_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (record_id, order_id, order["owner_id"], "refund", order["amount_fen"], order["credit_units"],
                        reference, reference_key, reason, actor_id, key, digest, original["id"], entry["id"], now))
            db.execute("UPDATE billing_orders SET status='refunded',updated_at=? WHERE id=?", (now, order_id))
            self._audit(db, actor_id, "offline.refund_recorded", order_id,
                        {**data, "recordId": record_id, "originalRecordId": original["id"], "ledgerId": entry["id"],
                         "requestId": request_id, "amountFen": order["amount_fen"], "creditUnits": order["credit_units"], "refundConfirmed": True})
            return self._order(db.execute("SELECT * FROM billing_orders WHERE id=?", (order_id,)).fetchone())

    def order(self, owner_id, order_id):
        self._owner(owner_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM billing_orders WHERE id=? AND owner_id=?", (order_id, owner_id)).fetchone()
        if row is None:
            raise NotFoundError("订单不存在")
        return self._order(row)

    def create_order(self, owner_id, *, package_id, idempotency_key):
        self._owner(owner_id)
        key = _text(idempotency_key, "幂等键", maximum=128)
        package_id = _text(package_id, "套餐", maximum=128)
        digest = _digest({"packageId": package_id})
        # Replays remain readable after the payment channel or package closes.
        with self._transaction() as db:
            old = db.execute("SELECT * FROM billing_orders WHERE owner_id=? AND idempotency_key=?", (owner_id, key)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise ConflictError("同一幂等键不能创建不同订单")
                return self._order(old)
            if not self.status()["purchaseEnabled"]:
                raise BillingUnavailable("支付或积分规则尚未开通，暂不可购买积分。")
            terms_acceptance = self.require_terms(owner_id)
            package = db.execute("SELECT * FROM billing_packages WHERE id=? AND active=1", (package_id,)).fetchone()
            if not package:
                raise NotFoundError("套餐不存在或已下架")
            now, order_id = _now(), _id("ord")
            db.execute("INSERT INTO billing_orders (id,owner_id,idempotency_key,request_hash,package_json,amount_fen,credit_units,currency,status,provider,created_at,updated_at,terms_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (order_id, owner_id, key, digest, _json(self._package(package)), package["amount_fen"], package["credit_units"], "CNY", "creating", self.payment_adapter.name, now, now, _json(terms_acceptance)))
            self._audit(db, owner_id, "order.created", order_id, {"packageId": package_id, "amountFen": package["amount_fen"], "termsAcceptance": terms_acceptance})
        # This call has a durable merchant order id before contacting a gateway.
        # Replays never create another gateway order; uncertain orders are queried
        # by the future adapter's reconciliation worker using this same id.
        try:
            checkout = self.payment_adapter.create_order(self.order(owner_id, order_id))
            if not isinstance(checkout, Mapping):
                raise ValueError("invalid payment checkout")
            safe = {key: checkout[key] for key in ("codeUrl", "expiresAt") if key in checkout}
            if not isinstance(safe.get("codeUrl"), str) or not safe["codeUrl"].startswith(("weixin://", "https://")) or len(safe["codeUrl"]) > 4096:
                raise ValueError("invalid payment URL")
            if "expiresAt" in safe:
                _text(safe["expiresAt"], "支付有效期", maximum=100)
            with self._transaction() as db:
                db.execute("UPDATE billing_orders SET checkout_json=?,status=CASE WHEN status='creating' THEN 'pending_payment' ELSE status END,updated_at=? WHERE id=?",
                           (_json(safe), _now(), order_id))
        except Exception:
            with self._transaction() as db:
                db.execute("UPDATE billing_orders SET status='payment_unavailable',updated_at=? WHERE id=? AND status='creating'", (_now(), order_id))
            current = self.order(owner_id, order_id)
            if current["status"] == "paid":
                return current  # A verified notification may have won this race.
            # Provider messages may include merchant credentials; never publish.
            raise BillingUnavailable("支付渠道暂不可用，订单已保留，未增加积分。") from None
        return self.order(owner_id, order_id)

    def accept_payment(self, event: VerifiedPaymentEvent):
        if not isinstance(event, VerifiedPaymentEvent):
            raise ValidationError("必须由支付适配器提供已验证事件")
        if not self.payment_adapter or self.payment_adapter.configured is not True or event.provider != self.payment_adapter.name:
            raise BillingUnavailable("支付渠道未配置或事件渠道不匹配")
        if event.status != "paid" or event.currency != "CNY":
            raise ValidationError("当前事件不能确认为已支付")
        _integer(event.amount_fen, "支付金额", minimum=1)
        for value in (event.event_id, event.order_id, event.transaction_id):
            _text(value, "支付事件编号", maximum=200)
        digest = _digest(asdict(event))
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM billing_payment_events WHERE provider=? AND event_id=?", (event.provider, event.event_id)).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise ConflictError("重复支付事件内容不一致")
                return {"accepted": True, "duplicate": True, "orderId": event.order_id}
            order = db.execute("SELECT * FROM billing_orders WHERE id=?", (event.order_id,)).fetchone()
            if not order:
                raise NotFoundError("支付订单不存在")
            if order["provider"] != event.provider or order["amount_fen"] != event.amount_fen or order["currency"] != event.currency:
                raise ConflictError("支付金额、币种或渠道与订单不符")
            other = db.execute("SELECT id FROM billing_orders WHERE provider=? AND transaction_id=?", (event.provider, event.transaction_id)).fetchone()
            if other and other[0] != event.order_id:
                raise ConflictError("此支付流水已属于其他订单")
            if order["status"] in {"paid", "refunded"} and order["transaction_id"] != event.transaction_id:
                raise ConflictError("此订单已由另一笔支付完成，需要核账")
            if order["status"] not in {"creating", "pending_payment", "payment_unavailable", "paid", "refunded"}:
                raise ConflictError("订单当前状态不能再次入账")
            self._post(db, owner_id=order["owner_id"], delta=order["credit_units"], kind="purchase",
                       reference=event.order_id, reason="购买积分到账", actor_id="payment-provider",
                       event_key=f"purchase:{event.order_id}")
            if order["status"] != "refunded":
                db.execute("UPDATE billing_orders SET status='paid',transaction_id=?,updated_at=? WHERE id=?", (event.transaction_id, _now(), event.order_id))
            db.execute("INSERT INTO billing_payment_events VALUES (?,?,?,?,?)", (event.provider, event.event_id, digest, event.order_id, _now()))
            self._audit(db, "payment-provider", "payment.accepted", event.order_id, {"eventId": event.event_id, "transactionId": event.transaction_id})
        return {"accepted": True, "duplicate": order["status"] in {"paid", "refunded"}, "orderId": event.order_id}

    def _authorized_order(self, actor_id, order_id, *, permission="billing:read"):
        user = self._owner(actor_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM billing_orders WHERE id=?", (order_id,)).fetchone()
        if not row or (row["owner_id"] != actor_id and "*" not in user.permissions and permission not in user.permissions):
            raise NotFoundError("订单不存在")
        return self._order(row)

    def _query_limit(self, actor_id, reference, kind):
        self.auth.consume_auth_limits((("billing-query-user", actor_id, 12, 60),
                                       ("billing-query-" + kind, reference, 1, 15)))

    def reconcile_order(self, actor_id, order_id):
        order = self._authorized_order(actor_id, order_id, permission="billing:manage")
        adapter = self.payment_adapter
        if not self.status()["paymentQueryAvailable"] or order["provider"] != adapter.name:
            raise BillingUnavailable("当前订单的支付查询渠道尚未配置。")
        self._query_limit(actor_id, order_id, "order")
        try:
            result = adapter.query_order(order)
        except Exception:
            raise BillingUnavailable("暂时无法核实微信订单状态，原订单与积分保持不变，请稍后查询。") from None
        if not isinstance(result, Mapping) or result.get("status") not in {"paid", "pending_payment", "closed", "refund", "failed"}:
            raise ValidationError("支付查询状态不正确")
        event = result.get("event")
        if result.get("status") == "paid":
            if not isinstance(event, VerifiedPaymentEvent) or event.order_id != order_id:
                raise ValidationError("支付查询未提供匹配的已验证事件")
            self.accept_payment(event)
        elif event is not None:
            raise ValidationError("未支付查询不能包含到账事件")
        # Preserve the authoritative payment/refund state. Pending/closed query
        # observations cannot erase a payment that won a callback race.
        with self._transaction() as db:
            self._audit(db, actor_id, "payment.queried", order_id, {"gatewayStatus": result.get("status")})
        return {"order": self._authorized_order(actor_id, order_id), "gatewayStatus": result.get("status")}

    @staticmethod
    def _request(row):
        return {"id": row["id"], "ownerId": row["owner_id"], "orderId": row["order_id"], "kind": row["kind"],
                "data": json.loads(row["data_json"]), "status": row["status"], "adminNote": row["admin_note"],
                "externalReference": row["external_reference"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def request_service(self, owner_id, order_id, *, kind, values, idempotency_key):
        self._owner(owner_id)
        if kind not in {"refund", "invoice"} or not isinstance(values, Mapping):
            raise ValidationError("申请类型不正确")
        key = _text(idempotency_key, "幂等键", maximum=128)
        if kind == "refund":
            data = {"reason": _text(values.get("reason"), "退款原因"),
                    "amountFen": _integer(values.get("amountFen"), "申请金额（分）", minimum=1)}
        else:
            data = {"title": _text(values.get("title"), "发票抬头", maximum=200),
                    "taxId": _text(values.get("taxId", ""), "税号", maximum=50, optional=True),
                    "email": _text(values.get("email"), "收票邮箱", maximum=254)}
            if "@" not in data["email"]:
                raise ValidationError("收票邮箱不正确")
        digest = _digest({"orderId": order_id, "kind": kind, "data": data})
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM billing_requests WHERE owner_id=? AND kind=? AND idempotency_key=?", (owner_id, kind, key)).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise ConflictError("同一幂等键不能重复提交不同申请")
                return self._request(prior)
            order = db.execute("SELECT * FROM billing_orders WHERE id=? AND owner_id=?", (order_id, owner_id)).fetchone()
            if not order:
                raise NotFoundError("订单不存在")
            if order["status"] != "paid":
                raise ConflictError("仅已支付订单可以申请退款或发票")
            if kind == "refund" and data["amountFen"] > order["amount_fen"]:
                raise ValidationError("申请退款金额不能超过订单实付金额")
            if kind == "refund" and order["provider"] == "offline" and data["amountFen"] != order["amount_fen"]:
                raise ValidationError("线下充值当前仅支持整单退款，请按原实付金额申请")
            data["provider"] = order["provider"]
            if db.execute("SELECT 1 FROM billing_requests WHERE order_id=? AND kind=? AND status!='rejected'", (order_id, kind)).fetchone():
                raise ConflictError("此订单已有同类申请，请查看处理进度")
            request_id, now = _id("req"), _now()
            db.execute("INSERT INTO billing_requests (id,owner_id,order_id,kind,idempotency_key,request_hash,data_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'requested',?,?)",
                       (request_id, owner_id, order_id, kind, key, digest, _json(data), now, now))
            self._audit(db, owner_id, f"{kind}.requested", request_id, {"orderId": order_id})
            return self._request(db.execute("SELECT * FROM billing_requests WHERE id=?", (request_id,)).fetchone())

    def review_request(self, actor_id, request_id, *, status, note, external_reference=""):
        self._admin(actor_id, "billing:manage")
        note = _text(note, "处理说明")
        reference = _text(external_reference, "外部凭证", maximum=200, optional=True)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM billing_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise NotFoundError("申请不存在")
            allowed = {"reviewing", "rejected", "approved"} if row["kind"] == "refund" else {"processing", "rejected", "issued"}
            if status not in allowed:
                raise ValidationError("处理状态不正确；渠道未实际退款前不能标为已退款")
            if row["status"] in {"issued", "rejected", "approved", "refund_processing", "refunded", "refund_closed", "refund_abnormal"}:
                if row["status"] == status and row["admin_note"] == note and row["external_reference"] == reference:
                    return self._request(row)
                raise ConflictError("申请已处理，不能覆盖历史结果")
            if status == "issued" and not reference:
                raise ValidationError("已开票需填写真实发票号码或外部凭证")
            db.execute("UPDATE billing_requests SET status=?,admin_note=?,external_reference=?,updated_at=? WHERE id=?",
                       (status, note, reference, _now(), request_id))
            self._audit(db, actor_id, f"{row['kind']}.reviewed", request_id, {"previousStatus": row["status"], "status": status, "note": note, "externalReference": reference})
            return self._request(db.execute("SELECT * FROM billing_requests WHERE id=?", (request_id,)).fetchone())

    @staticmethod
    def _refund(row):
        return {"requestId": row["request_id"], "orderId": row["order_id"], "status": row["status"],
                "amountFen": row["amount_fen"], "creditUnits": row["credit_units"],
                "refundId": row["refund_id"], "updatedAt": row["updated_at"]}

    def _refund_result(self, request_id):
        with self._lock:
            request = self._db.execute("SELECT * FROM billing_requests WHERE id=? AND kind='refund'", (request_id,)).fetchone()
            if not request:
                raise NotFoundError("退款申请不存在")
            order = self._db.execute("SELECT * FROM billing_orders WHERE id=?", (request["order_id"],)).fetchone()
            refund = self._db.execute("SELECT * FROM billing_refunds WHERE request_id=?", (request_id,)).fetchone()
            return {"request": self._request(request), "order": self._order(order),
                    "refund": self._refund(refund) if refund else None}

    def execute_refund(self, actor_id, request_id):
        """Explicit opt-in, full-order refunds only; no invented partial ratio.

        Return the same durable attempt on retries. An uncertain network result
        is reconciled using that same refund ID, never sent under a fresh ID.
        """
        self._admin(actor_id, "billing:manage")
        if not self.status()["refundExecutionEnabled"]:
            raise BillingUnavailable("真实退款尚未开通；审核结果已保留，不会向支付渠道提交退款。")
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM billing_refunds WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                return self._refund_result(request_id)
            request = db.execute("SELECT * FROM billing_requests WHERE id=? AND kind='refund'", (request_id,)).fetchone()
            if not request:
                raise NotFoundError("退款申请不存在")
            order = db.execute("SELECT * FROM billing_orders WHERE id=?", (request["order_id"],)).fetchone()
            if (request["status"] != "approved" or order["status"] != "paid" or not order["transaction_id"]
                    or order["provider"] != self.payment_adapter.name):
                raise ConflictError("仅审核通过且已确认付款的订单可以发起原路退款")
            amount = json.loads(request["data_json"])["amountFen"]
            if amount != order["amount_fen"]:
                raise ConflictError("当前仅支持整单退款；部分退款的积分处理规则未配置，请由运营继续处理。")
            if db.execute("SELECT 1 FROM billing_refunds WHERE order_id=?", (order["id"],)).fetchone():
                raise ConflictError("此订单已有退款执行记录，请查询原退款单，不能重复发起。")
            balance = db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (order["owner_id"],)).fetchone()
            if not balance or balance[0] < order["credit_units"]:
                raise ConflictError("当前积分不足以收回该订单全部积分，未提交微信退款，请先核账。")
            now = _now()
            db.execute("INSERT INTO billing_refunds VALUES (?,?,?,NULL,'submitting',?,?,?,?)",
                       (request_id, order["id"], order["provider"], amount, order["credit_units"], now, now))
            self._post(db, owner_id=order["owner_id"], delta=-order["credit_units"], kind="refund_recovery",
                       reference=request_id, reason="原路整单退款收回充值积分", actor_id="refund-worker",
                       event_key=f"refund-recovery:{request_id}")
            db.execute("UPDATE billing_requests SET status='refund_processing',updated_at=? WHERE id=?", (now, request_id))
            self._audit(db, actor_id, "refund.submitted", request_id, {"orderId": order["id"], "amountFen": amount, "creditUnits": order["credit_units"]})
            gateway_order, gateway_request = self._order(order), self._request(request)
        try:
            event = self.payment_adapter.create_refund(gateway_order, gateway_request)
            if not isinstance(event, VerifiedRefundEvent) or event.refund_request_id != request_id or event.order_id != gateway_order["id"]:
                raise ValidationError("退款提交未提供匹配的已验证事件")
            self.accept_refund(event)
        except Exception:
            # May have been accepted remotely, or the callback may have won.
            # Never restore spendable credits merely because HTTP timed out.
            with self._transaction() as db:
                db.execute("UPDATE billing_refunds SET status='unknown',updated_at=? WHERE request_id=? AND status='submitting'", (_now(), request_id))
            result = self._refund_result(request_id)
            if result["refund"]["status"] not in {"succeeded", "closed"}:
                result["message"] = "退款请求已记录，渠道结果尚待核实，请查询此退款单；不要重复发起。"
            return result
        return self._refund_result(request_id)

    def reconcile_refund(self, actor_id, request_id):
        self._admin(actor_id, "billing:manage")
        result = self._refund_result(request_id)
        adapter = self.payment_adapter
        if (not adapter or adapter.configured is not True or not callable(getattr(adapter, "query_refund", None))
                or result["order"]["provider"] != adapter.name):
            raise BillingUnavailable("当前退款的查询渠道尚未配置。")
        if not result["refund"]:
            raise ConflictError("此申请尚未向支付渠道发起退款")
        self._query_limit(actor_id, request_id, "refund")
        try:
            event = adapter.query_refund(result["order"], result["request"])
        except Exception as exc:
            # A process can stop after recording/recovering credits but before
            # sending HTTP. Only a signed NOT_FOUND may recover that gap, using
            # the exact same merchant refund ID and amount (no second debit).
            if (getattr(exc, "code", None) != "payment_resource_not_exists"
                    or result["refund"]["status"] not in {"unknown", "submitting"}
                    or not self.status()["refundExecutionEnabled"]
                    or (datetime.now(timezone.utc) - datetime.fromisoformat(result["refund"]["updatedAt"])).total_seconds() < 60):
                raise BillingUnavailable("暂时无法核实退款结果，已保留原退款单，请稍后查询。") from None
            try:
                event = adapter.create_refund(result["order"], {**result["request"], "status": "approved"})
            except Exception:
                raise BillingUnavailable("原退款请求仍待核实，已保留相同退款单号与积分回收记录。") from None
        if not isinstance(event, VerifiedRefundEvent) or event.refund_request_id != request_id:
            raise ValidationError("退款查询未提供匹配的已验证事件")
        self.accept_refund(event)
        return self._refund_result(request_id)

    def accept_refund(self, event: VerifiedRefundEvent):
        if not isinstance(event, VerifiedRefundEvent):
            raise ValidationError("必须由支付适配器提供已验证退款事件")
        if not self.payment_adapter or self.payment_adapter.configured is not True or event.provider != self.payment_adapter.name:
            raise BillingUnavailable("退款渠道未配置或事件渠道不匹配")
        if event.status not in {"processing", "succeeded", "closed", "abnormal"} or event.currency != "CNY":
            raise ValidationError("退款事件状态或币种不正确")
        for value in (event.event_id, event.order_id, event.refund_request_id, event.refund_id, event.transaction_id):
            _text(value, "退款事件编号", maximum=200)
        _integer(event.amount_fen, "退款金额", minimum=1)
        _integer(event.total_fen, "原支付金额", minimum=1)
        digest = _digest(asdict(event))
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM billing_refund_events WHERE provider=? AND event_id=?", (event.provider, event.event_id)).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise ConflictError("重复退款事件内容不一致")
                return {"accepted": True, "duplicate": True, "requestId": event.refund_request_id}
            refund = db.execute("SELECT * FROM billing_refunds WHERE request_id=?", (event.refund_request_id,)).fetchone()
            order = db.execute("SELECT * FROM billing_orders WHERE id=?", (event.order_id,)).fetchone()
            if not refund or not order:
                raise NotFoundError("未找到已批准执行的退款单，请核账")
            if (refund["order_id"] != event.order_id or refund["provider"] != event.provider
                    or order["provider"] != event.provider or refund["amount_fen"] != event.amount_fen
                    or order["amount_fen"] != event.total_fen or order["transaction_id"] != event.transaction_id
                    or (refund["refund_id"] and refund["refund_id"] != event.refund_id)):
                raise ConflictError("退款金额、订单、渠道或支付流水不一致")
            other = db.execute("SELECT request_id FROM billing_refunds WHERE provider=? AND refund_id=?", (event.provider, event.refund_id)).fetchone()
            if other and other[0] != event.refund_request_id:
                raise ConflictError("渠道退款流水已属于另一申请")
            recovery = db.execute("SELECT * FROM billing_ledger WHERE event_key=?", (f"refund-recovery:{event.refund_request_id}",)).fetchone()
            if not recovery or recovery["delta_units"] != -refund["credit_units"]:
                raise ConflictError("退款积分回收记录缺失，需要核账")
            previous = refund["status"]
            terminal = previous in {"succeeded", "closed"}
            if terminal and event.status in {"succeeded", "closed"} and event.status != previous:
                raise ConflictError("退款终态与渠道事件冲突，需要核账")
            # A delayed PROCESSING/ABNORMAL observation cannot undo completion.
            next_status = previous if terminal else event.status
            now = _now()
            if next_status == "succeeded":
                total = db.execute("SELECT coalesce(sum(amount_fen),0) FROM billing_refunds WHERE order_id=? AND status='succeeded' AND request_id!=?", (event.order_id, event.refund_request_id)).fetchone()[0]
                if total + event.amount_fen > order["amount_fen"]:
                    raise ConflictError("累计退款金额超过订单实付金额")
                db.execute("UPDATE billing_orders SET status='refunded',updated_at=? WHERE id=?", (now, event.order_id))
            elif next_status == "closed":
                self._post(db, owner_id=order["owner_id"], delta=refund["credit_units"], kind="refund_return",
                           reference=event.refund_request_id, reason="渠道关闭退款，退还已收回积分", actor_id="refund-worker",
                           event_key=f"refund-return:{event.refund_request_id}")
            db.execute("UPDATE billing_refunds SET status=?,refund_id=?,updated_at=? WHERE request_id=?",
                       (next_status, event.refund_id, now, event.refund_request_id))
            request_status = {"processing": "refund_processing", "succeeded": "refunded", "closed": "refund_closed", "abnormal": "refund_abnormal"}[next_status]
            db.execute("UPDATE billing_requests SET status=?,external_reference=?,updated_at=? WHERE id=?", (request_status, event.refund_id, now, event.refund_request_id))
            db.execute("INSERT INTO billing_refund_events VALUES (?,?,?,?,?)", (event.provider, event.event_id, digest, event.refund_request_id, now))
            self._audit(db, "payment-provider", "refund.verified", event.refund_request_id, {"previousStatus": previous, "status": next_status, "eventId": event.event_id, "refundId": event.refund_id})
        return {"accepted": True, "duplicate": previous == next_status, "requestId": event.refund_request_id}

    def record_usage(self, *, owner_id, job_id, call_id, stage, provider, model, attempt_id="",
                     input_tokens=None, output_tokens=None, cached_input_tokens=None, reasoning_output_tokens=None,
                     cost_micro_usd=None, cost_source=None, rate_card_version=None, outcome="completed",
                     cache_write_tokens=None, usage_scope=None):
        """Trusted worker entry, not an HTTP write API. Unknown counts stay null.

        Callers must use one id per real request across diagnostic snapshots;
        retries get new ids, reused transcription does not create another call.
        Supplier cost is never derived from a customer's credit adjustment.
        """
        # Already-started supplier work can finish after an account is disabled;
        # retain its cost while continuing to reject new customer operations.
        self.auth.get_user(_text(owner_id, "用户", maximum=128))
        data = {"ownerId": owner_id, "jobId": _text(job_id, "任务", maximum=128),
                "callId": _text(call_id, "调用", maximum=128), "stage": _text(stage, "调用阶段", maximum=100),
                "provider": _text(provider, "供应商", maximum=100), "model": _text(model, "模型", maximum=128),
                "attemptId": _text(attempt_id, "尝试编号", maximum=128, optional=True),
                "outcome": _text(outcome, "结果", maximum=50)}
        if cache_write_tokens is not None:
            _integer(cache_write_tokens, "缓存写入Token")
            if input_tokens is not None and cache_write_tokens + (cached_input_tokens or 0) > input_tokens:
                raise ValidationError("缓存读取和写入不能大于总输入")
            data["cacheWriteTokens"] = cache_write_tokens
        if usage_scope is not None:
            if usage_scope not in {"request", "aggregate"}:
                raise ValidationError("用量汇总范围无效")
            data["usageScope"] = usage_scope
        counts = {"inputTokens": input_tokens, "outputTokens": output_tokens, "cachedInputTokens": cached_input_tokens,
                  "reasoningOutputTokens": reasoning_output_tokens, "costMicroUsd": cost_micro_usd}
        for key, value in counts.items():
            if value is not None:
                _integer(value, key)
        if input_tokens is not None and cached_input_tokens is not None and cached_input_tokens > input_tokens:
            raise ValidationError("缓存输入不能大于总输入")
        if output_tokens is not None and reasoning_output_tokens is not None and reasoning_output_tokens > output_tokens:
            raise ValidationError("推理输出不能大于总输出")
        if cost_micro_usd is not None:
            if cost_source not in {"provider_reported", "rate_card_calculated"}:
                raise ValidationError("成本必须标明供应商报告或费率计算来源")
            if cost_source == "rate_card_calculated":
                _text(rate_card_version, "费率版本", maximum=128)
        elif cost_source is not None or rate_card_version is not None:
            raise ValidationError("未知成本不能标为已计算")
        data.update({**counts, "costSource": cost_source, "rateCardVersion": rate_card_version,
                     "usageKnown": input_tokens is not None and output_tokens is not None})
        with self._transaction() as db:
            old = db.execute("SELECT * FROM billing_usage WHERE call_id=?", (call_id,)).fetchone()
            if old:
                if old["payload_hash"] != _digest(data):
                    raise ConflictError("已记录供应商调用不能被覆盖；修正需独立核账事件")
                return json.loads(old["data_json"])
            db.execute("INSERT INTO billing_usage VALUES (?,?,?,?,?,?,?,?)", (call_id, owner_id, job_id, _json(data), _digest(data), int(data["usageKnown"]), cost_micro_usd, _now()))
        return data

    def settle_usage(self, *, owner_id, job_id, credit_units, idempotency_key, policy_version, reason,
                     insufficient_balance_policy=None):
        """Atomic terminal charge under an explicitly configured policy.

        No policy is selected here. Legacy callers still receive needs_review
        for insufficient funds. A disabled account can settle work that had
        already started, but cannot start new work or buy credits itself.
        """
        self.auth.get_user(_text(owner_id, "用户", maximum=128))
        units = _integer(credit_units, "结算积分", minimum=1)
        policy = _text(policy_version, "计费规则版本", maximum=128)
        reason = _text(reason, "结算说明")
        job_id = _text(job_id, "任务", maximum=128)
        key = _text(idempotency_key, "幂等键", maximum=128)
        if insufficient_balance_policy is not None and insufficient_balance_policy not in self.supported_insufficient_balance_policies:
            raise ValidationError("余额不足规则不受支持")
        event_key = f"usage:{owner_id}:{key}"
        digest = _digest({"ownerId": owner_id, "jobId": job_id, "creditUnits": units, "policyVersion": policy,
                          "reason": reason, "insufficientBalancePolicy": insufficient_balance_policy})
        with self._transaction() as db:
            previous = db.execute("SELECT * FROM billing_usage_settlements WHERE event_key=?", (event_key,)).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise ConflictError("同一结算不能更换金额或规则")
                return json.loads(previous["result_json"])
            if not self.status()["enabled"]:
                return {"status": "disabled", "chargedUnits": 0}
            balance_row = db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (owner_id,)).fetchone()
            balance = balance_row[0] if balance_row else 0
            if balance < units and insufficient_balance_policy is None:
                return {"status": "needs_review", "chargedUnits": 0, "reason": "insufficient_balance_policy_unresolved"}
            charged = min(balance, units)
            deferred = units - charged if insufficient_balance_policy == "deferred_due" else 0
            absorbed = units - charged if insufficient_balance_policy == "cap_at_balance" else 0
            result = {"status": "settled", "chargedUnits": charged, "deferredUnits": deferred,
                      "platformAbsorbedUnits": absorbed, "expectedCreditUnits": units,
                      "insufficientBalancePolicy": insufficient_balance_policy}
            if charged:
                result["entry"] = self._post(db, owner_id=owner_id, delta=-charged, kind="usage", reference=job_id,
                                             reason=f"{reason}（规则 {policy}）", actor_id="billing-worker", event_key=event_key)
            if deferred:
                due_id = _id("due")
                db.execute("INSERT INTO billing_dues VALUES (?,?,?,?,?)", (due_id, event_key, owner_id, deferred, _now()))
                result["dueId"] = due_id
            db.execute("INSERT INTO billing_usage_settlements VALUES (?,?,?,?,?,?)", (event_key, digest, owner_id, job_id, _json(result), _now()))
            self._audit(db, "billing-worker", "usage.settled", job_id, {**result, "policyVersion": policy, "eventKey": event_key})
            return result

    def list_records(self, kind, *, owner_id=None, actor_id=None, limit=50, offset=0, q='', status='', filter_owner_id='', date_from='', date_to='', source=''):
        _integer(limit, "每页条数", minimum=1, maximum=100)
        _integer(offset, "偏移", maximum=1_000_000)
        if owner_id is None:
            self._admin(actor_id, "billing:read")
        else:
            self._owner(owner_id)
        tables = {"ledger": ("billing_ledger", self._ledger_entry), "orders": ("billing_orders", self._order),
                  "refund": ("billing_requests", self._request), "invoice": ("billing_requests", self._request),
                  "usage": ("billing_usage", lambda row: {**json.loads(row["data_json"]), "createdAt": row["created_at"]}),
                  "audit": ("billing_audit", lambda row: {"id": row["id"], "actorId": row["actor_id"], "action": row["action"], "resourceId": row["resource_id"], "details": json.loads(row["details_json"]), "createdAt": row["created_at"]})}
        if kind not in tables:
            raise ValidationError("记录类型不正确")
        if kind in {"audit", "usage"}:
            self._admin(actor_id, "billing:read")
        table, transform = tables[kind]
        filters, params = [], []
        if source:
            self._admin(actor_id, 'billing:read')
            if kind != 'orders' or source not in {'offline', 'online'}:
                raise ValidationError('订单来源筛选不正确')
            filters.append("provider='offline'" if source == 'offline' else "provider!='offline'")
        if owner_id is not None:
            filters.append("owner_id=?")
            params.append(owner_id)
        if kind in {"refund", "invoice"}:
            filters.append("kind=?")
            params.append(kind)
        if any((q, status, filter_owner_id, date_from, date_to)):
            self._admin(actor_id, 'billing:read')
            searchable = {'orders': ('id','owner_id','transaction_id','offline_json'), 'ledger': ('id','owner_id','reference_id'), 'refund': ('id','owner_id','order_id'), 'invoice': ('id','owner_id','order_id'), 'usage': ('call_id','owner_id','job_id'), 'audit': ('id','actor_id','resource_id')}
            extra, extra_values = admin_record_filters(q=q, status=status, filter_owner_id=filter_owner_id,
                date_from=date_from, date_to=date_to, columns=searchable[kind],
                status_column='status' if kind in {'orders','refund','invoice'} else 'kind' if kind == 'ledger' else None,
                owner_column='actor_id' if kind == 'audit' else 'owner_id')
            filters.extend(extra); params.extend(extra_values)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._lock:
            total = self._db.execute(f"SELECT count(*) FROM {table}{where}", params).fetchone()[0]
            rows = self._db.execute(f"SELECT * FROM {table}{where} ORDER BY julianday(created_at) DESC,rowid DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"items": [transform(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def dashboard(self, actor_id):
        self._admin(actor_id, "billing:read")
        with self._lock:
            orders = self._db.execute("SELECT count(*),coalesce(sum(amount_fen),0) FROM billing_orders WHERE status IN ('paid','refunded')").fetchone()
            refunded = self._db.execute("SELECT coalesce(sum(amount_fen),0) FROM billing_refunds WHERE status='succeeded'").fetchone()[0]
            usage = self._db.execute("SELECT count(*),coalesce(sum(cost_micro_usd),0),coalesce(sum(cost_micro_usd IS NULL),0),coalesce(sum(usage_known=0),0) FROM billing_usage").fetchone()
            requests = self._db.execute("SELECT count(*) FROM billing_requests WHERE kind='refund' AND status IN ('requested','reviewing','approved','refund_processing','refund_abnormal')").fetchone()[0]
            charged = self._db.execute("SELECT coalesce(-sum(delta_units),0) FROM billing_ledger WHERE kind='usage'").fetchone()[0]
            offline = self._db.execute("SELECT coalesce(sum(CASE WHEN kind='recharge' THEN amount_fen ELSE 0 END),0),coalesce(sum(CASE WHEN kind='refund' THEN amount_fen ELSE 0 END),0) FROM billing_offline_records").fetchone()
            non_revenue = self._db.execute("SELECT kind,coalesce(sum(delta_units),0) FROM billing_ledger WHERE kind IN ('gift','compensation','adjustment') GROUP BY kind").fetchall()
        return {"paidOrders": orders[0], "revenueFen": orders[1] - refunded, "grossRevenueFen": orders[1], "refundedFen": refunded,
                "usageCalls": usage[0], "knownCostMicroUsd": usage[1],
                "unknownCostCalls": usage[2], "unknownUsageCalls": usage[3], "chargedCreditUnits": charged,
                "openRefundRequests": requests, "costCurrency": "USD", "revenueCurrency": "CNY",
                "offlineGrossRevenueFen": offline[0], "offlineRefundedFen": offline[1],
                "nonRevenueCreditUnits": {row[0]: row[1] for row in non_revenue},
                "costNote": "仅汇总已记录成本；未知项待核账，不将积分调整计入供应商成本。", **self.status()}
