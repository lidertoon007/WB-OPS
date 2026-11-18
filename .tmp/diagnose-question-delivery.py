import base64
import subprocess

script = '''import sqlite3,json,datetime
c=sqlite3.connect("file:/opt/wb-ops/current/data/wbops.sqlite3?mode=ro",uri=True)
queries={
"now":"SELECT datetime('now')",
"totals":"SELECT i.item_type,COUNT(*),SUM(COALESCE(d.published,0)),SUM(CASE WHEN COALESCE(d.published,0)=0 AND i.wb_unanswered IS NOT 0 THEN 1 ELSE 0 END) FROM inbox_items i LEFT JOIN reply_drafts d ON d.item_id=i.id GROUP BY i.item_type",
"question_delivery":"SELECT COALESCE(s.value,''),COUNT(*) FROM inbox_items i LEFT JOIN app_settings s ON s.key='reply_delivery:'||i.id WHERE i.item_type='question' GROUP BY COALESCE(s.value,'')",
"question_events_daily":"SELECT substr(a.created_at,1,10),a.event,COUNT(*) FROM audit_log a JOIN inbox_items i ON i.id=a.item_id WHERE i.item_type='question' AND a.event IN ('published','publish_failed','publish_uncertain','draft_confirmed_by_send') GROUP BY 1,2 ORDER BY 1 DESC LIMIT 30",
"question_latest_events":"SELECT a.event,a.created_at FROM audit_log a JOIN inbox_items i ON i.id=a.item_id WHERE i.item_type='question' AND a.event IN ('published','publish_failed','publish_uncertain') ORDER BY a.id DESC LIMIT 12",
"sync_events":"SELECT event,created_at,details FROM audit_log WHERE event='sync' ORDER BY id DESC LIMIT 6",
"question_dates":"SELECT MIN(created_at),MAX(created_at) FROM inbox_items WHERE item_type='question'",
}
for name,q in queries.items(): print(name,json.dumps(c.execute(q).fetchall(),ensure_ascii=False))
'''
encoded=base64.b64encode(script.encode()).decode()
remote=f"sudo /opt/wb-ops/venv/bin/python -c \"import base64;exec(base64.b64decode('{encoded}'))\""
result=subprocess.run(['ssh','-p','443','ubuntu@185.149.145.12',remote],capture_output=True)
print(result.stdout.decode())
print(result.stderr.decode())
raise SystemExit(result.returncode)
