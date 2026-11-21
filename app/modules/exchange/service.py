import csv, io, json, base64, hashlib
from decimal import Decimal
from datetime import datetime, timezone
from app import store
from app.engine import evaluate
from app.errors import BusinessError
from .analyze import decide, _parent_map, _root_of

FIELDS=['id','parent','packages','package_unit','net','gross','weight_unit','hazard','un_code','container','revision']
DECISION_LABELS={'FULL':'全量交换','PARTIAL':'交可证明范围','WAIT':'等全补'}

def _now():return datetime.now(timezone.utc).isoformat()
def _canon(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True)
def _norm(items):return sorted(_canon(i) for i in items)

def _decode(raw):
    for encoding in ('utf-8-sig','gb18030'):
        try:return raw.decode(encoding)
        except UnicodeDecodeError:continue
    raise BusinessError('BAD_ENCODING','文件编码无法识别')

def _apply_ops(text,arrived):
    rows=list(csv.reader(io.StringIO(text,newline='')))
    if not rows:raise BusinessError('EMPTY_FILE','请选择非空文件')
    header,data=rows[0],rows[1:]
    affected=[]
    for corr in arrived:
        if not isinstance(corr,dict):raise BusinessError('BAD_CORRECTION','补正须为对象')
        op=corr.get('op')
        if op=='upsert':
            row=corr.get('row')
            if not isinstance(row,dict) or not str(row.get('id','')).strip():
                raise BusinessError('BAD_CORRECTION','补正行缺少编号')
            cells=[str(row.get(field,'')) for field in FIELDS]
            target=cells[0].strip()
            data=[r for r in data if not(r and r[0].strip()==target)]
            data.append(cells);affected.append(target)
        elif op=='withdraw':
            target=str(corr.get('id','')).strip()
            if not target:raise BusinessError('BAD_CORRECTION','撤回缺少编号')
            parents={r[0].strip():r[1].strip() for r in data if len(r)>=2 and r[0].strip()}
            remove={target};changed=True
            while changed:
                changed=False
                for child,parent in parents.items():
                    if parent in remove and child not in remove:remove.add(child);changed=True
            data=[r for r in data if not(r and r[0].strip() in remove)]
            affected.append(target)
        else:raise BusinessError('BAD_CORRECTION','不支持的补正操作')
    out=io.StringIO();writer=csv.writer(out,lineterminator='\n')
    writer.writerow(header)
    for r in data:writer.writerow(r)
    return out.getvalue(),affected

def apply_corrections(db_path,base_run,arrived,overrides):
    """补正落盘：在旧批源快照上应用已到达补正并重算，受影响编号随新批返回。"""
    if not arrived and not overrides:return base_run,[]
    snapshot=base_run.get('input_snapshot')
    if snapshot is None:raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    corrected,affected=_apply_ops(_decode(base64.b64decode(snapshot['bytes_b64'])),arrived)
    new_snapshot={'bytes_b64':base64.b64encode(corrected.encode()).decode(),'context':snapshot['context']}
    run=store.save(db_path,evaluate({'dataset':base_run['source'],'overrides':overrides},snapshot=new_snapshot))
    return run,affected

def build_package(run,outcome,exchange_id,version,cutoff):
    """生成交换包内容。类型显式区分全量与增量，增量包随附未闭合清单，绝不冒充全量。"""
    package_type='FULL' if outcome['decision']=='FULL' else 'INCREMENTAL'
    parents=_parent_map(run['records']);scope=set(outcome['scope'])
    records=[{k:v for k,v in r.items() if not k.startswith('_')} for r in run['records'] if _root_of(parents,r['id']) in scope]
    roots=[r for r in run['records'] if r['id'] in scope]
    totals={'pieces':sum(r['pieces'] for r in roots),
            'net_kg':str(sum((Decimal(r['net_kg']) for r in roots),Decimal(0))),
            'gross_kg':str(sum((Decimal(r['gross_kg']) for r in roots),Decimal(0)))}
    return {'package_id':f'{exchange_id}-P{version}','exchange_id':exchange_id,
            'run_id':run['manifest']['run_id'],'version':version,'package_type':package_type,
            'is_full':package_type=='FULL','cutoff':cutoff,'scope':outcome['scope'],
            'excluded_unclosed':outcome['unclosed'],'unattached':outcome['unattached'],
            'records':records,'totals':totals,
            'rule_version':run['manifest']['rule_version'],'generated_at':_now()}

