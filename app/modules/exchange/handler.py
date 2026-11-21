from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store
from . import service

router=APIRouter()

@router.get('/exchange')
def page(request:Request):
    return render(request,'exchange')

@router.post('/api/exchange')
def execute(payload:dict=Body(default={})):
    from app.main import DB_PATH
    run_id=payload.get('run_id')
    base=store.get(DB_PATH,run_id) if run_id else store.save(DB_PATH,evaluate({'dataset':payload.get('dataset','dirty')}))
    return service.execute(DB_PATH,base,payload)

@router.get('/api/exchanges')
def exchanges():
    from app.main import DB_PATH
    return service.history(DB_PATH)

@router.get('/api/exchange/{exchange_id}')
def detail(exchange_id:str):
    from app.main import DB_PATH
    return service.get(DB_PATH,exchange_id)

@router.get('/api/exchange/{exchange_id}/packages/{version}')
def package(exchange_id:str,version:int):
    from app.main import DB_PATH
    return service.package(DB_PATH,exchange_id,version)
