"""Account-scoped support tickets. Consent records do not grant CAD access."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from uuid import uuid4

from .platform import AuthorizationError, ConflictError, NotFoundError, ValidationError

CATEGORIES = {"modeling", "billing", "account", "data_deletion", "other"}
DATA_SCOPES = {"source_files", "models", "workspace", "account", "support", "backups"}
DATA_OUTCOMES = {"pending_confirmation", "processing", "completed", "partial", "not_processed"}


def _data_request(value):
    if not isinstance(value, dict) or set(value) != {"scopes", "scopeDescription", "acknowledged"}:
        raise ValidationError("请填写数据删除申请的范围与确认信息")
    scopes = value["scopes"]
    if not isinstance(scopes, list) or not scopes or len(scopes) > len(DATA_SCOPES) or any(not isinstance(item, str) or item not in DATA_SCOPES for item in scopes) or len(set(scopes)) != len(scopes):
        raise ValidationError("请选择有效且不重复的数据范围")
    if value["acknowledged"] is not True:
        raise ValidationError("请确认提交申请不会立即删除数据")
    return {"scopes": sorted(scopes), "scopeDescription": _text(value["scopeDescription"], "数据范围说明", 2000), "acknowledged": True}
STATUSES = {"open", "in_progress", "waiting_customer", "resolved", "closed"}
PRIORITIES = {"low", "normal", "high", "urgent"}
TRANSITIONS = {
    "open": {"in_progress", "waiting_customer", "resolved", "closed"},
    "in_progress": {"waiting_customer", "resolved", "closed"},
    "waiting_customer": {"in_progress", "resolved", "closed"},
    "resolved": {"in_progress", "closed"}, "closed": {"in_progress"},
}

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

def _text(value, label, maximum=5000):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValidationError(f"{label}须填写 1 至 {maximum} 个字符")
    return value.strip()

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValidationError("请求编号格式不正确")
    return value

def _page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValidationError("分页参数无效")


class SupportService:
    def __init__(self, auth, database=None, *, cad_store=None):
        self.auth, self.cad_store = auth, cad_store
        self.database = str(database if database is not None else auth.database)
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.database, check_same_thread=False, isolation_level=None, timeout=15)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
          CREATE TABLE IF NOT EXISTS support_tickets (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, subject TEXT NOT NULL,
            category TEXT NOT NULL, run_id TEXT, drawing_consent INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, revision INTEGER NOT NULL, status_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
          CREATE INDEX IF NOT EXISTS support_tickets_owner ON support_tickets(owner_id,updated_at);
          CREATE TABLE IF NOT EXISTS support_data_requests (
            ticket_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS support_data_receipts (
            id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            actor_name TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS support_data_receipts_ticket ON support_data_receipts(ticket_id,created_at);
          CREATE TABLE IF NOT EXISTS support_assignment (
            ticket_id TEXT PRIMARY KEY, assigned_to TEXT, priority TEXT NOT NULL DEFAULT 'normal');
          CREATE INDEX IF NOT EXISTS support_assignment_owner ON support_assignment(assigned_to,priority);
          CREATE TABLE IF NOT EXISTS support_internal_notes (
            id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            actor_name TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS support_notes_ticket ON support_internal_notes(ticket_id,created_at);
          CREATE TABLE IF NOT EXISTS support_messages (
            id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            actor_name TEXT NOT NULL, author_role TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS support_messages_ticket ON support_messages(ticket_id,created_at);
          CREATE TABLE IF NOT EXISTS support_audit (
            id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            action TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS support_audit_ticket ON support_audit(ticket_id,created_at);
          CREATE TABLE IF NOT EXISTS support_idempotency (
            actor_id TEXT NOT NULL, action TEXT NOT NULL, request_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY(actor_id,action,request_key));
        """)
        for table in ("support_messages", "support_internal_notes", "support_audit", "support_idempotency", "support_data_requests", "support_data_receipts"):
            for operation in ("UPDATE", "DELETE"):
                self._db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable support record'); END")

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

    def _actor(self, actor_id, admin=False):
        actor = self.auth.get_user(actor_id)
        if not actor.active:
            raise AuthorizationError("账号已停用")
        if admin and "*" not in actor.permissions and "support:manage" not in actor.permissions:
            raise AuthorizationError("当前账号没有工单管理权限")
        return actor

    def _owned(self, db, actor_id, ticket_id, admin=False):
        row = db.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None or (not admin and row["owner_id"] != actor_id):
            raise NotFoundError("工单不存在")
        return row

    def _ticket(self, row, admin=False):
        total = self._db.execute("SELECT count(*) FROM support_messages WHERE ticket_id=?", (row["id"],)).fetchone()[0]
        result = {"id": row["id"], "number": row["id"], "ownerId": row["owner_id"], "subject": row["subject"],
                "category": row["category"], "runId": row["run_id"], "drawingConsent": bool(row["drawing_consent"]),
                "status": row["status"], "revision": row["revision"], "statusNote": row["status_note"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"], "closedAt": row["closed_at"], "messageCount": total}
        if row["category"] == "data_deletion":
            request = self._db.execute("SELECT * FROM support_data_requests WHERE ticket_id=?", (row["id"],)).fetchone()
            receipts = self._db.execute("SELECT * FROM support_data_receipts WHERE ticket_id=? ORDER BY created_at,id", (row["id"],)).fetchall()
            result["dataRequest"] = {**json.loads(request["request_json"]), "createdAt": request["created_at"]} if request else None
            result["dataReceipts"] = [{"id": receipt["id"], "authorName": receipt["actor_name"], "createdAt": receipt["created_at"], **json.loads(receipt["result_json"])} for receipt in receipts]
        if admin:
            assignment = self._db.execute("SELECT assigned_to,priority FROM support_assignment WHERE ticket_id=?", (row["id"],)).fetchone()
            assigned_to = assignment["assigned_to"] if assignment else None
            assignee = None
            if assigned_to:
                try:
                    user = self.auth.get_user(assigned_to)
                    assignee = {"id": user.id, "displayName": user.display_name, "available": user.active and bool({"*", "support:manage"}.intersection(user.permissions))}
                except NotFoundError:
                    assignee = {"id": assigned_to, "displayName": "已移除的负责人", "available": False}
            result.update(assignedTo=assigned_to, assignee=assignee, priority=assignment["priority"] if assignment else "normal",
                          internalNoteCount=self._db.execute("SELECT count(*) FROM support_internal_notes WHERE ticket_id=?", (row["id"],)).fetchone()[0])
        return result

    def assignees(self, actor_id):
        self._actor(actor_id, True)
        return {"items": [{"id": user.id, "displayName": user.display_name, "email": user.email}
                          for user in self.auth.list_users() if user.active and {"*", "support:manage"}.intersection(user.permissions)]}

    @staticmethod
    def _message(row):
        return {"id": row["id"], "ticketId": row["ticket_id"], "actorId": row["actor_id"], "authorName": row["actor_name"],
                "authorRole": row["author_role"], "body": row["body"], "createdAt": row["created_at"]}

    def _audit(self, db, actor_id, ticket_id, action, details):
        db.execute("INSERT INTO support_audit VALUES (?,?,?,?,?,?)", ("sa_" + uuid4().hex, ticket_id, actor_id, action, _json(details), _now()))

    def _replay(self, db, actor_id, action, key, payload):
        row = db.execute("SELECT payload_hash,result_json FROM support_idempotency WHERE actor_id=? AND action=? AND request_key=?", (actor_id, action, key)).fetchone()
        if not row:
            return None
        if row["payload_hash"] != hashlib.sha256(_json(payload).encode()).hexdigest():
            raise ConflictError("同一请求编号不能用于不同内容，请确认前次结果后重新提交")
        return json.loads(row["result_json"])

    def _remember(self, db, actor_id, action, key, payload, result):
        db.execute("INSERT INTO support_idempotency VALUES (?,?,?,?,?)", (actor_id, action, key, hashlib.sha256(_json(payload).encode()).hexdigest(), _json(result)))

    def _add_message(self, db, actor, ticket_id, body, admin):
        message_id = "sm_" + uuid4().hex
        db.execute("INSERT INTO support_messages VALUES (?,?,?,?,?,?,?)", (message_id, ticket_id, actor.id, actor.display_name, "admin" if admin else "customer", body, _now()))
        self._audit(db, actor.id, ticket_id, "message.created", {"messageId": message_id, "authorRole": "admin" if admin else "customer"})
        return message_id

    def create(self, actor_id, values):
        actor = self._actor(actor_id)
        required = {"subject", "category", "body", "runId", "drawingConsent", "idempotencyKey"}
        if not isinstance(values, dict) or set(values) - (required | {"dataRequest"}) or not required <= set(values):
            raise ValidationError("工单创建字段不正确")
        key = _key(values["idempotencyKey"])
        subject, body = _text(values["subject"], "问题标题", 120), _text(values["body"], "问题说明")
        if not isinstance(values["category"], str) or values["category"] not in CATEGORIES or type(values["drawingConsent"]) is not bool:
            raise ValidationError("问题分类或附图授权无效")
        data_request = _data_request(values.get("dataRequest")) if values["category"] == "data_deletion" else None
        if data_request is None and "dataRequest" in values:
            raise ValidationError("仅数据删除申请可以附带数据范围")
        run_id = None if values["runId"] is None else _text(values["runId"], "任务编号", 128)
        if values["drawingConsent"] and not run_id:
            raise ValidationError("附图授权须关联自己的建模任务")
        payload = {"subject": subject, "body": body, "category": values["category"], "runId": run_id, "drawingConsent": values["drawingConsent"]}
        if data_request:
            payload["dataRequest"] = data_request
        with self._transaction() as db:
            replay = self._replay(db, actor_id, "ticket.create", key, payload)
            if replay:
                return self._ticket(self._owned(db, actor_id, replay["ticketId"]))
            if run_id:
                if self.cad_store is None:
                    raise ValidationError("当前服务未配置任务关联，请清空任务编号后提交")
                try:
                    record = self.cad_store.load(run_id)
                except (KeyError, ValueError):
                    raise NotFoundError("关联任务不存在或不属于当前账号") from None
                if record.get("owner") != actor_id:
                    raise NotFoundError("关联任务不存在或不属于当前账号")
            ticket_id = "SUP-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid4().hex[:16].upper()
            now = _now()
            db.execute("INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (ticket_id, actor_id, subject, values["category"], run_id, int(values["drawingConsent"]), "open", 1, "", now, now, None))
            if data_request:
                db.execute("INSERT INTO support_data_requests VALUES (?,?,?)", (ticket_id, _json(data_request), now))
            self._audit(db, actor_id, ticket_id, "ticket.created", {"category": values["category"], "runId": run_id, "drawingConsent": values["drawingConsent"]})
            self._add_message(db, actor, ticket_id, body, False)
            self._remember(db, actor_id, "ticket.create", key, payload, {"ticketId": ticket_id})
            return self._ticket(self._owned(db, actor_id, ticket_id))

    def list_tickets(self, actor_id, *, admin=False, status=None, limit=20, offset=0, q='', assigned_to=None, priority=None):
        self._actor(actor_id, admin)
        _page(limit, offset)
        if status is not None and status not in STATUSES and not (admin and status == 'pending'):
            raise ValidationError("工单状态无效")
        if not isinstance(q, str) or len(q) > 128:
            raise ValidationError("搜索内容最多 128 个字符")
        if not admin and (q or assigned_to is not None or priority is not None):
            raise AuthorizationError("工单分派筛选仅供后台使用")
        if priority is not None and (not isinstance(priority, str) or priority not in PRIORITIES):
            raise ValidationError("工单优先级无效")
        if assigned_to is not None and (not isinstance(assigned_to, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', assigned_to)):
            raise ValidationError("负责人筛选无效")
        filters, args = ([] if admin else ["owner_id=?"]), ([] if admin else [actor_id])
        if status == 'pending':
            filters.append("status IN ('open','in_progress')")
        elif status:
            filters.append("status=?"); args.append(status)
        if q.strip():
            term = '%' + q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            filters.append("(id LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' OR owner_id LIKE ? ESCAPE '\\' OR run_id LIKE ? ESCAPE '\\')")
            args.extend([term] * 4)
        if assigned_to == 'unassigned':
            filters.append("NOT EXISTS (SELECT 1 FROM support_assignment a WHERE a.ticket_id=support_tickets.id AND a.assigned_to IS NOT NULL)")
        elif assigned_to:
            filters.append("EXISTS (SELECT 1 FROM support_assignment a WHERE a.ticket_id=support_tickets.id AND a.assigned_to=?)")
            args.append(actor_id if assigned_to == 'me' else assigned_to)
        if priority:
            filters.append("coalesce((SELECT priority FROM support_assignment a WHERE a.ticket_id=support_tickets.id),'normal')=?")
            args.append(priority)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._lock:
            total = self._db.execute("SELECT count(*) FROM support_tickets" + where, args).fetchone()[0]
            rows = self._db.execute("SELECT * FROM support_tickets" + where + " ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            result = {"items": [self._ticket(row, admin) for row in rows], "total": total, "limit": limit, "offset": offset}
            if admin:
                pending = "FROM support_tickets t LEFT JOIN support_assignment a ON a.ticket_id=t.id WHERE t.status IN ('open','in_progress')"
                result['views'] = {'pending': self._db.execute('SELECT count(*) ' + pending).fetchone()[0],
                                   'mine': self._db.execute('SELECT count(*) ' + pending + ' AND a.assigned_to=?', (actor_id,)).fetchone()[0],
                                   'unassigned': self._db.execute('SELECT count(*) ' + pending + ' AND a.assigned_to IS NULL').fetchone()[0]}
            return result

    def ticket(self, actor_id, ticket_id, *, admin=False):
        self._actor(actor_id, admin)
        with self._lock:
            return self._ticket(self._owned(self._db, actor_id, ticket_id, admin), admin)

    def messages(self, actor_id, ticket_id, *, admin=False, limit=50, offset=0):
        self._actor(actor_id, admin); _page(limit, offset)
        with self._lock:
            self._owned(self._db, actor_id, ticket_id, admin)
            total = self._db.execute("SELECT count(*) FROM support_messages WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
            rows = self._db.execute("SELECT * FROM support_messages WHERE ticket_id=? ORDER BY created_at,id LIMIT ? OFFSET ?", (ticket_id, limit, offset)).fetchall()
            return {"items": [self._message(row) for row in rows], "total": total, "offset": offset, "limit": limit}

    def append(self, actor_id, ticket_id, values, *, admin=False):
        actor = self._actor(actor_id, admin)
        if not isinstance(values, dict) or set(values) != {"body", "idempotencyKey"}:
            raise ValidationError("工单说明字段不正确")
        body, key = _text(values["body"], "说明内容"), _key(values["idempotencyKey"])
        action, payload = f"message:{ticket_id}", {"body": body, "admin": admin}
        with self._transaction() as db:
            ticket = self._owned(db, actor_id, ticket_id, admin)
            replay = self._replay(db, actor_id, action, key, payload)
            if replay:
                message = db.execute("SELECT * FROM support_messages WHERE id=?", (replay["messageId"],)).fetchone()
                return {"ticket": self._ticket(ticket, admin), "message": self._message(message)}
            if ticket["status"] == "closed":
                raise ConflictError("工单已关闭，不能追加说明；管理员可重新开启处理")
            message_id = self._add_message(db, actor, ticket_id, body, admin)
            status = "waiting_customer" if admin else "open"
            db.execute("UPDATE support_tickets SET status=?,revision=revision+1,updated_at=?,status_note='' WHERE id=?", (status, _now(), ticket_id))
            if status != ticket["status"]:
                self._audit(db, actor_id, ticket_id, "status.changed", {"before": ticket["status"], "after": status, "reason": "管理员已回复，等待客户核对" if admin else "客户已补充说明"})
            self._remember(db, actor_id, action, key, payload, {"messageId": message_id})
            return {"ticket": self._ticket(self._owned(db, actor_id, ticket_id, admin), admin), "message": self._message(db.execute("SELECT * FROM support_messages WHERE id=?", (message_id,)).fetchone())}

    def update(self, actor_id, ticket_id, values, *, admin=False):
        self._actor(actor_id, admin)
        allowed = {"revision", "idempotencyKey", "status", "reason", "assignedTo", "priority"} if admin else {"revision", "idempotencyKey", "status", "drawingConsent"}
        if not isinstance(values, dict) or set(values) - allowed or not {"revision", "idempotencyKey"} <= set(values) or not {"status", "drawingConsent", "assignedTo", "priority"}.intersection(values):
            raise ValidationError("工单更新字段不正确")
        if type(values["revision"]) is not int or values["revision"] < 1:
            raise ValidationError("须提供正确的工单版本")
        key = _key(values["idempotencyKey"])
        if "status" in values and (not isinstance(values["status"], str) or values["status"] not in STATUSES or (not admin and values["status"] != "closed")):
            raise ValidationError("客户仅可关闭工单；处理状态须由管理员修改")
        if "drawingConsent" in values and type(values["drawingConsent"]) is not bool:
            raise ValidationError("附图授权必须为布尔值")
        if 'priority' in values and (not isinstance(values['priority'], str) or values['priority'] not in PRIORITIES):
            raise ValidationError('工单优先级无效')
        if 'assignedTo' in values and values['assignedTo'] is not None:
            target = _text(values['assignedTo'], '负责人', 128)
            if target != values['assignedTo']:
                raise ValidationError('负责人编号无效')
            try:
                self._actor(target, True)
            except (AuthorizationError, NotFoundError):
                raise ValidationError('负责人必须是具有工单管理权限的已启用账号') from None
        reason = _text(values.get("reason"), "状态变更说明", 500) if admin else "客户关闭工单"
        payload = {key: value for key, value in values.items() if key != "idempotencyKey"}
        action = f"ticket.update:{ticket_id}"
        with self._transaction() as db:
            ticket = self._owned(db, actor_id, ticket_id, admin)
            if self._replay(db, actor_id, action, key, payload):
                return self._ticket(ticket, admin)
            if ticket["revision"] != values["revision"]:
                raise ConflictError("工单已有新内容，请刷新后重新确认")
            status = values.get("status", ticket["status"])
            if admin and ticket["category"] == "data_deletion" and status == "resolved":
                receipt = db.execute("SELECT result_json FROM support_data_receipts WHERE ticket_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (ticket_id,)).fetchone()
                if not receipt or json.loads(receipt["result_json"])["outcome"] not in {"completed", "partial", "not_processed"}:
                    raise ValidationError("请先记录数据处理回执，再标记已提供处理结论；工单状态不代表数据已删除")
            consent = values.get("drawingConsent", bool(ticket["drawing_consent"]))
            if consent and not ticket["run_id"]:
                raise ValidationError("附图授权须关联自己的建模任务")
            if admin and status != ticket["status"] and status not in TRANSITIONS[ticket["status"]]:
                raise ValidationError("当前工单不支持此状态流转")
            now = _now()
            db.execute("UPDATE support_tickets SET status=?,drawing_consent=?,revision=revision+1,status_note=?,updated_at=?,closed_at=? WHERE id=?",
                       (status, int(consent), reason if "status" in values else ticket["status_note"], now, (ticket["closed_at"] or now) if status == "closed" else None, ticket_id))
            if "status" in values:
                self._audit(db, actor_id, ticket_id, "status.changed", {"before": ticket["status"], "after": status, "reason": reason})
            if "drawingConsent" in values:
                self._audit(db, actor_id, ticket_id, "drawing_consent.changed", {"before": bool(ticket["drawing_consent"]), "after": consent, "runId": ticket["run_id"]})
            if 'assignedTo' in values or 'priority' in values:
                old = db.execute('SELECT assigned_to,priority FROM support_assignment WHERE ticket_id=?', (ticket_id,)).fetchone()
                previous = {'assignedTo': old['assigned_to'] if old else None, 'priority': old['priority'] if old else 'normal'}
                new = {field: values.get(field, previous[field]) for field in previous}
                db.execute('INSERT INTO support_assignment VALUES (?,?,?) ON CONFLICT(ticket_id) DO UPDATE SET assigned_to=excluded.assigned_to,priority=excluded.priority', (ticket_id,new['assignedTo'],new['priority']))
                self._audit(db, actor_id, ticket_id, 'assignment.changed', {'before': previous, 'after': new, 'reason': reason})
            self._remember(db, actor_id, action, key, payload, {"ticketId": ticket_id})
            return self._ticket(self._owned(db, actor_id, ticket_id, admin), admin)

    def record_data_receipt(self, actor_id, ticket_id, values):
        """Record a manually verified outcome; this method never deletes customer data."""
        actor = self._actor(actor_id, True)
        fields = {"revision", "outcome", "handledScope", "retainedScope", "retentionPlan", "evidenceRef", "confirmed", "idempotencyKey"}
        if not isinstance(values, dict) or set(values) != fields or type(values["revision"]) is not int or values["revision"] < 1:
            raise ValidationError("数据处理回执字段或工单版本不正确")
        if not isinstance(values["outcome"], str) or values["outcome"] not in DATA_OUTCOMES or values["confirmed"] is not True:
            raise ValidationError("请选择实际处理结果并确认回执内容")
        result = {"outcome": values["outcome"], **{key: _text(values[key], label, maximum) for key, label, maximum in (
            ("handledScope", "已处理或待处理范围", 2000), ("retainedScope", "仍保留的数据与原因", 2000),
            ("retentionPlan", "保留期限与备份处理安排", 2000), ("evidenceRef", "可核对的处理记录编号", 500))}}
        payload = {**result, "revision": values["revision"], "confirmed": True}
        key, action = _key(values["idempotencyKey"]), f"data.receipt:{ticket_id}"
        with self._transaction() as db:
            ticket = self._owned(db, actor_id, ticket_id, True)
            if self._replay(db, actor_id, action, key, payload):
                return self._ticket(ticket, True)
            if ticket["category"] != "data_deletion":
                raise ValidationError("此工单不是数据删除申请")
            if ticket["revision"] != values["revision"]:
                raise ConflictError("工单已有新内容，请刷新后重新确认")
            if ticket["status"] == "closed":
                raise ValidationError("请先重新开启工单，再记录处理回执")
            receipt_id, now = "sdr_" + uuid4().hex, _now()
            db.execute("INSERT INTO support_data_receipts VALUES (?,?,?,?,?,?)", (receipt_id, ticket_id, actor.id, actor.display_name, _json(result), now))
            status = "waiting_customer" if result["outcome"] == "pending_confirmation" else "in_progress" if result["outcome"] == "processing" else "resolved"
            db.execute("UPDATE support_tickets SET status=?,revision=revision+1,updated_at=?,status_note=? WHERE id=?", (status, now, "已记录人工数据处理回执；请查看回执中的实际范围与保留安排。", ticket_id))
            self._audit(db, actor_id, ticket_id, "data.receipt.created", {"receiptId": receipt_id, "outcome": result["outcome"], "previousStatus": ticket["status"], "status": status})
            self._remember(db, actor_id, action, key, payload, {"receiptId": receipt_id})
            return self._ticket(self._owned(db, actor_id, ticket_id, True), True)

    def notes(self, actor_id, ticket_id, *, limit=20, offset=0):
        self._actor(actor_id, True); _page(limit, offset)
        with self._lock:
            self._owned(self._db, actor_id, ticket_id, True)
            total = self._db.execute('SELECT count(*) FROM support_internal_notes WHERE ticket_id=?', (ticket_id,)).fetchone()[0]
            rows = self._db.execute('SELECT * FROM support_internal_notes WHERE ticket_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (ticket_id,limit,offset)).fetchall()
            return {'items': [dict(id=row['id'], ticketId=row['ticket_id'], actorId=row['actor_id'], authorName=row['actor_name'], body=row['body'], createdAt=row['created_at']) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def append_note(self, actor_id, ticket_id, values):
        actor = self._actor(actor_id, True)
        if not isinstance(values, dict) or set(values) != {'body','revision','idempotencyKey'}:
            raise ValidationError('内部备注字段不正确')
        if type(values['revision']) is not int or values['revision'] < 1:
            raise ValidationError('须提供正确的工单版本')
        payload = {'body': _text(values['body'], '内部备注'), 'revision': values['revision']}
        key, action = _key(values['idempotencyKey']), f'note:{ticket_id}'
        with self._transaction() as db:
            ticket = self._owned(db, actor_id, ticket_id, True)
            replay = self._replay(db, actor_id, action, key, payload)
            if replay:
                return {'ticket': self._ticket(ticket, True), 'noteId': replay['noteId']}
            if ticket['revision'] != values['revision']:
                raise ConflictError('工单已有新内容，请刷新后重新确认')
            note_id = 'sn_' + uuid4().hex
            db.execute('INSERT INTO support_internal_notes VALUES (?,?,?,?,?,?)', (note_id,ticket_id,actor.id,actor.display_name,payload['body'],_now()))
            # Internal notes never become public replies or change customer-facing status.
            db.execute('UPDATE support_tickets SET revision=revision+1 WHERE id=?', (ticket_id,))
            self._audit(db, actor_id, ticket_id, 'note.created', {'noteId': note_id})
            self._remember(db, actor_id, action, key, payload, {'noteId': note_id})
            return {'ticket': self._ticket(self._owned(db,actor_id,ticket_id,True), True), 'noteId': note_id}

    def audit(self, actor_id, ticket_id, *, limit=50, offset=0):
        self._actor(actor_id, True); _page(limit, offset)
        with self._lock:
            self._owned(self._db, actor_id, ticket_id, True)
            total = self._db.execute("SELECT count(*) FROM support_audit WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
            rows = self._db.execute("SELECT * FROM support_audit WHERE ticket_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (ticket_id, limit, offset)).fetchall()
            return {"total": total, "offset": offset, "limit": limit, "items": [{"id": row["id"], "actorId": row["actor_id"], "action": row["action"], "details": json.loads(row["details_json"]), "createdAt": row["created_at"]} for row in rows]}
