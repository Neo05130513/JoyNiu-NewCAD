"""Versioned credit rates and sealed, per-attempt settlement hooks.

The production composition may wire these hooks into CAD execution. A policy
snapshot fixes the applicable rules, never reserves a customer's balance. Real call ids
come from the worker journal; neither browser token counts nor old traces can
be used to construct a bill. No price or insufficient-funds policy is seeded.
"""
from __future__ import annotations

import inspect
import json
from fractions import Fraction
from typing import Mapping

from .billing import admin_record_filters, BillingUnavailable, MAX_INTEGER, _digest, _id, _integer, _json, _now, _text
from .platform import ConflictError, NotFoundError, ValidationError
from .official_api_pricing import catalog, exact_decimal, normalize_api_pricing, price_call


TERMINAL_STATUSES = frozenset({"ready", "review_required", "needs_input", "failed", "cancelled", "interrupted"})
INSUFFICIENT_POLICIES = frozenset({"deferred_due", "cap_at_balance"})
RATE_FIELDS = ("inputUnitsPerMillion", "cachedInputUnitsPerMillion", "outputUnitsPerMillion")


class BillingPolicyService:
    def __init__(self, billing):
        self.billing = billing
        # Share the business DB connection/transaction lock. In-memory tests
        # and production SQLite use exactly the same atomic operations.
        with billing._lock, billing._db:
            billing._db.executescript("""
            CREATE TABLE IF NOT EXISTS billing_policy_versions (
              id TEXT PRIMARY KEY, document_json TEXT NOT NULL,
              actor_id TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_policy_state (
              id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
              version_id TEXT, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_policy_attempts (
              attempt_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
              job_id TEXT NOT NULL, version_id TEXT, charge_enabled INTEGER NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_policy_seals (
              attempt_id TEXT PRIMARY KEY, terminal_status TEXT NOT NULL,
              call_ids_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS billing_policy_receipts (
              attempt_id TEXT PRIMARY KEY, result_json TEXT NOT NULL,
              created_at TEXT NOT NULL);
            INSERT OR IGNORE INTO billing_policy_state VALUES (1,0,NULL,0,'');
            """)
            columns = {row[1] for row in billing._db.execute("PRAGMA table_info(billing_policy_attempts)")}
            if "terms_json" not in columns:
                billing._db.execute("ALTER TABLE billing_policy_attempts ADD COLUMN terms_json TEXT NOT NULL DEFAULT '{}'")
            for table in ("billing_policy_versions", "billing_policy_attempts", "billing_policy_seals", "billing_policy_receipts"):
                for operation in ("UPDATE", "DELETE"):
                    billing._db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                                        "BEGIN SELECT RAISE(ABORT, 'immutable billing policy record'); END")

    @staticmethod
    def _version(row):
        return {"id": row["id"], **json.loads(row["document_json"]),
                "createdBy": row["actor_id"], "createdAt": row["created_at"]}

    def supported_policies(self):
        # The wallet owns atomic balance/debt writes. Never simulate a newly
        # selected policy by reading a balance then doing a different debit.
        supported = getattr(self.billing, "supported_insufficient_balance_policies", ())
        supported = supported() if callable(supported) else supported
        return sorted(INSUFFICIENT_POLICIES.intersection(supported or ()))

    @staticmethod
    def _missing(version):
        if version is None:
            return ["rates", "insufficientBalancePolicy", "billableStatuses"]
        missing = []
        if not version["rates"]:
            missing.append("rates")
        if version["insufficientBalancePolicy"] is None:
            missing.append("insufficientBalancePolicy")
        if not version["billableStatuses"]:
            missing.append("billableStatuses")
        return missing

    def status(self):
        """Safe as BillingService.charging_status_provider; no status recursion."""
        with self.billing._lock:
            state = self.billing._db.execute("SELECT * FROM billing_policy_state WHERE id=1").fetchone()
            row = self.billing._db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (state["version_id"],)).fetchone()
        version = self._version(row) if row else None
        missing = self._missing(version)
        supported = self.supported_policies()
        supported_policy = version is not None and version["insufficientBalancePolicy"] in supported
        configured = not missing and supported_policy
        return {"revision": state["revision"], "versionId": state["version_id"],
                "enabled": bool(state["enabled"]) and configured, "configured": configured,
                "missingFields": missing, "supportedInsufficientBalancePolicies": supported,
                "insufficientBalancePolicy": version["insufficientBalancePolicy"] if version else None,
                "settlementTiming": "attempt_terminal", "rounding": "ceil_once_per_attempt",
                "message": "计费规则尚未配置完整，当前不会扣积分。" if missing else
                           "所选余额不足规则尚未实现，当前不会扣积分。" if not supported_policy else
                           "积分计费已启用。" if state["enabled"] else "积分计费已关闭。"}

    def overview(self, actor_id):
        self.billing._admin(actor_id)
        with self.billing._lock:
            rows = self.billing._db.execute("SELECT * FROM billing_policy_versions ORDER BY created_at DESC,id DESC LIMIT 50").fetchall()
            state = self.status()
            if state["versionId"] and all(row["id"] != state["versionId"] for row in rows):
                rows.append(self.billing._db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (state["versionId"],)).fetchone())
        return {**state, "versions": [self._version(row) for row in rows], "officialCatalog": catalog()}

    def pricing(self):
        state = self.status()
        public = {"enabled": state["enabled"], "versionId": state["versionId"] if state["enabled"] else None,
                  "unit": "积分", "tokenUnit": 1_000_000, "settlementTiming": "attempt_terminal",
                  "rounding": "ceil_once_per_attempt", "message": state["message"], "rates": [],
                  "billableStatuses": [], "insufficientBalancePolicy": None}
        if state["enabled"]:
            with self.billing._lock:
                version = self._version(self.billing._db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (state["versionId"],)).fetchone())
            public.update({key: version[key] for key in ("rates", "billableStatuses", "insufficientBalancePolicy")})
            if "apiPricing" in version:
                public["apiPricing"] = version["apiPricing"]
        return public

    def ensure_provider_supported(self, *, owner_id, job_id, attempt_id, provider, model):
        attempt = self._read_attempt(owner_id, job_id, attempt_id)
        if not attempt["charge_enabled"]:
            return
        with self.billing._lock:
            version = self._version(self.billing._db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (attempt["version_id"],)).fetchone())
        if not any(row["provider"] == provider and row["model"] == model for row in version["rates"]):
            raise BillingUnavailable("所选模型的积分费率尚未配置，暂不能启动此付费调用。")

    def save_version(self, actor_id, values):
        self.billing._admin(actor_id)
        if not isinstance(values, Mapping) or set(values) - {"rates", "insufficientBalancePolicy", "billableStatuses", "reason", "apiPricing"}:
            raise ValidationError("计费规则字段不正确")
        reason = _text(values.get("reason"), "变更原因")
        rates = values.get("rates")
        if not isinstance(rates, list) or len(rates) > 100:
            raise ValidationError("费率必须为最多100项的列表")
        normalized, keys = [], set()
        official = "apiPricing" in values
        for row in rates:
            if not isinstance(row, Mapping) or set(row) != ({"provider", "model"} if official else {"provider", "model", *RATE_FIELDS}):
                raise ValidationError("每项费率须指定供应商、模型及三类每百万token积分")
            provider = _text(row["provider"], "供应商", maximum=100)
            model = _text(row["model"], "模型", maximum=128)
            if "*" in provider or "*" in model or (provider, model) in keys:
                raise ValidationError("费率必须按供应商和模型唯一精确配置")
            keys.add((provider, model))
            normalized.append({"provider": provider, "model": model,
                               **({} if official else {key: _integer(row[key], key) for key in RATE_FIELDS})})
        policy = values.get("insufficientBalancePolicy")
        if policy is not None and policy not in INSUFFICIENT_POLICIES:
            raise ValidationError("余额不足规则必须明确选择或保持未配置")
        statuses = values.get("billableStatuses")
        if (not isinstance(statuses, list) or any(not isinstance(item, str) or item not in TERMINAL_STATUSES for item in statuses)
                or len(statuses) != len(set(statuses))):
            raise ValidationError("须明确配置需要按实际用量结算的终态")
        if official and (set(statuses) != {"ready", "review_required"} or policy != "deferred_due"):
            raise ValidationError("官方API折算规则固定仅对成功候选或已确认轮次收费，余额不足时记录待补缴")
        document = {"rates": sorted(normalized, key=lambda row: (row["provider"], row["model"])),
                    "insufficientBalancePolicy": policy, "billableStatuses": sorted(statuses), "reason": reason}
        if official:
            document["apiPricing"], document["rates"] = normalize_api_pricing(values["apiPricing"], document["rates"])
        version_id = _id("bp")
        with self.billing._transaction() as db:
            db.execute("INSERT INTO billing_policy_versions VALUES (?,?,?,?)", (version_id, _json(document), actor_id, _now()))
            self.billing._audit(db, actor_id, "billing_policy.version_created", version_id, document)
            row = db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (version_id,)).fetchone()
        return self._version(row)

    def set_enabled(self, actor_id, *, revision, version_id, enabled, reason):
        self.billing._admin(actor_id)
        revision = _integer(revision, "规则状态版本")
        if type(enabled) is not bool:
            raise ValidationError("enabled必须是布尔值")
        reason = _text(reason, "变更原因")
        if version_id is not None:
            version_id = _text(version_id, "规则版本", maximum=128)
        with self.billing._transaction() as db:
            before = dict(db.execute("SELECT * FROM billing_policy_state WHERE id=1").fetchone())
            if before["revision"] != revision:
                raise ConflictError("计费规则已更新，请刷新后重试")
            row = db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (version_id,)).fetchone()
            if version_id and row is None:
                raise NotFoundError("计费规则版本不存在")
            version = self._version(row) if row else None
            if enabled and (self._missing(version) or version["insufficientBalancePolicy"] not in self.supported_policies()):
                raise ValidationError("费率、收费终态及已实现的余额不足规则必须配置完整后才能启用")
            db.execute("UPDATE billing_policy_state SET revision=revision+1,version_id=?,enabled=?,updated_at=? WHERE id=1",
                       (version_id, int(enabled), _now()))
            self.billing._audit(db, actor_id, "billing_policy.enabled" if enabled else "billing_policy.disabled",
                                version_id or "unconfigured", {"before": before, "enabled": enabled, "versionId": version_id, "reason": reason})
        return self.status()

    def check_start(self, owner_id):
        """New-work admission hook only; no debit, hold, or running-task pause."""
        self.billing._owner(owner_id)
        state = self.status()
        if not state["enabled"]:
            return {"allowed": True, "chargeEnabled": False, "versionId": None}
        terms_acceptance = self.billing.require_terms(owner_id)
        with self.billing._lock:
            row = self.billing._db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (owner_id,)).fetchone()
        balance = row[0] if row else 0
        if balance <= 0:
            return {"allowed": False, "chargeEnabled": True, "versionId": state["versionId"],
                    "reason": "insufficient_balance", "message": "积分余额不足，请充值后再发起新任务。"}
        # Optional wallet-owned debt gate; customer adjustments do not erase it.
        debt_gate = getattr(self.billing, "can_start_charged_work", None)
        if callable(debt_gate) and debt_gate(owner_id) is False:
            return {"allowed": False, "chargeEnabled": True, "versionId": state["versionId"],
                    "reason": "outstanding_debt", "message": "请先补缴已完成任务的待付积分。"}
        return {"allowed": True, "chargeEnabled": True, "versionId": state["versionId"], "termsAcceptance": terms_acceptance}

    def register_attempt(self, *, owner_id, job_id, attempt_id, chargeable=True):
        """Register a trusted worker attempt; read-only enrichment may opt out.

        The internal flag is never read from customer request data. Its first
        registration fixes charging for this attempt, including later retries
        of the registration itself. Ownership/active-account checks still apply.
        """
        if type(chargeable) is not bool:
            raise ValidationError("内部任务计费标志必须为布尔值")
        owner_id, job_id, attempt_id = (_text(value, label, maximum=128) for value, label in
                                       ((owner_id, "用户"), (job_id, "任务"), (attempt_id, "任务尝试")))
        self.billing._owner(owner_id)
        with self.billing._transaction() as db:
            old = db.execute("SELECT * FROM billing_policy_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old:
                if old["owner_id"] != owner_id or old["job_id"] != job_id:
                    raise ConflictError("任务尝试已绑定其他账户或任务")
                return {"allowed": True, "chargeEnabled": bool(old["charge_enabled"]), "versionId": old["version_id"], "attemptId": attempt_id}
            guard = self.check_start(owner_id) if chargeable else {
                "allowed": True, "chargeEnabled": False, "versionId": self.status()["versionId"],
            }
            if not guard["allowed"]:
                return guard
            db.execute("INSERT INTO billing_policy_attempts(attempt_id,owner_id,job_id,version_id,charge_enabled,created_at,terms_json) VALUES (?,?,?,?,?,?,?)",
                       (attempt_id, owner_id, job_id, guard["versionId"], int(guard["chargeEnabled"]), _now(), _json(guard.get("termsAcceptance", {}))))
        return {**guard, "attemptId": attempt_id}

    def recheck_attempt_start(self, *, owner_id, job_id, attempt_id):
        """Admission after queue/preparation, strictly before its first call.

        Preserve the immutable rate/acceptance snapshot; this reads eligibility
        without debiting, reserving, or reassessing a task already in progress.
        """
        self.billing._owner(owner_id)
        attempt = self._read_attempt(owner_id, job_id, attempt_id)
        if not attempt["charge_enabled"]:
            return {"allowed": True, "versionId": attempt["version_id"]}
        self.billing.require_terms(owner_id)
        with self.billing._lock:
            row = self.billing._db.execute("SELECT credit_units FROM billing_accounts WHERE owner_id=?", (owner_id,)).fetchone()
            due = self.billing._due_units(self.billing._db, owner_id)
        if due > 0 or not row or row[0] <= 0:
            return {"allowed": False, "versionId": attempt["version_id"],
                    "message": "排队期间积分余额或待补缴状态已变化，请充值或补缴后重新发起任务。"}
        return {"allowed": True, "versionId": attempt["version_id"]}

    def _read_attempt(self, owner_id, job_id, attempt_id):
        with self.billing._lock:
            row = self.billing._db.execute("SELECT * FROM billing_policy_attempts WHERE attempt_id=? AND owner_id=? AND job_id=?",
                                           (attempt_id, owner_id, job_id)).fetchone()
        if row is None:
            raise NotFoundError("任务未登记计费规则，不能追溯扣款")
        return row

    def _receipt(self, attempt_id, result):
        # Wallet settlement uses a stable attempt idempotency key. If a process
        # dies after the debit, a retry recovers it before saving this receipt.
        with self.billing._transaction() as db:
            row = db.execute("SELECT result_json FROM billing_policy_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row:
                return json.loads(row[0])
            db.execute("INSERT INTO billing_policy_receipts VALUES (?,?,?)", (attempt_id, _json(result), _now()))
        return result

    def settle_attempt(self, *, owner_id, job_id, attempt_id, terminal_status, call_ids):
        """Trusted worker hook after all provider calls stop and journal flushes.

        ``call_ids`` is the final actual-call roster, including failed calls.
        Missing supplier usage remains pending; immutable receipts cannot be
        rewritten when a historical trace or later localization arrives.
        """
        self.billing.auth.get_user(owner_id)  # An inactive owner's completed work is still a fact.
        attempt = self._read_attempt(owner_id, job_id, attempt_id)
        if terminal_status not in TERMINAL_STATUSES:
            raise ValidationError("仅任务真实终态才能结算")
        if (not isinstance(call_ids, list) or len(call_ids) > 10000
                or any(not isinstance(item, str) or not item or len(item) > 128 for item in call_ids)
                or len(call_ids) != len(set(call_ids))):
            raise ValidationError("必须提供唯一的实际调用编号清单")
        roster = sorted(call_ids)
        seal = {"terminalStatus": terminal_status, "callIds": roster}
        with self.billing._transaction() as db:
            old = db.execute("SELECT payload_hash FROM billing_policy_seals WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old and old[0] != _digest(seal):
                raise ConflictError("任务结算清单已封存，不能追加历史或其他调用")
            if not old:
                db.execute("INSERT INTO billing_policy_seals VALUES (?,?,?,?,?)", (attempt_id, terminal_status, _json(roster), _digest(seal), _now()))
            receipt = db.execute("SELECT result_json FROM billing_policy_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if receipt:
                return json.loads(receipt[0])
        base = {"attemptId": attempt_id, "jobId": job_id, "versionId": attempt["version_id"],
                "terminalStatus": terminal_status, "callCount": len(roster), "chargedUnits": 0}
        if not attempt["charge_enabled"]:
            return self._receipt(attempt_id, {**base, "status": "not_charged", "reason": "disabled_at_start", "expectedCreditUnits": 0})
        with self.billing._lock:
            version = self._version(self.billing._db.execute("SELECT * FROM billing_policy_versions WHERE id=?", (attempt["version_id"],)).fetchone())
            raw = self.billing._db.execute("SELECT data_json FROM billing_usage WHERE owner_id=? AND job_id=?", (owner_id, job_id)).fetchall()
        rows = [json.loads(row[0]) for row in raw]
        rows = [row for row in rows if row.get("attemptId") == attempt_id]
        if terminal_status not in version["billableStatuses"]:
            return self._receipt(attempt_id, {**base, "status": "not_charged", "reason": "outcome_excluded_by_policy", "expectedCreditUnits": 0})
        if {row["callId"] for row in rows} != set(roster):
            return {**base, "status": "needs_review", "reason": "call_roster_mismatch", "expectedCreditUnits": None}
        rates = {(row["provider"], row["model"]): row for row in version["rates"]}
        numerator = 0
        exact_points = Fraction(0)
        breakdown = []
        for row in rows:
            rate = rates.get((row["provider"], row["model"]))
            if rate is None:
                return {**base, "status": "needs_review", "reason": "model_rate_missing", "expectedCreditUnits": None}
            incoming, cached, outgoing = (row.get(key) for key in ("inputTokens", "cachedInputTokens", "outputTokens"))
            if any(type(value) is not int or value < 0 for value in (incoming, cached, outgoing)) or cached > incoming:
                return {**base, "status": "needs_review", "reason": "supplier_usage_unknown", "expectedCreditUnits": None}
            if "apiPricing" in version:
                priced = price_call(row, rate, version["apiPricing"])
                if priced is None:
                    return {**base, "status": "needs_review", "reason": "supplier_usage_unknown", "expectedCreditUnits": None}
                exact_points += priced[0]
                breakdown.append(priced[1])
                continue
            numerator += ((incoming - cached) * rate["inputUnitsPerMillion"]
                          + cached * rate["cachedInputUnitsPerMillion"] + outgoing * rate["outputUnitsPerMillion"])
        # Reasoning already belongs to outputTokens. Sum rational charges first
        # and round once for the entire attempt, never once per diagnostic/call.
        if "apiPricing" in version:
            units = -(-exact_points.numerator // exact_points.denominator)
            base.update({"apiPricing": version["apiPricing"], "callBreakdown": breakdown,
                         "apiEquivalentUsd": exact_decimal(sum((Fraction(item["apiEquivalentUsd"]) for item in breakdown), Fraction(0))),
                         "creditUnitsExact": exact_decimal(exact_points),
                         "rateNumerator": str(exact_points.numerator), "rateDenominator": exact_points.denominator})
        else:
            units = (numerator + 999_999) // 1_000_000
            base.update({"rateNumerator": str(numerator), "rateDenominator": 1_000_000})
        base["expectedCreditUnits"] = units
        if units > MAX_INTEGER:
            return {**base, "status": "needs_review", "reason": "credit_amount_out_of_range"}
        if units == 0:
            return self._receipt(attempt_id, {**base, "status": "not_charged", "reason": "zero_actual_charge"})
        if not self.status()["enabled"]:
            return {**base, "status": "pending_settlement", "reason": "charging_disabled"}
        policy = version["insufficientBalancePolicy"]
        if policy not in self.supported_policies():
            return {**base, "status": "needs_review", "reason": "insufficient_balance_policy_unavailable"}
        kwargs = {"owner_id": owner_id, "job_id": job_id, "credit_units": units,
                  "idempotency_key": "attempt-" + _digest({"owner": owner_id, "attempt": attempt_id}),
                  "policy_version": version["id"], "reason": "任务本次实际调用用量结算"}
        if "insufficient_balance_policy" in inspect.signature(self.billing.settle_usage).parameters:
            kwargs["insufficient_balance_policy"] = policy
        result = self.billing.settle_usage(**kwargs)
        combined = {**base, **result}
        if result["status"] == "settled":
            return self._receipt(attempt_id, combined)
        return combined

    def settlements(self, *, owner_id=None, actor_id=None, limit=50, offset=0, q="", status="", filter_owner_id="", date_from="", date_to=""):
        if owner_id is None:
            self.billing._admin(actor_id, "billing:read")
        else:
            self.billing._owner(owner_id)
        _integer(limit, "每页条数", minimum=1, maximum=100)
        _integer(offset, "偏移", maximum=1_000_000)
        where, arguments = (" WHERE a.owner_id=?", [owner_id]) if owner_id else ("", [])
        if any((q,status,filter_owner_id,date_from,date_to)):
            self.billing._admin(actor_id, 'billing:read')
            extra, values = admin_record_filters(q=q, status=status, filter_owner_id=filter_owner_id,
                date_from=date_from, date_to=date_to, columns=('a.attempt_id','a.job_id','a.owner_id'),
                status_column="coalesce(json_extract(r.result_json,'$.status'),'pending_settlement')",
                owner_column='a.owner_id', date_column='s.created_at')
            if extra:
                where += (' AND ' if where else ' WHERE ') + ' AND '.join(extra)
                arguments.extend(values)
        query = " FROM billing_policy_attempts a JOIN billing_policy_seals s ON s.attempt_id=a.attempt_id LEFT JOIN billing_policy_receipts r ON r.attempt_id=a.attempt_id"
        with self.billing._lock:
            total = self.billing._db.execute("SELECT count(*)" + query + where, arguments).fetchone()[0]
            rows = self.billing._db.execute("SELECT a.*,s.terminal_status,r.result_json" + query + where +
                                            " ORDER BY julianday(s.created_at) DESC,a.attempt_id DESC LIMIT ? OFFSET ?", (*arguments, limit, offset)).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "items": [{"attemptId": row["attempt_id"], "jobId": row["job_id"], "ownerId": row["owner_id"],
                                           "versionId": row["version_id"], "terminalStatus": row["terminal_status"],
                                           **(json.loads(row["result_json"]) if row["result_json"] else {"status": "pending_settlement", "expectedCreditUnits": None})}
                                          for row in rows]}
