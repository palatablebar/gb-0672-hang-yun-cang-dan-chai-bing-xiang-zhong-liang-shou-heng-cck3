import pytest
from fastapi.testclient import TestClient
from app import main
from app.engine import evaluate
from app.modules.exchange import analyze, service

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'));return TestClient(main.app)

FIX_M001={'arrived':[
    {'op':'upsert','row':{'id':'M001','parent':'','packages':'40','package_unit':'箱','net':'400','gross':'440','weight_unit':'kg','hazard':'none','un_code':'','container':'C001','revision':'2'}},
    {'op':'upsert','row':{'id':'M001-P1-2','parent':'M001-P1','packages':'1','package_unit':'pallet','net':'0.1','gross':'0.11','weight_unit':'t','hazard':'none','un_code':'','container':'C001','revision':'2'}}]}
PURGE=[{'op':'withdraw','id':i} for i in ('ORPHAN','LOOP-A','BAD','CONFLICT','COLUMN')]

def test_closure_dirty(sample=None):
    result=analyze.closure(evaluate())
    assert result['scope']==['M002','M003','M004','M005','M006']
    assert [u['id'] for u in result['unclosed']]==['M001']
    assert {u['code'] for u in result['unattached']}>={'MISSING_PARENT','DEPENDENCY_CYCLE','DECLARATION_MISMATCH'}

def test_partial_decision_keeps_incremental_explicit(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);r=c.post('/api/exchange',json={}).json()
    assert r['decision']=='PARTIAL' and r['decision_label']=='交可证明范围'
    assert r['package_type']=='INCREMENTAL' and r['package']['is_full'] is False
    assert r['scope']==['M002','M003','M004','M005','M006']
    assert [u['id'] for u in r['unclosed']]==['M001']
    assert r['downstream']['verdict']=='可接收增量包'
    package=c.get(f"/api/exchange/{r['exchange_id']}/packages/1").json()
    assert package['package_type']=='INCREMENTAL' and package['is_full'] is False
    assert package['excluded_unclosed'] and package['totals']['pieces']==200
    assert 'FULL' != package['package_type']

def test_wait_when_downstream_refuses_incremental(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json={'downstream_incremental':False}).json()
    assert r['decision']=='WAIT' and r['package_type']=='NONE'
    assert r['package'] is None and r['receipt'] is None
    assert r['downstream']['verdict']=='仅接收全量包'

def test_full_on_clean(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json={'dataset':'clean'}).json()
    assert r['decision']=='FULL' and r['package_type']=='FULL' and r['package']['is_full'] is True
    assert len(r['scope'])==6 and not r['unclosed'] and not r['unattached']

def test_same_exchange_retry_no_duplicate_receipt(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    a=c.post('/api/exchange',json={}).json();b=c.post('/api/exchange',json={}).json()
    assert b['idempotent_replay'] is True and a['exchange_id']==b['exchange_id']
    assert a['receipt']['receipt_id']==b['receipt']['receipt_id']
    detail=c.get(f"/api/exchange/{a['exchange_id']}").json()
    assert len(detail['receipts'])==1 and len(detail['versions'])==1
    assert c.get('/api/exchanges').json()[0]['receipts']==1

def test_pending_correction_uncloses_root(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json={'pending':[{'ids':['M003-P1']}]}).json()
    assert r['decision']=='PARTIAL' and 'M003' not in r['scope']
    m3=next(u for u in r['unclosed'] if u['id']=='M003')
    assert m3['reasons'][0]['code']=='PENDING_CORRECTION'

def test_pending_unknown_reference_blocks_full(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json={'dataset':'clean','pending':[{'ids':['GHOST']}]}).json()
    assert r['decision']=='PARTIAL'
    assert any(u['id']=='GHOST' and u['reasons'][0]['code']=='UNKNOWN_REFERENCE' for u in r['unclosed'])

def test_arrived_correction_reverify_and_scope_regain(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json=FIX_M001).json()
    assert r['corrections']['arrived']==2 and r['affected_roots']==['M001']
    assert r['reverified']==[{'id':'M001','status':'闭合','reasons':[]}]
    assert 'M001' in r['scope'] and r['run_id']!=r['base_run_id']

def test_full_after_corrections_and_withdrawals(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json={'arrived':FIX_M001['arrived']+PURGE}).json()
    assert r['decision']=='FULL' and r['package']['is_full'] is True
    assert r['unclosed']==[] and r['unattached']==[]

def test_withdrawn_container_never_shipped(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    r=c.post('/api/exchange',json={'arrived':[{'op':'withdraw','id':'M006-P2-1'}]}).json()
    assert 'M006' not in r['scope']
    m6=next(u for u in r['unclosed'] if u['id']=='M006')
    assert any(x['code']=='WEIGHT_MISMATCH' for x in m6['reasons'])
    package=c.get(f"/api/exchange/{r['exchange_id']}/packages/1").json()
    assert all('M006' not in rec['id'] for rec in package['records'])

def test_interrupted_generation_restores_previous_version(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    first=c.post('/api/exchange',json={}).json()
    ex=first['exchange_id']
    original=service.build_package
    monkeypatch.setattr(service,'build_package',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('io error')))
    broken=c.post('/api/exchange',json={'pending':[{'ids':['M003']}]})
    assert broken.status_code==500 and broken.json()['code']=='EXCHANGE_INTERRUPTED'
    detail=c.get(f'/api/exchange/{ex}').json()
    assert detail['current_version']==1 and len(detail['receipts'])==1
    assert c.get(f'/api/exchange/{ex}/packages/1').status_code==200
    monkeypatch.setattr(service,'build_package',original)
    retry=c.post('/api/exchange',json={'pending':[{'ids':['M003']}]}).json()
    assert retry['current_version']==2
    assert c.get(f'/api/exchange/{ex}/packages/1').status_code==200
    assert len(c.get(f'/api/exchange/{ex}').json()['receipts'])==2

def test_exchange_key_versions_accumulate(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    a=c.post('/api/exchange',json={'exchange_key':'CUTOFF-1'}).json()
    b=c.post('/api/exchange',json={'exchange_key':'CUTOFF-1','pending':[{'ids':['M004-P1']}]}).json()
    assert a['exchange_id']==b['exchange_id'] and b['current_version']==2
    detail=c.get(f"/api/exchange/{a['exchange_id']}").json()
    assert [v['version'] for v in detail['versions']]==[1,2]
    again=c.post('/api/exchange',json={'exchange_key':'CUTOFF-1','pending':[{'ids':['M004-P1']}]}).json()
    assert again['idempotent_replay'] is True
    assert len(c.get(f"/api/exchange/{a['exchange_id']}").json()['receipts'])==2

def test_bad_correction_rejected(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.post('/api/exchange',json={'arrived':[{'op':'upsert','row':{}}]}).status_code==422
    assert c.post('/api/exchange',json={'arrived':[{'op':'teleport','id':'M001'}]}).status_code==422

def test_missing_exchange_and_package_404(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.get('/api/exchange/EXnone').status_code==404
    r=c.post('/api/exchange',json={}).json()
    assert c.get(f"/api/exchange/{r['exchange_id']}/packages/9").status_code==404
