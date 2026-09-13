"""Permission-gated operations projections over the existing account/CAD databases.

No workspace text, drawing bytes, model plans, raw errors, or capability URLs
are read into an operations response. Cancel audit and queue mutation share one
CAD database transaction, so retrying cannot create an unaudited stop request.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import heapq
import importlib
import itertools
import json
import math
import os
import re
import shutil
import sqlite3
import threading
from uuid import uuid4

from .platform import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .cad_job_registry import CadJobRegistry
from .operations_report import read_operations_report

STATUSES = frozenset({'queued', 'running', 'cancel_requested', 'needs_input', 'review_required', 'ready', 'failed', 'interrupted', 'cancelled'})
ACTIVE = frozenset({'queued', 'running', 'cancel_requested'})
STAGES = STATUSES | frozenset({'started', 'prepare', 'preparing', 'read', 'reading', 'source_reader', 'source_reading', 'source_transcription', 'source_spatial', 'source_question_review', 'source_question_resolved', 'source_locations', 'planning', 'thinking', 'build', 'building', 'execute', 'execute_plan', 'inspect', 'inspect_geometry', 'review', 'drawing_review', 'finalize', 'human_confirmation', 'provider_retry', 'retry', 'queued_for_resource'})
STAGES |= frozenset({'agent_working','plan_saved','provider_error','read_source','interpret_source_spatial','observe_source','geometry_repair','inspect_source','record_observations','observations_recorded','observations_rejected','edit_plan','inspect_draft','projection_compare','independent_drawing_review','ask_user','finish','source_transcription_reused','source_spatial_interpretation','source_spatial_reused','source_spatial_complete','source_spatial_unavailable'})
ERRORS = frozenset({'timeout', 'transport', 'incomplete', 'invalid_json', 'empty_response', 'invalid_stream', 'authentication', 'permission_denied', 'rate_limit', 'quota_exceeded', 'context_length', 'invalid_input', 'invalid_plan', 'invalid_geometry', 'worker_timeout', 'worker_interrupted', 'worker_failed', 'cancelled', 'provider_error', 'billing_admission', 'source_reader_failed', 'source_identity_mismatch', 'source_localization_timeout', 'source_localization_unavailable', 'inspection_failed', 'execution_failed', 'geometry_failed'})
ERRORS |= frozenset({'invalid_response','unexpected_tool','output_limit','upstream_error','provider_failure','time_limit','turn_limit','source_limit','not_configured','source_required','source_transcription_failed','source_inspection_limit','invalid_final_json','connection_error','connection_reset','network_error'})
ERRORS |= frozenset('http_' + str(code) for code in range(400,600))
_STAGE_VALUES = ','.join("'" + item + "'" for item in sorted(STAGES))
_TRACE_STAGE = """(SELECT CASE WHEN json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.stage') IN (STAGES) THEN json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.stage') WHEN json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.action') IN (STAGES) THEN json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.action') ELSE json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.status') END
    FROM json_each(r.payload,'$.trace') t WHERE t.type='object' AND
    (json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.stage') IN (STAGES) OR json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.action') IN (STAGES) OR json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{}' END,'$.status') IN (STAGES))
    ORDER BY cast(t.key AS INTEGER) DESC LIMIT 1)""".replace('STAGES',_STAGE_VALUES)
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
MODEL = re.compile(r'(?:gpt-(?:6-astra|5(?:\.[1-6])?(?:-(?:sol|terra|luna|mini|nano|pro|codex|codex-spark))?|4(?:o|\.1)(?:-mini)?)|o[134](?:-mini|-pro)?|claude-(?:sonnet|opus|haiku)-[0-9]+(?:-[0-9]+)?|gemini-[0-9]+\.[0-9]+-(?:pro|flash)|deepseek-(?:chat|reasoner))(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?\Z', re.I)
PROVIDERS = frozenset({'codex-cli', 'codex', 'openai', 'remote', 'relay', 'anthropic', 'gemini', 'deepseek', 'qwen', 'deterministic-ui-fixture'})
HANDLING_STATUSES = frozenset({'unassigned', 'pending', 'in_progress', 'resolved'})
PRIORITIES = frozenset({'normal', 'high', 'urgent'})
BUSINESS_ZONE = timezone(timedelta(hours=8))


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) and not value.lower().startswith(('sk-', 'bearer', 'token-')) else None


def moment(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).isoformat()
    except ValueError:
        return None


def number(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def first_known(values, allowed):
    return next((value for value in values if isinstance(value,str) and value in allowed), None)


def bounded_count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def elapsed_value(value):
    return value if type(value) in (int,float) and math.isfinite(value) and 0 <= value <= 1_000_000_000 else None


def model_name(value):
    return value if isinstance(value, str) and MODEL.fullmatch(value) else None


def like(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValidationError('分页参数不正确')


def query_text(value):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > 128:
        raise ValidationError('搜索条件最多128字符')
    return value.strip()


def table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


@lru_cache(maxsize=1)
def kernel_status():
    try:
        module = importlib.import_module('cadquery')
        return {'available': True, 'engine': 'cadquery-occt', 'version': str(module.__version__)[:40], 'executionProbe': False}
    except Exception:
        return {'available': False, 'engine': 'cadquery-occt', 'version': None, 'executionProbe': False}


class AdminOperationsService:
    def __init__(self, services, *, cad_store, billing=None, billing_policy=None, database=None):
        self.auth = services.auth
        self.store = cad_store
        self.registry = CadJobRegistry(cad_store)
        self.billing, self.policy = billing, billing_policy
        self._lock = threading.RLock()
        self._cad = sqlite3.connect(cad_store.database, timeout=15, check_same_thread=False)
        self._cad.row_factory = sqlite3.Row
        self._owns_platform = database is not None and str(database) != str(self.auth.database)
        self._platform = sqlite3.connect(database, timeout=15, check_same_thread=False) if self._owns_platform else self.auth._connection
        self._platform.row_factory = sqlite3.Row
        self._cad.executescript('''
          CREATE TABLE IF NOT EXISTS admin_operations_audit (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
            target_id TEXT NOT NULL, reason TEXT NOT NULL, accepted INTEGER NOT NULL, created_at TEXT NOT NULL, financial_visible INTEGER);
          CREATE TABLE IF NOT EXISTS admin_operations_idempotency (
            actor_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            audit_id TEXT NOT NULL, run_id TEXT NOT NULL, accepted INTEGER NOT NULL,
            PRIMARY KEY(actor_id,request_key));
          CREATE TABLE IF NOT EXISTS admin_customer_crm (
            owner_id TEXT PRIMARY KEY, company TEXT NOT NULL, contact_name TEXT NOT NULL,
            phone TEXT NOT NULL, tags_json TEXT NOT NULL, internal_note TEXT NOT NULL,
            version INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS admin_task_handling (
            run_id TEXT PRIMARY KEY, status TEXT NOT NULL, priority TEXT NOT NULL,
            version INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS admin_operations_notes (
            id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS admin_operations_notes_target ON admin_operations_notes(target_kind,target_id,created_at DESC,id DESC);
          CREATE TABLE IF NOT EXISTS admin_operations_mutations (
            actor_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL, PRIMARY KEY(actor_id,request_key));
        ''')
        if 'financial_visible' not in {row[1] for row in self._cad.execute('PRAGMA table_info(admin_operations_audit)')}:
            self._cad.execute('ALTER TABLE admin_operations_audit ADD COLUMN financial_visible INTEGER')
        for name in ('admin_operations_audit', 'admin_operations_idempotency', 'admin_operations_notes', 'admin_operations_mutations'):
            for operation in ('UPDATE', 'DELETE'):
                self._cad.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_no_{operation.lower()} BEFORE {operation} ON {name} BEGIN SELECT RAISE(ABORT,'immutable operations record'); END")
        self._cad.commit()

    def close(self):
        self._cad.close()
        if self._owns_platform:
            self._platform.close()

    def actor(self, actor_id, permission):
        actor = self.auth.get_user(actor_id)
        if not actor.active or not self.allowed(actor, permission):
            raise AuthorizationError('没有此后台操作权限')
        return actor

    @staticmethod
    def allowed(actor, permission):
        permissions = actor.permissions
        return bool({'*', permission} & set(permissions))

    def _financial(self, actor):
        return self.allowed(actor, 'billing:read')

    def _business(self):
        return self.billing._db if self.billing is not None else self._platform

    def _business_lock(self):
        return self.billing._lock if self.billing is not None else self.auth._lock

    @staticmethod
    def _text(value, name, maximum, minimum=0):
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ValidationError(f'{name}须为{minimum}至{maximum}字符')
        value = value.strip()
        if any(ord(char) < 32 and char not in '\n\t' for char in value):
            raise ValidationError(f'{name}包含无效控制字符')
        return value

    @staticmethod
    def _fields(values, fields):
        if not isinstance(values, dict) or set(values) != set(fields):
            raise ValidationError('提交字段不正确，请刷新页面后重试')

    def _crm(self, owner):
        with self._lock:
            row = self._cad.execute('SELECT * FROM admin_customer_crm WHERE owner_id=?', (owner,)).fetchone()
        return {'company': row['company'] if row else '', 'contactName': row['contact_name'] if row else '',
                'phone': row['phone'] if row else '', 'tags': json.loads(row['tags_json']) if row else [],
                'internalNote': row['internal_note'] if row else '', 'version': row['version'] if row else 0,
                'updatedAt': moment(row['updated_at']) if row else None, 'updatedBy': row['updated_by'] if row else None}

    def _handling(self, run_id):
        with self._lock:
            row = self._cad.execute('SELECT * FROM admin_task_handling WHERE run_id=?', (run_id,)).fetchone()
        return {'status': row['status'] if row else 'unassigned', 'priority': row['priority'] if row else 'normal',
                'version': row['version'] if row else 0, 'updatedAt': moment(row['updated_at']) if row else None,
                'updatedBy': row['updated_by'] if row else None}

    def _target(self, kind, target_id):
        if not identifier(target_id):
            raise ValidationError('目标编号格式不正确')
        if kind == 'customer':
            with self.auth._lock:
                if self._platform.execute('SELECT 1 FROM users WHERE id=?', (target_id,)).fetchone() is None:
                    raise NotFoundError('客户不存在')
        else:
            self._task_row(target_id)

    def _mutate(self, actor_id, permission, action, target_id, values, reason, change):
        key = values.get('idempotencyKey')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', key):
            raise ValidationError('幂等编号格式不正确')
        digest = hashlib.sha256(json.dumps({'action': action, 'target': target_id, 'values': values}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Account revocation, write, audit and idempotent receipt cannot interleave
        # within this process; independent workers serialize on BEGIN IMMEDIATE.
        with self._lock, self.auth._lock:
            self.actor(actor_id, permission)
            self._cad.execute('BEGIN IMMEDIATE')
            try:
                old = self._cad.execute('SELECT request_hash,result_json FROM admin_operations_mutations WHERE actor_id=? AND request_key=?', (actor_id, key)).fetchone()
                if old:
                    if old['request_hash'] != digest:
                        raise ConflictError('同一幂等编号不能提交不同内容')
                    result = {**json.loads(old['result_json']), 'replayed': True}
                else:
                    result = change()
                    audit_id = 'ops_' + uuid4().hex
                    self._cad.execute('INSERT INTO admin_operations_audit (id,actor_id,action,target_id,reason,accepted,created_at) VALUES (?,?,?,?,?,?,?)',
                                      (audit_id, actor_id, action, target_id, reason, 1, now()))
                    result.update(auditId=audit_id, replayed=False)
                    self._cad.execute('INSERT INTO admin_operations_mutations VALUES (?,?,?,?)', (actor_id, key, digest, json.dumps(result, ensure_ascii=False)))
                self._cad.commit()
                return result
            except Exception:
                self._cad.rollback()
                raise

    def update_crm(self, actor_id, owner, values):
        self.actor(actor_id, 'admin:customer-manage')
        self._fields(values, ('company', 'contactName', 'phone', 'tags', 'internalNote', 'expectedVersion', 'idempotencyKey', 'reason'))
        clean = {key: self._text(values[key], label, maximum) for key, label, maximum in
                 [('company', '公司名称', 120), ('contactName', '联系人', 80), ('phone', '联系电话', 40), ('internalNote', '内部备注', 2000)]}
        tags = values['tags']
        if not isinstance(tags, list) or len(tags) > 12:
            raise ValidationError('标签最多12项')
        clean['tags'] = list(dict.fromkeys(self._text(tag, '标签', 24, 1) for tag in tags))
        version = values['expectedVersion']
        if type(version) is not int or not 0 <= version <= 1_000_000_000:
            raise ValidationError('资料版本不正确')
        reason = self._text(values['reason'], '修改原因', 500, 5)
        def change():
            self._target('customer', owner)
            if self._crm(owner)['version'] != version:
                raise ConflictError('客户资料已被其他管理员修改，请刷新后重新编辑')
            self._cad.execute('INSERT INTO admin_customer_crm VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET company=excluded.company,contact_name=excluded.contact_name,phone=excluded.phone,tags_json=excluded.tags_json,internal_note=excluded.internal_note,version=excluded.version,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                              (owner, clean['company'], clean['contactName'], clean['phone'], json.dumps(clean['tags'], ensure_ascii=False), clean['internalNote'], version + 1, now(), actor_id))
            return {'crm': self._crm(owner)}
        return self._mutate(actor_id, 'admin:customer-manage', 'customer.crm.updated', owner, values, reason, change)

    def update_handling(self, actor_id, run_id, values):
        self.actor(actor_id, 'admin:task-followup')
        self._fields(values, ('status', 'priority', 'expectedVersion', 'idempotencyKey', 'reason'))
        if not isinstance(values['status'], str) or values['status'] not in HANDLING_STATUSES or not isinstance(values['priority'], str) or values['priority'] not in PRIORITIES:
            raise ValidationError('人工处理状态或优先级不正确')
        version = values['expectedVersion']
        if type(version) is not int or not 0 <= version <= 1_000_000_000:
            raise ValidationError('处理记录版本不正确')
        reason = self._text(values['reason'], '处理原因', 500, 5)
        def change():
            self._target('task', run_id)
            if self._handling(run_id)['version'] != version:
                raise ConflictError('任务处理记录已被其他管理员修改，请刷新后重新编辑')
            self._cad.execute('INSERT INTO admin_task_handling VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,priority=excluded.priority,version=excluded.version,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                              (run_id, values['status'], values['priority'], version + 1, now(), actor_id))
            return {'handling': self._handling(run_id)}
        return self._mutate(actor_id, 'admin:task-followup', 'task.handling.updated', run_id, values, reason, change)

    def _notes(self, kind, target_id, *, limit=20, offset=0):
        page(limit, offset)
        with self._lock:
            total = self._cad.execute('SELECT count(*) FROM admin_operations_notes WHERE target_kind=? AND target_id=?', (kind, target_id)).fetchone()[0]
            rows = self._cad.execute('SELECT id,content,created_at,created_by FROM admin_operations_notes WHERE target_kind=? AND target_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (kind, target_id, limit, offset)).fetchall()
        return {'items': [{'id': row['id'], 'content': row['content'], 'createdAt': moment(row['created_at']), 'createdBy': row['created_by']} for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def notes(self, actor_id, kind, target_id, **filters):
        self.actor(actor_id, 'admin:customers' if kind == 'customer' else 'admin:tasks')
        self._target(kind, target_id)
        return self._notes(kind, target_id, **filters)

    def add_note(self, actor_id, kind, target_id, values):
        permission = 'admin:customer-manage' if kind == 'customer' else 'admin:task-followup'
        self.actor(actor_id, permission)
        self._fields(values, ('content', 'idempotencyKey'))
        content = self._text(values['content'], '跟进内容', 2000, 1)
        def change():
            self._target(kind, target_id)
            entry = {'id': 'note_' + uuid4().hex, 'content': content, 'createdAt': moment(now()), 'createdBy': actor_id}
            self._cad.execute('INSERT INTO admin_operations_notes VALUES (?,?,?,?,?,?)', (entry['id'], kind, target_id, content, entry['createdAt'], actor_id))
            return {'entry': entry}
        # Full internal content stays in the permission-gated note record, while
        # the common audit carries only a fixed indication that content exists.
        return self._mutate(actor_id, permission, kind + '.note.added', target_id, values, '已记录内部跟进内容', change)

    def _customer(self, row, financial=False):
        user_id = row['id']
        with self.auth._lock:
            roles = [value[0] for value in self._platform.execute('SELECT role FROM user_roles WHERE user_id=? ORDER BY role', (user_id,))]
            support_available = table(self._platform, 'support_tickets')
            tickets = self._platform.execute("SELECT count(*) FROM support_tickets WHERE owner_id=? AND status IN ('open','in_progress')", (user_id,)).fetchone()[0] if support_available else None
        with self._lock:
            tasks = self._cad.execute('SELECT count(*) FROM (SELECT run_id FROM cad_runs WHERE owner=? UNION SELECT run_id FROM cad_job_attempts WHERE owner=?)', (user_id,user_id)).fetchone()[0]
        return {'id': user_id, 'email': row['email'], 'displayName': row['display_name'], 'active': bool(row['active']), 'roles': roles,
                'createdAt': moment(row['created_at']), 'updatedAt': moment(row['updated_at']), 'taskCount': tasks,
                'openTicketCount': tickets, 'financial': self._wallet(user_id) if financial else None, 'crm': self._crm(user_id)}

    def _wallet(self, owner):
        db = self._business()
        with self._business_lock():
            if not table(db, 'billing_accounts'):
                return None
            row = db.execute('SELECT credit_units FROM billing_accounts WHERE owner_id=?', (owner,)).fetchone()
            due = db.execute('SELECT coalesce(sum(credit_units),0) FROM billing_dues WHERE owner_id=?', (owner,)).fetchone()[0] if table(db, 'billing_dues') else 0
            paid = db.execute('SELECT coalesce(sum(credit_units),0) FROM billing_due_payments WHERE owner_id=?', (owner,)).fetchone()[0] if table(db, 'billing_due_payments') else 0
            return {'balanceUnits': row[0] if row else 0, 'dueUnits': max(0, due-paid)}

    def customers(self, actor_id, *, q='', active=None, limit=20, offset=0):
        actor = self.actor(actor_id, 'admin:customers')
        page(limit, offset)
        q = query_text(q)
        if active is not None and type(active) is not bool:
            raise ValidationError('active须为布尔值')
        filters, params = [], []
        if q:
            with self._lock:
                crm_owners = [row[0] for row in self._cad.execute("SELECT owner_id FROM admin_customer_crm WHERE company LIKE ? ESCAPE '\\' OR contact_name LIKE ? ESCAPE '\\' OR phone LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value LIKE ? ESCAPE '\\')", [like(q)] * 4)]
            crm_filter = ' OR id IN (SELECT value FROM json_each(?))' if crm_owners else ''
            filters.append("(email LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\'" + crm_filter + ')')
            params += [like(q)] * 3
            if crm_owners:
                params.append(json.dumps(crm_owners))
        if active is not None:
            filters.append('active=?'); params.append(int(active))
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        with self.auth._lock:
            total = self._platform.execute('SELECT count(*) FROM users' + where, params).fetchone()[0]
            rows = self._platform.execute('SELECT id,email,display_name,active,created_at,updated_at FROM users' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
        financial = self._financial(actor)
        return {'items': [self._customer(row, financial) for row in rows], 'total': total, 'limit': limit, 'offset': offset, 'financialVisible': financial}

    def customer(self, actor_id, owner):
        actor = self.actor(actor_id, 'admin:customers')
        with self.auth._lock:
            row = self._platform.execute('SELECT id,email,display_name,active,created_at,updated_at FROM users WHERE id=?', (owner,)).fetchone()
            if row is None:
                raise NotFoundError('客户不存在')
            tickets = self._platform.execute('SELECT id,category,run_id,status,created_at,updated_at FROM support_tickets WHERE owner_id=? ORDER BY updated_at DESC,id DESC LIMIT 10', (owner,)).fetchall() if table(self._platform, 'support_tickets') else []
        visible = self._financial(actor)
        result = {'customer': self._customer(row, visible), 'financialVisible': visible, 'financial': None,
                  'crm': self._crm(owner), 'followups': self._notes('customer', owner),
                  'tasks': self._tasks(owner=owner, limit=10)['items'],
                  'tickets': [{'id': item['id'], 'number': item['id'], 'category': item['category'] if item['category'] in {'account','billing','modeling','other'} else 'other',
                               'runId': identifier(item['run_id']), 'status': identifier(item['status']), 'createdAt': moment(item['created_at']), 'updatedAt': moment(item['updated_at'])} for item in tickets]}
        if visible:
            db = self._business()
            with self._business_lock():
                ledger = db.execute('SELECT id,delta_units,balance_after,kind,reference_id,created_at FROM billing_ledger WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT 10', (owner,)).fetchall() if table(db, 'billing_ledger') else []
                orders = db.execute('SELECT id,amount_fen,credit_units,currency,status,created_at,updated_at FROM billing_orders WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT 10', (owner,)).fetchall() if table(db, 'billing_orders') else []
            result['financial'] = {'wallet': self._wallet(owner),
                'ledger': [{'id': item['id'], 'deltaUnits': item['delta_units'], 'balanceAfter': item['balance_after'], 'kind': identifier(item['kind']), 'referenceId': identifier(item['reference_id']), 'createdAt': moment(item['created_at'])} for item in ledger],
                'orders': [{'id': item['id'], 'amountFen': item['amount_fen'], 'creditUnits': item['credit_units'], 'currency': item['currency'] if item['currency'] in {'CNY','USD'} else None, 'status': identifier(item['status']), 'createdAt': moment(item['created_at']), 'updatedAt': moment(item['updated_at'])} for item in orders],
                'usage': self._usage(owner=owner, financial=True)}
            with self._lock:
                historical = self._cad.execute('SELECT count(DISTINCT r.run_id) FROM cad_runs r WHERE r.owner=? AND NOT EXISTS (SELECT 1 FROM cad_job_attempts a WHERE a.run_id=r.run_id)', (owner,)).fetchone()[0]
            if historical and result['financial']['usage']['calls'] == 0:
                result['financial']['usage'] = {key: None for key in result['financial']['usage']}
            result['financial']['usage'].update(scope='recorded_calls', historicalTasksWithoutJournal=historical)
        self._record_read(actor_id, 'customer.detail.read', owner, visible)
        return result

    _TASK_SQL = '''WITH keys AS (SELECT run_id FROM cad_runs UNION SELECT run_id FROM cad_job_attempts)
      SELECT k.run_id,coalesce(a.owner,r.owner) owner,a.job_id,a.request_id,a.status registry_status,a.cancel_requested,
      r.revision,json_extract(r.payload,'$.status') run_status,
      coalesce(a.created_at,json_extract(r.payload,'$.createdAt')) created_at,
      coalesce(a.updated_at,json_extract(r.payload,'$.updatedAt')) updated_at,
      json_extract(r.payload,'$.completedAt') completed_at,
      CASE WHEN json_type(r.payload,'$.elapsedSeconds') IN ('integer','real') THEN json_extract(r.payload,'$.elapsedSeconds') END reported_elapsed,
      json_extract(r.payload,'$.progress.stage') stage, TRACE_STAGE trace_stage,
      coalesce(json_extract(r.payload,'$.progress.errorCode'),json_extract(r.payload,'$.errorCode'),json_extract(r.payload,'$.errors[0].code')) error_code,
      coalesce(json_extract(r.payload,'$.provider.lastErrorCode'),json_extract(r.payload,'$.provider.lastErrorKind')) provider_error,
      coalesce(json_extract(r.payload,'$.progress.provider.lastErrorCode'),json_extract(r.payload,'$.progress.provider.lastErrorKind')) progress_provider_error,
      json_extract(r.payload,'$.providerMetrics.lastErrorKind') metrics_error_kind,
      json_extract(r.payload,'$.providerMetrics.lastErrorCode') metrics_error_code,
      coalesce(json_extract(r.payload,'$.providerMetrics.history[#-1].errorKind'),json_extract(r.payload,'$.providerMetrics.history[#-1].lastErrorKind'),json_extract(r.payload,'$.providerMetrics.history[#-1].errorCode'),json_extract(r.payload,'$.providerMetrics.history[#-1].code'),json_extract(r.payload,'$.providerMetrics.history[#-1].kind'),json_extract(r.payload,'$.providerMetrics.history[#-1].lastErrorCode')) history_error,
      json_extract(r.payload,'$.trace[#-1].code') trace_error,
      CASE WHEN json_type(r.payload,'$.providerMetrics.retryCount')='integer' THEN json_extract(r.payload,'$.providerMetrics.retryCount') END metrics_retries,
      CASE WHEN json_type(r.payload,'$.provider.retryCount')='integer' THEN json_extract(r.payload,'$.provider.retryCount') END provider_retries,
      CASE WHEN json_type(r.payload,'$.providerMetrics.attempts')='integer' THEN json_extract(r.payload,'$.providerMetrics.attempts') END metrics_attempts,
      CASE WHEN json_type(r.payload,'$.provider.attempts')='integer' THEN json_extract(r.payload,'$.provider.attempts') END provider_attempts,
      json_extract(r.payload,'$.provider.name') provider,json_extract(r.payload,'$.provider.mode') provider_mode,json_extract(r.payload,'$.provider.model') model,
      json_array_length(json_extract(r.payload,'$.trace')) trace_count,
      CASE WHEN json_extract(r.payload,'$.status')='running' OR r.run_id IS NULL THEN CASE WHEN a.cancel_requested=1 THEN 'cancel_requested' ELSE coalesce(a.status,'running') END ELSE json_extract(r.payload,'$.status') END status
      FROM keys k LEFT JOIN cad_job_attempts a ON a.run_id=k.run_id
      LEFT JOIN cad_runs r ON r.run_id=k.run_id AND r.revision=(SELECT max(revision) FROM cad_runs WHERE run_id=k.run_id)'''.replace('TRACE_STAGE',_TRACE_STAGE)

    def _task(self, row):
        with self.auth._lock:
            user = self._platform.execute('SELECT id,email,display_name FROM users WHERE id=?', (row['owner'],)).fetchone()
        created, completed = moment(row['created_at']), moment(row['completed_at'])
        elapsed = None
        if created and (completed or row['status'] in ACTIVE):
            elapsed = max(0, round((datetime.fromisoformat(completed) if completed else datetime.now(timezone.utc)).timestamp() - datetime.fromisoformat(created).timestamp(), 2))
        if elapsed is None:
            elapsed = elapsed_value(row['reported_elapsed'])
        status = row['status'] if row['status'] in STATUSES else 'unknown'
        error_candidates = [row[key] for key in ('error_code','provider_error','progress_provider_error','metrics_error_kind','metrics_error_code','history_error','trace_error')]
        error = first_known(error_candidates,ERRORS) or ('unknown_error' if any(error_candidates) else None)
        return {'runId': identifier(row['run_id']), 'jobId': identifier(row['job_id']) or identifier('job_' + row['run_id'][4:]), 'attemptId': identifier(row['run_id']),
                'requestId': identifier(row['request_id']), 'ownerId': row['owner'], 'customer': {'id': user['id'], 'email': user['email'], 'displayName': user['display_name']} if user else None,
                'status': status, 'stage': first_known([row['stage'],row['trace_stage']],STAGES),
                'errorCode': error,
                'revision': row['revision'], 'createdAt': created, 'updatedAt': moment(row['updated_at']), 'completedAt': completed,
                'elapsedSeconds': elapsed, 'canCancel': status in ACTIVE and row['registry_status'] in ACTIVE,
                'handling': self._handling(row['run_id'])}

    def _tasks(self, *, q='', status=None, owner=None, limit=20, offset=0, job=None, handling_status=None, priority=None):
        page(limit, offset); q = query_text(q)
        if status is not None and status not in STATUSES:
            raise ValidationError('任务状态不正确')
        if handling_status is not None and handling_status not in HANDLING_STATUSES:
            raise ValidationError('人工处理状态不正确')
        if priority is not None and priority not in PRIORITIES:
            raise ValidationError('优先级不正确')
        conditions, values = [], []
        if handling_status is not None:
            conditions.append("coalesce((SELECT h.status FROM admin_task_handling h WHERE h.run_id=task.run_id),'unassigned')=?"); values.append(handling_status)
        if priority is not None:
            conditions.append("coalesce((SELECT h.priority FROM admin_task_handling h WHERE h.run_id=task.run_id),'normal')=?"); values.append(priority)
        if owner:
            conditions.append('owner=?'); values.append(owner)
        if job:
            conditions.append('job_id=?'); values.append(job)
        if status:
            conditions.append('status=?'); values.append(status)
        if q:
            with self.auth._lock:
                owners = [row[0] for row in self._platform.execute("SELECT id FROM users WHERE email LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\'", (like(q), like(q)))]
            parts = ["run_id LIKE ? ESCAPE '\\'", "job_id LIKE ? ESCAPE '\\'", "request_id LIKE ? ESCAPE '\\'"]
            values += [like(q)] * 3
            if owners:
                parts.append('owner IN (SELECT value FROM json_each(?))'); values.append(json.dumps(owners))
            conditions.append('(' + ' OR '.join(parts) + ')')
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        query = 'SELECT * FROM (' + self._TASK_SQL + ') task' + where
        with self._lock:
            total = self._cad.execute('SELECT count(*) FROM (' + query + ')', values).fetchone()[0]
            rows = self._cad.execute(query + ' ORDER BY created_at DESC,run_id DESC LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
        return {'items': [self._task(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def tasks(self, actor_id, **filters):
        self.actor(actor_id, 'admin:tasks')
        return self._tasks(**filters)

    def _task_row(self, run_id):
        with self._lock:
            row = self._cad.execute('SELECT * FROM (' + self._TASK_SQL + ') WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise NotFoundError('任务不存在')
        return row

    def _usage(self, *, owner=None, attempt=None, financial=False):
        calls = {}
        db = self._business()
        fields = ['inputTokens','cachedInputTokens','outputTokens','reasoningOutputTokens','costMicroUsd']
        where = ' WHERE owner_id=?' if owner else ''
        params = [owner] if owner else []
        if attempt:
            where += (' AND ' if where else ' WHERE ') + "json_extract(data_json,'$.attemptId')=?"; params.append(attempt)
        with self._business_lock():
            if table(db, 'billing_usage'):
                columns = ','.join(f"json_extract(data_json,'$.{field}')" for field in fields)
                for row in db.execute('SELECT call_id,' + columns + ' FROM billing_usage' + where, params):
                    calls[row[0]] = [number(value) for value in row[1:]]
        where = ' WHERE owner=?' if owner else ''
        params = [owner] if owner else []
        if attempt:
            where += (' AND ' if where else ' WHERE ') + 'attempt_id=?'; params.append(attempt)
        with self._lock:
            columns = ','.join(f"json_extract(usage_json,'$.{field}')" for field in ['input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','cost_micro_usd'])
            for row in self._cad.execute('SELECT call_id,' + columns + ' FROM cad_provider_calls' + where, params):
                previous = calls.get(row[0], [None] * 5)
                calls[row[0]] = [number(value) if number(value) is not None else previous[index] for index, value in enumerate(row[1:])]
        result = {'calls': len(calls), 'unknownUsageCalls': sum(values[0] is None or values[2] is None for values in calls.values())}
        for index, field in enumerate(fields):
            if field == 'costMicroUsd' and not financial:
                continue
            result[field] = None if any(values[index] is None for values in calls.values()) else sum(values[index] for values in calls.values())
        if financial:
            result['unknownCostCalls'] = sum(values[4] is None for values in calls.values())
        return result

    def _settlements(self, owner, job):
        db = self._business()
        with self._business_lock():
            if not table(db, 'billing_policy_receipts'):
                return []
            columns = ','.join(f"json_extract(r.result_json,'$.{key}') {key}" for key in ['status','expectedCreditUnits','chargedUnits','deferredUnits','platformAbsorbedUnits'])
            rows = db.execute('SELECT a.attempt_id,a.job_id,a.version_id,s.terminal_status,s.created_at,' + columns +
                ' FROM billing_policy_attempts a JOIN billing_policy_seals s ON s.attempt_id=a.attempt_id LEFT JOIN billing_policy_receipts r ON r.attempt_id=a.attempt_id WHERE a.owner_id=? AND a.job_id=? ORDER BY s.created_at DESC,a.attempt_id DESC LIMIT 20', (owner, job)).fetchall()
        return [{'attemptId': identifier(row['attempt_id']), 'jobId': identifier(row['job_id']), 'versionId': identifier(row['version_id']),
                 'terminalStatus': row['terminal_status'] if row['terminal_status'] in STATUSES else 'unknown', 'status': row['status'] if row['status'] in {'charged','not_charged','settled','pending_settlement','deferred_due','cap_at_balance'} else 'pending_settlement',
                 **{key: number(row[key]) for key in ['expectedCreditUnits','chargedUnits','deferredUnits','platformAbsorbedUnits']}, 'createdAt': moment(row['created_at'])} for row in rows]

    def task(self, actor_id, run_id):
        actor = self.actor(actor_id, 'admin:tasks')
        row = self._task_row(run_id); task = self._task(row)
        visible = self._financial(actor)
        usage = self._usage(owner=row['owner'], attempt=run_id, financial=visible)
        # Before the durable call journal existed, absence of rows does not
        # establish zero supplier usage for a historical task.
        if row['registry_status'] is None and usage['calls'] == 0:
            usage = {key: None for key in usage}
        error = task['errorCode']
        category = 'timeout' if error and ('timeout' in error or error in {'time_limit','http_408','http_504'}) else 'cancelled' if error == 'cancelled' else 'access' if error in {'authentication','permission_denied'} else 'provider' if error in {'transport','provider_error','rate_limit','quota_exceeded','provider_failure','upstream_error','invalid_response','invalid_json','invalid_stream','incomplete','empty_response','connection_error','connection_reset','network_error'} or (error and error.startswith('http_')) else 'geometry' if error in {'invalid_geometry','invalid_plan','inspection_failed','execution_failed','geometry_failed'} else 'unknown' if error else None
        self._record_read(actor_id, 'task.detail.read', run_id, visible)
        return {'task': task, 'handling': task['handling'], 'notes': self._notes('task', run_id),
                'attempts': self._tasks(owner=row['owner'], job=row['job_id'], limit=20)['items'] if row['job_id'] else [task],
                'usage': {**usage, 'scope': 'attempt'}, 'settlements': self._settlements(row['owner'], task['jobId']) if visible else None,
                'financialVisible': visible, 'redacted': True, 'diagnostics': {'phase': task['stage'], 'errorCode': error, 'errorCategory': category,
                    'provider': first_known([row['provider'],row['provider_mode']],PROVIDERS), 'model': model_name(row['model']),
                    'retryCount': next((bounded_count(row[key]) for key in ('metrics_retries','provider_retries') if bounded_count(row[key]) is not None),None),
                    'providerAttempts': next((bounded_count(row[key]) for key in ('metrics_attempts','provider_attempts') if bounded_count(row[key]) is not None),None),
                    'traceEvents': number(row['trace_count'])}}

    def _record_read(self, actor_id, action, target_id, financial):
        with self._lock, self._cad:
            self._cad.execute('INSERT INTO admin_operations_audit (id,actor_id,action,target_id,reason,accepted,created_at,financial_visible) VALUES (?,?,?,?,?,?,?,?)',
                              ('ops_' + uuid4().hex, actor_id, action, target_id, '', 1, now(), int(financial)))

    def cancel(self, actor_id, run_id, values):
        self.actor(actor_id, 'admin:task-manage')
        if not isinstance(values, dict) or set(values) != {'reason','idempotencyKey'}:
            raise ValidationError('停止任务只接受reason和idempotencyKey')
        reason, key = values['reason'], values['idempotencyKey']
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise ValidationError('停止原因须为5至500字符')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', key):
            raise ValidationError('幂等编号格式不正确')
        reason = reason.strip()
        digest = hashlib.sha256(json.dumps({'runId': run_id, 'reason': reason}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            self._cad.execute('BEGIN IMMEDIATE')
            try:
                old = self._cad.execute('SELECT * FROM admin_operations_idempotency WHERE actor_id=? AND request_key=?', (actor_id, key)).fetchone()
                if old:
                    if old['request_hash'] != digest:
                        raise ConflictError('同一幂等编号不能提交不同的停止请求')
                    accepted, audit_id, replayed = bool(old['accepted']), old['audit_id'], True
                else:
                    row = self._task_row(run_id)
                    accepted = row['status'] in ACTIVE and row['registry_status'] in ACTIVE
                    if row['status'] in ACTIVE and row['registry_status'] not in ACTIVE:
                        raise ConflictError('此历史任务不支持在线停止，请核对运行状态')
                    if accepted:
                        # Preserve queued capacity until its dispatcher acknowledges the stop.
                        self._cad.execute("UPDATE cad_job_attempts SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'queued' ELSE 'cancel_requested' END,updated_at=? WHERE run_id=? AND owner=? AND status IN ('queued','running','cancel_requested')", (now(), run_id, row['owner']))
                    audit_id, replayed = 'ops_' + uuid4().hex, False
                    self._cad.execute('INSERT INTO admin_operations_audit (id,actor_id,action,target_id,reason,accepted,created_at) VALUES (?,?,?,?,?,?,?)', (audit_id, actor_id, 'task.cancel_requested' if accepted else 'task.cancel_noop', run_id, reason, int(accepted), now()))
                    self._cad.execute('INSERT INTO admin_operations_idempotency VALUES (?,?,?,?,?,?)', (actor_id,key,digest,audit_id,run_id,int(accepted)))
                self._cad.commit()
            except Exception:
                self._cad.rollback(); raise
        task = self._task(self._task_row(run_id))
        return {'task': task, 'accepted': accepted, 'replayed': replayed, 'auditId': audit_id,
                'message': '停止请求已记录；等待工作进程停止，期间不会假报已取消。' if task['status'] in ACTIVE and accepted else '任务已经结束，当前状态已保留。'}

    def _task_counts(self):
        with self._lock:
            rows = self._cad.execute('SELECT status,count(*) n FROM (' + self._TASK_SQL + ') GROUP BY status').fetchall()
        counts = {status: 0 for status in sorted(STATUSES)}
        for row in rows:
            status = row['status'] if row['status'] in STATUSES else 'unknown'
            counts[status] = counts.get(status, 0) + row['n']
        return {'total': sum(counts.values()), 'byStatus': counts, 'queued': counts['queued'], 'running': counts['running'], 'cancelRequested': counts['cancel_requested']}

    def _support_counts(self):
        with self.auth._lock:
            if not table(self._platform, 'support_tickets'):
                return {'available': False, 'total': None, 'open': None, 'waitingCustomer': None, 'resolved': None, 'closed': None}
            row = self._platform.execute("SELECT count(*),coalesce(sum(status IN ('open','in_progress')),0),coalesce(sum(status='waiting_customer'),0),coalesce(sum(status='resolved'),0),coalesce(sum(status='closed'),0) FROM support_tickets").fetchone()
        return {'available': True, 'total': row[0], 'open': row[1], 'waitingCustomer': row[2], 'resolved': row[3], 'closed': row[4]}

    def _billing_counts(self, financial=False):
        db = self._business()
        with self._business_lock():
            available = table(db, 'billing_accounts')
            pending = db.execute('SELECT count(*) FROM billing_policy_seals s LEFT JOIN billing_policy_receipts r ON r.attempt_id=s.attempt_id WHERE r.attempt_id IS NULL').fetchone()[0] if table(db, 'billing_policy_receipts') else None
            result = {'available': available, 'chargingEnabled': bool(self.policy.status()['enabled']) if self.policy else None, 'pendingSettlements': pending}
            if financial and available:
                balance = db.execute('SELECT coalesce(sum(credit_units),0) FROM billing_accounts').fetchone()[0]
                due = db.execute('SELECT coalesce(sum(credit_units),0) FROM billing_dues').fetchone()[0] - db.execute('SELECT coalesce(sum(credit_units),0) FROM billing_due_payments').fetchone()[0] if table(db, 'billing_dues') else 0
                orders = db.execute("SELECT count(*),coalesce(sum(amount_fen),0) FROM billing_orders WHERE status IN ('paid','refunded')").fetchone() if table(db, 'billing_orders') else (0,0)
                refunds = db.execute("SELECT coalesce(sum(amount_fen),0) FROM billing_refunds WHERE status='succeeded'").fetchone()[0] if table(db, 'billing_refunds') else 0
                result.update(balanceUnits=balance, dueUnits=max(0,due), paidOrders=orders[0], revenueFen=orders[1]-refunds)
        # Aggregate unknown usage across the durable call journal and delivered
        # billing records; a call present in both is counted only once.
        usage = self._usage()
        result.update(unknownUsageCalls=usage['unknownUsageCalls'], usageCalls=usage['calls'])
        return result

    @staticmethod
    def _alerts(tasks, support, billing, disk=None):
        result = []
        checks = [('queued_tasks','info',tasks['queued'],'有任务正在排队。'),
                  ('cancellation_pending','warning',tasks['cancelRequested'],'任务正在等待工作进程确认停止。'),
                  ('pending_settlements','warning',billing['pendingSettlements'],'已结束调用尚待完成核账。'),
                  ('unknown_usage','warning',billing['unknownUsageCalls'],'存在用量尚未确认的实际调用。'),
                  ('open_support','info',support['open'],'存在尚未关闭的支持工单。')]
        for code, severity, count, message in checks:
            if count:
                result.append({'code':code,'severity':severity,'count':count,'message':message})
        if disk and disk['available'] and disk['totalBytes'] > 0 and disk['freeBytes'] / disk['totalBytes'] < .1:
            result.append({'code':'low_disk','severity':'warning','count':None,'message':'CAD 工作区所在文件系统剩余空间低于 10%。'})
        result.append({'code':'monitoring_unconfigured','severity':'info','count':None,'message':'尚未配置外部持续监控和告警通知。'})
        return result

    def _trend(self, days, financial):
        if type(days) is not int or days not in {7, 30, 90}:
            raise ValidationError('趋势范围仅支持7、30或90天')
        today = datetime.fromisoformat(now().replace('Z', '+00:00')).astimezone(BUSINESS_ZONE).date()
        start = today - timedelta(days=days-1)
        end = today + timedelta(days=1)
        low = datetime.combine(start, datetime.min.time(), tzinfo=BUSINESS_ZONE).isoformat()
        high = datetime.combine(end, datetime.min.time(), tzinfo=BUSINESS_ZONE).isoformat()
        points = {(start+timedelta(days=index)).isoformat(): {'date': (start+timedelta(days=index)).isoformat(),
                  'tasks': 0, 'success': 0, 'failed': 0, 'reviewRequired': 0, 'needsInput': 0,
                  'historicalTasksWithoutJournal': 0} for index in range(days)}
        def day(value):
            parsed = moment(value)
            return datetime.fromisoformat(parsed).astimezone(BUSINESS_ZONE).date().isoformat() if parsed else None
        undated = 0
        with self._lock:
            # Count one current revision per task, grouped by its creation date.
            # These are cohort outcomes as of now, not historical transition events.
            rows = self._cad.execute('SELECT run_id,created_at,status,registry_status FROM (' + self._TASK_SQL + ')').fetchall()
        for row in rows:
            date = day(row['created_at'])
            if date is None:
                undated += 1
            if date not in points:
                continue
            item = points[date]
            item['tasks'] += 1
            item['success'] += int(row['status'] == 'ready')
            item['failed'] += int(row['status'] == 'failed')
            item['reviewRequired'] += int(row['status'] == 'review_required')
            item['needsInput'] += int(row['status'] == 'needs_input')
            item['historicalTasksWithoutJournal'] += int(row['registry_status'] is None)
        fields = ['inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'costMicroUsd']
        calls = {}
        db = self._business()
        with self._business_lock():
            if table(db, 'billing_usage'):
                columns = ','.join(f"json_extract(data_json,'$.{field}')" for field in fields)
                for row in db.execute('SELECT call_id,created_at,' + columns + ' FROM billing_usage WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)', (low, high)):
                    calls[row[0]] = (row[1], [number(value) for value in row[2:]])
        billing_ids = list(calls)
        columns = ','.join(f"json_extract(usage_json,'$.{field}')" for field in ['input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','cost_micro_usd'])
        with self._lock:
            rows = self._cad.execute('SELECT call_id,started_at,' + columns + ' FROM cad_provider_calls WHERE julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?)', (low, high)).fetchall()
            # A late billing receipt must keep the original call date, including
            # a call from before the selected range. Never count it twice.
            for offset in range(0, len(billing_ids), 400):
                batch = billing_ids[offset:offset+400]
                rows.extend(self._cad.execute('SELECT call_id,started_at,' + columns + ' FROM cad_provider_calls WHERE call_id IN (' + ','.join('?' for _ in batch) + ')', batch).fetchall())
        for row in rows:
            previous = calls.get(row[0], (None, [None]*5))[1]
            calls[row[0]] = (row[1], [number(value) if number(value) is not None else previous[index] for index, value in enumerate(row[2:])])
        by_day = {date: [] for date in points}
        for date, values in calls.values():
            date = day(date)
            if date in by_day:
                by_day[date].append(values)
        for date, point in points.items():
            values = by_day[date]
            usage = {'calls': len(values), 'unknownUsageCalls': sum(item[0] is None or item[2] is None for item in values)}
            for index, field in enumerate(fields):
                if field == 'costMicroUsd' and not financial:
                    continue
                usage[field] = sum(item[index] for item in values) if values and all(item[index] is not None for item in values) else None
            if financial:
                usage['unknownCostCalls'] = sum(item[4] is None for item in values)
            point['usage'] = usage
        return {'days': days, 'timeZone': 'Asia/Shanghai', 'dateBasis': 'task_created_at_and_call_created_at',
                'statusBasis': 'latest_task_status', 'successStatuses': ['ready'], 'usageScope': 'recorded_calls',
                'points': list(points.values()), 'undatedTasks': undated,
                'totals': {key: sum(item[key] for item in points.values()) for key in ['tasks','success','failed','reviewRequired','needsInput','historicalTasksWithoutJournal']}}

    def overview(self, actor_id, *, days=30):
        actor = self.actor(actor_id, 'admin:overview')
        visible = self._financial(actor)
        trend = self._trend(days, visible)
        with self.auth._lock:
            row = self._platform.execute('SELECT count(*),coalesce(sum(active=1),0),coalesce(sum(active=0),0) FROM users').fetchone()
            staff = self._platform.execute("SELECT count(DISTINCT user_id) FROM user_roles WHERE role IN ('admin','ops','finance','support','auditor')").fetchone()[0]
        tasks, support, billing = self._task_counts(), self._support_counts(), self._billing_counts(visible)
        financial = {key: billing.get(key) for key in ('balanceUnits','dueUnits','paidOrders','revenueFen','pendingSettlements','usageCalls')} if visible and billing['available'] else None
        host = read_operations_report()
        alerts = self._alerts(tasks,support,billing)
        if host['monitoring']['configured']:
            alerts = [item for item in alerts if item['code'] != 'monitoring_unconfigured']
        return {'generatedAt':now(),'customers':{'total':row[0],'active':row[1],'inactive':row[2], 'businessCustomers': row[0]-staff, 'staffAccounts': staff},'tasks':tasks,'support':support,
                'trend': trend,
                'financialVisible':visible,'financial':financial,'alerts':alerts + host['alerts'],
                'monitoring':host['monitoring']}

    def system(self, actor_id):
        self.actor(actor_id, 'admin:system')
        tasks, support, billing = self._task_counts(), self._support_counts(), self._billing_counts()
        try:
            usage = shutil.disk_usage(self.store.root)
            disk = {'available':True,'freeBytes':usage.free,'totalBytes':usage.total,'scope':'cad_workspace_filesystem',
                    'message':'仅表示 CAD 工作区所在文件系统；容器环境下不代表宿主机全部磁盘。'}
        except OSError:
            disk = {'available':False,'freeBytes':None,'totalBytes':None,'scope':'cad_workspace_filesystem','message':'当前无法读取 CAD 工作区文件系统容量。'}
        databases = {}
        for key, connection, lock in [('platformReadable',self._platform,self.auth._lock),('cadReadable',self._cad,self._lock)]:
            try:
                with lock:
                    connection.execute('SELECT 1').fetchone()
                databases[key] = True
            except sqlite3.Error:
                databases[key] = False
        with self._lock:
            running_operations = self._cad.execute("SELECT count(*) FROM cad_billing_operations WHERE status='running'").fetchone()[0]
        provider = 'codex' if os.getenv('JOYNIU_CAD_PROVIDER','relay').strip().lower() == 'codex' else 'relay'
        configured_model = os.getenv('JOYNIU_CAD_CODEX_MODEL','gpt-6-astra') if provider == 'codex' else (os.getenv('JOYNIU_LLM_MODEL') or os.getenv('JOYNIU_AI_MODEL') or 'gpt-5.6-sol')
        effort = os.getenv('JOYNIU_LLM_REASONING_EFFORT') or os.getenv('JOYNIU_AI_REASONING_EFFORT') or 'high'
        effort = effort.strip().lower()
        if effort not in {'low','medium','high','xhigh','max','ultra'}:
            effort = 'high'
        host = read_operations_report()
        alerts = self._alerts(tasks,support,billing,disk)
        if host['monitoring']['configured']:
            alerts = [item for item in alerts if item['code'] != 'monitoring_unconfigured']
        local_backup = host['checks'].get('backup', {})
        backup_available = host['monitoring']['available'] and bool(local_backup.get('lastVerifiedAt')) and not local_backup.get('verificationFailed')
        backup = {'available':backup_available, 'stale':host['monitoring']['stale'], **local_backup,
                  'message':'已读取本机备份校验报告；异地副本尚未接入。' if backup_available else '尚无可读取的近期本机备份校验报告；不据此推断手工备份是否存在。'}
        return {'generatedAt':now(),'queue':{'available':True,'queued':tasks['queued'],'running':tasks['running'],'cancelRequested':tasks['cancelRequested'],
                    'runningOperations':running_operations,'capacity':{'maxConcurrent':self.registry.max_concurrent,'maxQueued':self.registry.max_queued,'maxPerOwner':self.registry.max_per_owner}},
                'billing':{key:billing[key] for key in ('available','chargingEnabled','pendingSettlements','unknownUsageCalls')},'support':support,
                'alerts':alerts + host['alerts'], 'monitoring':host['monitoring'], 'hostOperations':host['checks'],
                'externalNotifications':{'available':False,'message':'尚未配置外部告警通知；可在本后台查看宿主告警。'},'backup':backup,
                'offsiteBackup':{'available':False,'message':'尚未接入并验证异地备份目标。'},
                'configuration':{'provider':provider,'model':model_name(configured_model.strip()),'reasoningEffort':effort,'source':'runtime_configuration','externalProbe':False},
                'cadKernel':kernel_status(),'databases':databases,'disk':disk}

    def audit(self, actor_id, *, source=None, actor_filter=None, limit=20, offset=0):
        actor = self.actor(actor_id, 'admin:audit'); page(limit,offset)
        choices = {'operations','accounts','billing','support'}
        if source is not None and source not in choices:
            raise ValidationError('审计来源不正确')
        if source == 'billing' and not self._financial(actor):
            raise AuthorizationError('读取账单审计需要财务读取权限')
        if not self._financial(actor):
            choices.remove('billing')
        if source:
            choices = {source}
        actor_filter = query_text(actor_filter)
        specifications = [
            ('operations',self._cad,'admin_operations_audit','target_id',"length(reason)>0"),
            ('accounts',self._platform,'auth_audit','target_id',"coalesce(json_extract(details_json,'$.reason'),'')!=''"),
            ('billing',self._business(),'billing_audit','resource_id',"coalesce(json_extract(details_json,'$.reason'),'')!=''"),
            ('support',self._platform,'support_audit','ticket_id',"coalesce(json_extract(details_json,'$.reason'),'')!='' OR coalesce(json_extract(details_json,'$.note'),'')!=''"),
        ]
        streams, total = [], 0
        with ExitStack() as stack:
            for lock in (self._lock,self.auth._lock,self._business_lock()):
                stack.enter_context(lock)
            for name, db, table_name, target, reason in specifications:
                if name not in choices or not table(db,table_name):
                    continue
                where, params = (' WHERE actor_id=?',(actor_filter,)) if actor_filter else ('',())
                total += db.execute('SELECT count(*) FROM ' + table_name + where,params).fetchone()[0]
                cursor = db.execute(f'SELECT id,actor_id,action,{target} target_id,created_at,({reason}) reason_recorded FROM {table_name}' + where + ' ORDER BY created_at DESC,id DESC', params)
                def rows(cursor=cursor, name=name):
                    for row in cursor:
                        yield {'id':identifier(row['id']),'source':name,'actorId':identifier(row['actor_id']),'action':identifier(row['action']),
                               'targetId':identifier(row['target_id']),'createdAt':moment(row['created_at']),'reasonRecorded':bool(row['reason_recorded'])}
                streams.append(rows())
            merged = heapq.merge(*streams,key=lambda row:(row['createdAt'] or '',row['id'] or ''),reverse=True)
            items = list(itertools.islice(merged,offset,offset+limit))
        return {'items':items,'total':total,'limit':limit,'offset':offset}
