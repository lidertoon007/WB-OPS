"""Reconcile false published question flags. Default is read-only; never sends answers."""
import argparse, asyncio, json, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
import httpx
from dotenv import dotenv_values

async def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--apply',action='store_true'); parser.add_argument('--expected-count',type=int,default=125)
    args=parser.parse_args(); root=Path('/opt/wb-ops/current'); cfg=dotenv_values('/etc/wb-ops/wb-ops.env')
    db=Path(cfg.get('WBOPS_DATABASE_URL','sqlite:///data/wbops.sqlite3').removeprefix('sqlite:///'))
    if not db.is_absolute(): db=root/db
    con=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    raw=con.execute("select value from app_settings where key='cabinets_json'").fetchone()
    cabinets=json.loads(raw[0] if raw else cfg.get('WBOPS_CABINETS_JSON','[]'))
    candidates=[]
    for cab in cabinets:
        if cab.get('marketplace','wildberries')!='wildberries' or not cab.get('token'): continue
        local={r['id']:dict(r) for r in con.execute("select i.id,d.text,d.confirmed,d.user_edited,coalesce(s.value,'') as delivery,(select max(a.id) from audit_log a where a.item_id=i.id) as audit_id from inbox_items i join reply_drafts d on d.item_id=i.id left join app_settings s on s.key='reply_delivery:'||i.id where i.item_type='question' and i.cabinet_id=? and d.published=1",(cab['id'],))}
        if not local: continue
        async with httpx.AsyncClient(base_url=cfg.get('WBOPS_FEEDBACKS_API_BASE_URL','https://feedbacks-api.wildberries.ru'),headers={'Authorization':cab['token']},timeout=30) as client:
            queue=[];skip=0
            while True:
                if skip>=10000:raise RuntimeError('WB queue exceeds API pagination boundary; abort')
                await asyncio.sleep(.4)
                res=await client.get('/api/v1/questions',params={'isAnswered':'false','take':1000,'skip':skip,'order':'dateDesc'})
                res.raise_for_status(); payload=res.json()
                if payload.get('error') is not False:raise RuntimeError('WB did not confirm queue')
                data=payload['data'];batch=data['questions'];queue.extend(batch)
                if len(batch)<1000:break
                skip+=1000
            if len(queue)!=data['countUnanswered']:raise RuntimeError('Queue not complete; abort')
            selected=[q for q in queue if q['id'] in local and q.get('answer') is None and q.get('state')=='suppliersPortalSynch']
            for q in selected:
                snapshot=local[q['id']]
                if snapshot['delivery']!='sent':continue
                # In apply mode recheck each exact question, not just its list membership.
                if args.apply:
                    await asyncio.sleep(.4)
                    response=await client.get('/api/v1/question',params={'id':q['id']});response.raise_for_status();p=response.json()
                    if p.get('error') is not False:raise RuntimeError('WB did not confirm question')
                    fresh=p.get('data') or {}
                    if fresh.get('id')!=q['id'] or fresh.get('answer') is not None or fresh.get('state')!='suppliersPortalSynch':continue
                candidates.append(snapshot)
            print(json.dumps({'cabinet':cab['id'],'candidate_count':len(selected)},ensure_ascii=False))
    con.close()
    print(json.dumps({'mode':'apply' if args.apply else 'dry_run','candidate_count':len(candidates),'expected_count':args.expected_count,'expected_count_matches':len(candidates)==args.expected_count,'backup_directory_writable':os.access(db.resolve().parent,os.W_OK),'audit_schema_valid':{'event','item_id','details','created_at','actor'}.issubset({r[1] for r in sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True).execute('pragma table_info(audit_log)')})}))
    if not args.apply:return
    if len(candidates)!=args.expected_count:raise RuntimeError('Candidate count changed; re-review before apply')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=db.with_name(db.name+'.before-question-reconcile-'+stamp+'.bak')
    source=sqlite3.connect(db);dest=sqlite3.connect(backup);source.backup(dest);dest.close();source.close();os.chmod(backup,0o600)
    writer=sqlite3.connect(db);writer.row_factory=sqlite3.Row
    try:
        writer.execute('BEGIN IMMEDIATE')
        for row in candidates:
            current=writer.execute("select d.text,d.confirmed,d.user_edited,d.published,coalesce(s.value,'') as delivery,(select max(a.id) from audit_log a where a.item_id=d.item_id) as audit_id from reply_drafts d left join app_settings s on s.key='reply_delivery:'||d.item_id where d.item_id=?",(row['id'],)).fetchone()
            if current is None or current['published']!=1 or any(current[k]!=row[k] for k in ('text','confirmed','user_edited','delivery','audit_id')):raise RuntimeError('Local state changed; abort')
            writer.execute('update reply_drafts set published=0 where item_id=?',(row['id'],))
            writer.execute("update app_settings set value='' where key=?",('reply_delivery:'+row['id'],))
            writer.execute('update inbox_items set wb_unanswered=1 where id=?',(row['id'],))
            writer.execute("insert into audit_log(event,item_id,details,created_at,actor) values(?,?,?,?,?)",('publication_reconciled_missing_wb_answer',row['id'],'WB GET: state=suppliersPortalSynch; answer=null; old published/sent corrected; draft preserved',datetime.now(timezone.utc).isoformat(),'maintenance'))
        writer.commit()
    except BaseException:writer.rollback();raise
    finally:writer.close()
    print(json.dumps({'corrected':len(candidates),'backup':str(backup),'answers_sent':0}))

if __name__=='__main__':asyncio.run(main())
