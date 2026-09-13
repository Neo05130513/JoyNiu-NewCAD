"""Business staff permissions are enforced on direct HTTP calls and services."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing_api import create_billing_router
from app.billing_policy_api import create_billing_policy_router
from app.commercial_terms_api import create_commercial_terms_router
from app.platform import AuthorizationError, permissions_for_roles
from app.platform_api import build_platform_services, create_platform_router
from app.support_api import create_support_router

ROLES = ('admin', 'ops', 'finance', 'support', 'auditor', 'designer', 'reviewer', 'manufacturing', 'viewer')

@pytest.fixture(scope='module')
def system(tmp_path_factory):
    services = build_platform_services(tmp_path_factory.mktemp('rbac') / 'platform.sqlite3', auth_secret='rbac-test-only-secret-for-local-integration')
    users = {role: services.auth.create_user(f'{role}@example.test', 'test-only-long-password', role, roles=[role]) for role in ROLES}
    app = FastAPI()
    billing_router = create_billing_router(services)
    support_router = create_support_router(services)
    policy_router = create_billing_policy_router(services, billing=billing_router.billing_service)
    terms_router = create_commercial_terms_router(services)
    for router in (create_platform_router(services), billing_router, support_router, policy_router, terms_router):
        app.include_router(router, prefix='/api/v1')
    headers = {role: {'Authorization': 'Bearer '+services.auth.issue_token(user).token} for role, user in users.items()}
    with TestClient(app) as client:
        yield client, services, users, headers, billing_router.billing_service, support_router.support_service, policy_router.policy_service
    billing_router.billing_service.close()
    services.close()


def test_engineering_roles_never_gain_business_permissions():
    for role in ('designer', 'reviewer', 'manufacturing', 'viewer'):
        assert not any(permission.startswith(('admin:', 'billing:', 'support:', 'terms:', 'user:')) for permission in permissions_for_roles([role]))
    assert permissions_for_roles(['admin']) == {'*'}
    assert 'billing:policy' not in permissions_for_roles(['finance'])
    assert 'billing:adjust' not in permissions_for_roles(['auditor'])
    assert 'admin:task-manage' in permissions_for_roles(['ops'])
    assert 'billing:read' not in permissions_for_roles(['support'])


def test_direct_read_routes_follow_matrix(system):
    client, _, _, headers, *_ = system
    financial_reads = ['/billing/admin/'+suffix for suffix in ('packages', 'orders', 'refund-requests', 'invoice-requests', 'dashboard', 'usage', 'audit', 'settlements')]
    routes = [(path, {'admin','finance','auditor'}) for path in financial_reads]
    routes += [('/support/admin/tickets', {'admin','support'}), ('/billing/admin/policy', {'admin'}), ('/commercial/admin/terms/history', {'admin'}), ('/auth/users', {'admin'})]
    for path, allowed in routes:
        assert client.get('/api/v1'+path).status_code == 401, path
        for role in ROLES:
            response = client.get('/api/v1'+path, headers=headers[role])
            assert response.status_code == (200 if role in allowed else 403), (path,role,response.text)
            if response.status_code == 200 and path != '/auth/users':
                assert response.headers['cache-control'] == 'no-store'


def test_readonly_and_wrong_staff_cannot_write_financial_or_policy_actions(system):
    client, _, users, headers, billing, *_ = system
    # Body validity / record existence must never be used to bypass the role gate.
    mutations = [
        ('POST','/billing/admin/packages',{}, {'admin'}),
        ('PATCH','/billing/admin/packages/missing',{}, {'admin'}),
        ('POST','/billing/admin/adjustments',{}, {'admin','finance'}),
        ('PATCH','/billing/admin/requests/missing',{}, {'admin','finance'}),
        ('POST','/billing/admin/requests/missing/refund',{}, {'admin','finance'}),
        ('POST','/billing/admin/requests/missing/refund/reconcile',{}, {'admin','finance'}),
        ('POST','/billing/admin/policy/versions',{}, {'admin'}),
        ('PATCH','/billing/admin/policy',{}, {'admin'}),
        ('POST','/commercial/admin/terms',{}, {'admin'}),
        ('PATCH',f'/auth/users/{users["designer"].id}/roles',{'roles':['admin']}, {'admin'}),
    ]
    for method,path,payload,allowed in mutations:
        for role in set(ROLES)-allowed:
            response=client.request(method,'/api/v1'+path,json=payload,headers=headers[role])
            assert response.status_code == 403, (role,path,response.text)
    adjustment={'ownerId':users['designer'].id,'creditUnits':7,'reason':'Local RBAC verification','idempotencyKey':'finance-rbac-once'}
    for _ in range(2):
        assert client.post('/api/v1/billing/admin/adjustments',json=adjustment,headers=headers['finance']).status_code == 201
    assert billing.wallet(users['designer'].id)['creditUnits'] == 7
    assert billing.status()['enabled'] is False
    assert billing.status()['purchaseEnabled'] is False
    assert billing.status()['refundExecutionEnabled'] is False


def test_support_can_reply_and_change_status_without_granting_other_staff_ticket_access(system):
    client, _, users, headers, _, support, _ = system
    body={'subject':'业务角色测试工单','category':'other','body':'测试客服权限','runId':None,'drawingConsent':False,'idempotencyKey':'rbac-ticket'}
    response=client.post('/api/v1/support/tickets',json=body,headers=headers['designer'])
    assert response.status_code == 201
    ticket=response.json(); url='/api/v1/support/admin/tickets/'+ticket['id']
    for role in set(ROLES)-{'admin','support'}:
        assert client.get(url,headers=headers[role]).status_code == 403
        assert client.post(url+'/messages',json={'body':'不得写入','idempotencyKey':'blocked'},headers=headers[role]).status_code == 403
        assert client.patch(url,json={'revision':ticket['revision'],'status':'resolved','reason':'不得更新','idempotencyKey':'blocked'},headers=headers[role]).status_code == 403
    assert client.get(url+'/audit',headers=headers['support']).status_code == 200
    reply=client.post(url+'/messages',json={'body':'客服测试回复','idempotencyKey':'support-reply'},headers=headers['support'])
    assert reply.status_code == 201
    current=reply.json()['ticket']
    changed=client.patch(url,json={'revision':current['revision'],'status':'resolved','reason':'已完成测试答复','idempotencyKey':'support-resolve'},headers=headers['support'])
    assert changed.status_code == 200 and changed.json()['status']=='resolved'
    assert support.messages(users['designer'].id,ticket['id'])['items'][-1]['authorRole']=='admin'


def test_service_boundary_rechecks_live_roles_and_inactive_accounts(system):
    client, services, users, headers, billing, support, policy = system
    for operation in (lambda:billing.dashboard(users['support'].id), lambda:billing.adjust(users['auditor'].id,owner_id=users['designer'].id,credit_units=1,reason='x',idempotency_key='denied'), lambda:policy.overview(users['finance'].id), lambda:support.list_tickets(users['finance'].id,admin=True)):
        with pytest.raises(AuthorizationError):operation()
    temporary=services.auth.create_user('finance-revoke@example.test','test-only-password','temporary',roles=['finance'])
    assert billing.dashboard(temporary.id)['paidOrders'] == 0
    token={'Authorization':'Bearer '+services.auth.issue_token(temporary).token}
    services.auth.assign_roles(temporary.id,['viewer'],actor_id=users['admin'].id)
    with pytest.raises(AuthorizationError):billing.dashboard(temporary.id)
    assert client.get('/api/v1/billing/admin/dashboard',headers=token).status_code in (401,403)
    services.auth.set_active(temporary.id,False,actor_id=users['admin'].id)
    with pytest.raises(AuthorizationError):billing.dashboard(temporary.id)


def test_cross_customer_reconciliation_requires_financial_write_permission_before_gateway(system):
    client, _, users, headers, billing, *_ = system
    # A local archived order fixture; payment switches stay off and no adapter exists.
    order_id='rbac-archived-order'
    with billing._transaction() as db:
        db.execute('INSERT INTO billing_orders (id,owner_id,idempotency_key,request_hash,package_json,amount_fen,credit_units,currency,status,provider,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (order_id,users['designer'].id,'rbac-archived','fixture','{}',100,10,'CNY','pending_payment','test-only','2026-09-10','2026-09-10'))
    path='/api/v1/billing/orders/'+order_id+'/reconcile'
    for role in ('auditor','support','ops','reviewer','manufacturing','viewer'):
        assert client.post(path,json={},headers=headers[role]).status_code == 404
    # Authorized staff and the customer reach the real configuration gate; no
    # fake successful query or payment is substituted when it is unavailable.
    for role in ('finance','admin','designer'):
        response=client.post(path,json={},headers=headers[role])
        assert response.status_code == 503 and '查询渠道' in response.text
    assert billing.order(users['designer'].id,order_id)['status']=='pending_payment'
    assert billing.status()['paymentConfigured'] is False
