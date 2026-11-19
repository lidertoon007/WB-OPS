import os,json,sqlite3,asyncio,httpx
from pathlib import Path
from dotenv import dotenv_values
cfg=dotenv_values('/etc/wb-ops/wb-ops.env')
root=Path('/opt/wb-ops/current')
db=Path(cfg.get('WBOPS_DATABASE_URL','sqlite:///data/wbops.sqlite3').removeprefix('sqlite:///'))
if not db.is_absolute(): db=root/db
c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
raw=c.execute("select value from app_settings where key='cabinets_json'").fetchone()
cabs=json.loads(raw[0] if raw else cfg.get('WBOPS_CABINETS_JSON','[]'))
rows=[dict(r) for r in c.execute("select i.id,i.cabinet_id,i.product_id,i.created_at,i.wb_unanswered,d.published,d.text as answer from inbox_items i left join reply_drafts d on d.item_id=i.id where i.item_type='question'")]
targets=[r for r in rows if r['product_id']==466345357]
print('local_targets',json.dumps([{k:v for k,v in r.items() if k!='answer'} for r in targets],ensure_ascii=False))
for r in targets:
 print('audit',r['id'],json.dumps([dict(a) for a in c.execute("select event,created_at,details from audit_log where item_id=? order by id",(r['id'],))],ensure_ascii=False))
async def main():
 for cab in cabs:
  if cab.get('marketplace','wildberries')!='wildberries' or not cab.get('token'): continue
  async with httpx.AsyncClient(base_url=cfg.get('WBOPS_FEEDBACKS_API_BASE_URL','https://feedbacks-api.wildberries.ru'),headers={'Authorization':cab['token']},timeout=30) as cl:
   found=[]; skip=0
   while True:
    res=await cl.get('/api/v1/questions',params={'isAnswered':'false','take':5000,'skip':skip,'order':'dateDesc'})
    if res.status_code!=200:
     print('cabinet_error',cab['id'],res.status_code);break
    payload=res.json();data=payload.get('data',{});batch=data.get('questions',[])
    if not isinstance(batch,list): print('invalid_batch',cab['id']);break
    found.extend(batch)
    if len(batch)<5000:break
    skip+=5000;await asyncio.sleep(.4)
   ids={str(q.get('id')) for q in found}
   local=[r for r in rows if r['cabinet_id']==cab['id']]
   overlap=[r for r in local if r['published'] and r['id'] in ids]
   print('cabinet_summary',json.dumps({'id':cab['id'],'name':cab.get('name'),'local_total':len(local),'local_published':sum(bool(r['published']) for r in local),'wb_unanswered_returned':len(found),'published_still_unanswered':len(overlap),'wb_reported_unanswered':data.get('countUnanswered')},ensure_ascii=False))
   for r in targets:
    if r['cabinet_id']!=cab['id']:continue
    q=next((x for x in found if str(x.get('id'))==r['id']),None)
    if q is None:
     await asyncio.sleep(.4);resp=await cl.get('/api/v1/question',params={'id':r['id']})
     if resp.status_code==200:q=resp.json().get('data')
    a=(q or {}).get('answer') or {}
    print('target_wb',json.dumps({'id':r['id'],'in_unanswered':r['id'] in ids,'state':(q or {}).get('state'),'answer_type':type((q or {}).get('answer')).__name__,'answer_nonempty':bool(a.get('text')) if isinstance(a,dict) else bool(a),'answer_matches_local':a.get('text')==r['answer'] if isinstance(a,dict) else False,'wasViewed':(q or {}).get('wasViewed')},ensure_ascii=False))
   await asyncio.sleep(.4)
asyncio.run(main())
