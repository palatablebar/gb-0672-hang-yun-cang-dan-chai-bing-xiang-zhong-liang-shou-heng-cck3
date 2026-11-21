from .analyze import closure

def apply(state):
    result=closure(state)
    conf=state['context'].get('exchange',{})
    downstream=bool(conf.get('downstream_incremental',True))
    blocked=bool(result['unclosed'] or result['unattached'])
    if not blocked:preview='全量可交'
    elif downstream and result['scope']:preview='交可证明范围'
    else:preview='等全补'
    state['exchange']={'preview':preview,**result}
    unclosed_map={u['id']:u for u in result['unclosed']}
    rows=[{'id':root,'status':'未闭合' if root in unclosed_map else '闭合',
           'reasons':'；'.join(r['code'] for r in unclosed_map[root]['reasons']) if root in unclosed_map else ''}
          for root in result['roots']]
    state['views']['exchange']={'title':'交换裁决','rows':rows,'unattached':result['unattached'],
        'metrics':{'预裁决':preview,'可交主单':len(result['scope']),'未闭合':len(result['unclosed']),
                   '未归属':len(result['unattached']),'下游':'可接收增量' if downstream else '仅接收全量',
                   '截单':conf.get('cutoff','-')}}
