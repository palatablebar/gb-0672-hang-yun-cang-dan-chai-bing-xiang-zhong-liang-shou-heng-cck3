from collections import defaultdict

def _parent_map(records):
    return {r['id']:r['parent'].strip() for r in records}

def _root_of(parents,node):
    seen=set();current=node
    while current in parents:
        if current in seen:return None
        seen.add(current)
        parent=parents[current]
        if not parent:return current
        current=parent
    return None

def closure(run):
    """把图、重量、危险品、隔离与申报对账问题逐单归属到主单；归不到主单的列入未归属。"""
    records=run['records'];parents=_parent_map(records)
    roots=sorted(run['graph']['roots'])
    issues=defaultdict(list);unattached=[]
    def attach(node,code,detail):
        root=_root_of(parents,node)
        if root:issues[root].append({'code':code,'detail':detail})
        else:unattached.append({'id':node,'code':code,'detail':detail})
    for err in run['graph']['errors']:
        candidates=[err.get('id','')]+list(err.get('path') or [])
        root=next((r for r in (_root_of(parents,c) for c in candidates) if r),None)
        detail='→'.join(str(p) for p in (err.get('path') or [])) or err.get('id','')
        if root:issues[root].append({'code':err['code'],'detail':detail})
        else:unattached.append({'id':err.get('id',''),'code':err['code'],'detail':detail})
    for chk in run['weight_checks']:
        if chk['code']=='BALANCED':continue
        attach(chk['id'],chk['code'],f"{chk['field']} 申报{chk['declared']} 子单合计{chk['children_sum']} 差异{chk['difference']}")
    for issue in run['hazard_issues']:
        node=issue.get('id','')
        if _root_of(parents,node):attach(node,issue['code'],issue.get('classes',''))
        else:
            hit_roots={_root_of(parents,r['id']) for r in records if r['container']==node}
            hit_roots.discard(None)
            if hit_roots:
                for root in sorted(hit_roots):issues[root].append({'code':issue['code'],'detail':f"箱{node} {issue.get('classes','')}".strip()})
            else:unattached.append({'id':node,'code':issue['code'],'detail':issue.get('classes','')})
    for row in run['quarantine']:
        attach(row.get('id',''),row['code'],row.get('detail',''))
    for field in run['balance']['declaration_differences']:
        unattached.append({'id':'申报对账','code':'DECLARATION_MISMATCH','detail':f'{field} 与申报不符'})
    unclosed=[{'id':r,'reasons':issues[r]} for r in roots if r in issues]
    return {'roots':roots,'scope':[r for r in roots if r not in issues],'unclosed':unclosed,'unattached':unattached}

def decide(run,pending,downstream_incremental):
    """裁决：全闭合则全量；有未闭合且下游收增量则交可证明范围；否则等全补。"""
    base=closure(run)
    parents=_parent_map(run['records'])
    merged={u['id']:[dict(r) for r in u['reasons']] for u in base['unclosed']}
    pending_roots=set()
    for item in pending:
        code='PENDING_WITHDRAWAL' if item.get('kind')=='withdrawal' else 'PENDING_CORRECTION'
        for node in item.get('ids',[]):
            root=_root_of(parents,node)
            if root is None:merged.setdefault(node,[]).append({'code':'UNKNOWN_REFERENCE','detail':f'补正对象不在舱单内：{node}'})
            else:
                pending_roots.add(root)
                merged.setdefault(root,[]).append({'code':code,'detail':f'补正未到：{node}'})
    unclosed=[]
    for key in sorted(merged):
        seen=set();reasons=[]
        for reason in merged[key]:
            mark=(reason['code'],reason['detail'])
            if mark not in seen:seen.add(mark);reasons.append(reason)
        unclosed.append({'id':key,'reasons':reasons})
    scope=[r for r in base['scope'] if r not in pending_roots]
    blocked=bool(unclosed or base['unattached'])
    if not blocked:decision,package_type='FULL','FULL'
    elif downstream_incremental and scope:decision,package_type='PARTIAL','INCREMENTAL'
    else:decision,package_type='WAIT','NONE'
    return {'decision':decision,'package_type':package_type,'scope':scope,'unclosed':unclosed,'unattached':base['unattached']}