def execute(db_path,base_run,payload):
    arrived=payload.get('arrived') or []
    pending=payload.get('pending') or []
    overrides=payload.get('overrides') or {}
    if not isinstance(arrived,list) or not isinstance(pending,list):
        raise BusinessError('BAD_CORRECTION','补正须为列表')
    if not isinstance(overrides,dict):raise BusinessError('BAD_CONTEXT','参数须为对象')
    conf=base_run.get('context',{}).get('exchange',{})
    downstream_incremental=bool(payload.get('downstream_incremental',conf.get('downstream_incremental',True)))
    cutoff=str(payload.get('cutoff') or conf.get('cutoff',''))
    run,affected=apply_corrections(db_path,base_run,arrived,overrides)
    outcome=decide(run,pending,downstream_incremental)
    base_parents=_parent_map(base_run['records']);new_parents=_parent_map(run['records'])
    affected_roots=sorted({r for n in affected for r in (_root_of(base_parents,n),_root_of(new_parents,n)) if r})
    unclosed_map={u['id']:u['reasons'] for u in outcome['unclosed']}
    reverified=[{'id':root,'status':'未闭合' if root in unclosed_map else '闭合','reasons':unclosed_map.get(root,[])} for root in affected_roots]
    exchange_key=str(payload.get('exchange_key') or base_run['manifest']['run_id'])
    exchange_id='EX'+hashlib.sha256(_canon({'exchange_key':exchange_key}).encode()).hexdigest()[:14]
    digest=hashlib.sha256(_canon({'exchange_key':exchange_key,'run_id':run['manifest']['run_id'],
        'arrived':_norm(arrived),'pending':_norm(pending),'overrides':overrides,
        'downstream_incremental':downstream_incremental,'cutoff':cutoff}).encode()).hexdigest()
    now=_now()
    with store.connect(db_path) as con:
        row=con.execute('SELECT * FROM exchanges WHERE exchange_key=?',(exchange_key,)).fetchone()
        if row and row['digest']==digest:
            replay=json.loads(row['result']);replay['idempotent_replay']=True
            return replay
        version=(row['current_version'] if row else 0)+1
        package=content=None
        if outcome['decision']!='WAIT':
            try:content=build_package(run,outcome,exchange_id,version,cutoff)
            except BusinessError:raise
            except Exception as exc:raise BusinessError('EXCHANGE_INTERRUPTED',f'包生成中断，已恢复旧版：{exc}',500)
            package={'package_id':content['package_id'],'version':version,
                     'package_type':content['package_type'],'is_full':content['is_full'],
                     'scope_count':len(content['scope']),
                     'digest':hashlib.sha256(_canon(content).encode()).hexdigest()}
        receipt=None
        if package:
            receipt={'receipt_id':'RC'+package['digest'][:12],'ledger_no':'LEDGER-'+package['digest'][:10],
                     'exchange_id':exchange_id,'package_id':package['package_id'],
                     'package_type':package['package_type'],'scope_count':package['scope_count'],'received_at':now}
        result={'exchange_id':exchange_id,'exchange_key':exchange_key,
                'decision':outcome['decision'],'decision_label':DECISION_LABELS[outcome['decision']],
                'package_type':outcome['package_type'],
                'downstream':{'incremental':downstream_incremental,
                              'verdict':'可接收增量包' if downstream_incremental else '仅接收全量包'},
                'cutoff':cutoff,'base_run_id':base_run['manifest']['run_id'],'run_id':run['manifest']['run_id'],
                'corrections':{'arrived':len(arrived),'pending':len(pending)},
                'affected_roots':affected_roots,'reverified':reverified,
                'scope':outcome['scope'],'unclosed':outcome['unclosed'],'unattached':outcome['unattached'],
                'package':package,'receipt':receipt,
                'current_version':version if package else (row['current_version'] if row else 0),
                'idempotent_replay':False,'generated_at':now}
        if row:
            con.execute('UPDATE exchanges SET digest=?,run_id=?,result=?,current_version=?,updated_at=? WHERE exchange_key=?',
                        (digest,run['manifest']['run_id'],json.dumps(result,ensure_ascii=False),result['current_version'],now,exchange_key))
        else:
            con.execute('INSERT INTO exchanges(id,exchange_key,digest,run_id,current_version,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (exchange_id,exchange_key,digest,run['manifest']['run_id'],result['current_version'],json.dumps(result,ensure_ascii=False),now,now))
        if package:
            con.execute('INSERT INTO exchange_packages(id,exchange_id,version,package_type,scope,content,digest,created_at) VALUES(?,?,?,?,?,?,?,?)',
                        (package['package_id'],exchange_id,version,package['package_type'],
                         json.dumps(outcome['scope'],ensure_ascii=False),json.dumps(content,ensure_ascii=False),package['digest'],now))
            con.execute('INSERT OR IGNORE INTO exchange_receipts(id,exchange_id,package_id,ledger_no,payload,created_at) VALUES(?,?,?,?,?,?)',
                        (receipt['receipt_id'],exchange_id,receipt['package_id'],receipt['ledger_no'],json.dumps(receipt,ensure_ascii=False),now))
        return result

def history(db_path):
    with store.connect(db_path) as con:
        rows=con.execute('SELECT id,exchange_key,run_id,current_version,result,updated_at FROM exchanges ORDER BY rowid DESC LIMIT 100').fetchall()
        out=[]
        for r in rows:
            result=json.loads(r['result'])
            receipts=con.execute('SELECT COUNT(*) c FROM exchange_receipts WHERE exchange_id=?',(r['id'],)).fetchone()['c']
            out.append({'exchange_id':r['id'],'exchange_key':r['exchange_key'],'run_id':r['run_id'],
                        'decision':result['decision'],'package_type':result['package_type'],
                        'current_version':r['current_version'],'receipts':receipts,'updated_at':r['updated_at']})
        return out

def get(db_path,exchange_id):
    with store.connect(db_path) as con:
        row=con.execute('SELECT * FROM exchanges WHERE id=?',(exchange_id,)).fetchone()
        if not row:raise BusinessError('NOT_FOUND','交换不存在',404)
        result=json.loads(row['result'])
        versions=[dict(v) for v in con.execute('SELECT version,package_type,digest,created_at FROM exchange_packages WHERE exchange_id=? ORDER BY version',(exchange_id,)).fetchall()]
        receipts=[json.loads(r['payload']) for r in con.execute('SELECT payload FROM exchange_receipts WHERE exchange_id=? ORDER BY rowid',(exchange_id,)).fetchall()]
        result.update(versions=versions,receipts=receipts,current_version=row['current_version'])
        return result

def package(db_path,exchange_id,version):
    with store.connect(db_path) as con:
        row=con.execute('SELECT content FROM exchange_packages WHERE exchange_id=? AND version=?',(exchange_id,version)).fetchone()
        if not row:raise BusinessError('NOT_FOUND','交换包版本不存在',404)
        return json.loads(row['content'])
