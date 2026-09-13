import test from 'node:test'
import assert from 'node:assert/strict'
import { financeCsv, financeFilters, adminFinanceViews } from './adminFinance.js'
import { createBillingClient } from './billingClient.js'
import { adminPath, readAppRoute } from './adminNavigation.js'

test('finance filters survive routes without retaining private or unrelated fields', () => {
  const input={ownerId:'客户 A',status:'paid',q:'订单 %_1',source:'offline',dateFrom:'2026-09-01',dateTo:'2026-09-10',access_token:'secret'}
  const normalized=financeFilters(input)
  assert.equal(normalized.access_token,undefined)
  const route=readAppRoute(new URL(adminPath('orders',normalized),'https://cad.test'))
  assert.deepEqual(route.params,normalized)
  for (const name of Object.keys(adminFinanceViews)) assert.equal(readAppRoute(new URL(adminPath(name),'https://cad.test')).valid,true)
})
test('admin financial queries are encoded and customer routes cannot apply cross-account filters',async()=>{
  const requests=[]
  const api=createBillingClient({fetchImpl:async(url)=>{requests.push(url);return {ok:true,json:async()=>({items:[],total:0})}}})
  await api.list('t','ledger',{admin:true,ownerId:'a&b',q:'%_"',dateFrom:'2026-09-01',status:'adjustment'})
  let parsed=new URL(requests[0],'https://cad.test')
  assert.ok(parsed.pathname.endsWith('/admin/ledger'));assert.equal(parsed.searchParams.get('ownerId'),'a&b');assert.equal(parsed.searchParams.get('q'),'%_"')
  await api.list('t','orders',{ownerId:'other',status:'paid',q:'other'})
  parsed=new URL(requests[1],'https://cad.test');assert.equal(parsed.searchParams.has('ownerId'),false);assert.equal(parsed.searchParams.has('q'),false)
})
test('financial CSV has a fixed safe schema, neutralizes formulas, and preserves unknown amounts',()=>{
  const csv=financeCsv('orders',[{id:'=HYPERLINK("bad")',ownerId:' \t+SUM(1)',status:'paid',amountFen:null,creditUnits:3,createdAt:'2026-09-10',secret:'never-export'}])
  assert.ok(csv.startsWith('\ufeff'));assert.match(csv,/'=HYPERLINK/);assert.match(csv,/' \t\+SUM/);assert.doesNotMatch(csv,/never-export/);assert.match(csv,/,"","3",/)
  assert.match(financeCsv('ledger',[{deltaUnits:-5}]),/"-5"/)
})
