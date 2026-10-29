"""Source context, versioned business rules, and request review evidence.
Local prototype only; actor names are self-reported and NOT authenticated.
"""
import json,datetime

def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
def setup(c):
 c.executescript("""
 CREATE TABLE IF NOT EXISTS source_system_profiles(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,description TEXT DEFAULT '',owner TEXT DEFAULT '',steward TEXT DEFAULT '',creation_process TEXT DEFAULT '',update_triggers TEXT DEFAULT '',validation_behavior TEXT DEFAULT '',history_behavior TEXT DEFAULT '',limitations TEXT DEFAULT '',status TEXT NOT NULL DEFAULT 'Draft',updated_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS business_process_profiles(id INTEGER PRIMARY KEY,name TEXT NOT NULL,source_system_id INTEGER REFERENCES source_system_profiles(id),description TEXT DEFAULT '',creation_triggers TEXT DEFAULT '',actors TEXT DEFAULT '',steps TEXT DEFAULT '',exceptions TEXT DEFAULT '',status TEXT NOT NULL DEFAULT 'Draft',updated_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS business_rules(id INTEGER PRIMARY KEY,rule_code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,rule_type TEXT NOT NULL DEFAULT 'Other',source_system_id INTEGER REFERENCES source_system_profiles(id),business_process_id INTEGER REFERENCES business_process_profiles(id),owner TEXT DEFAULT '',steward TEXT DEFAULT '',status TEXT NOT NULL DEFAULT 'Draft',current_version INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS business_rule_versions(id INTEGER PRIMARY KEY,rule_id INTEGER NOT NULL REFERENCES business_rules(id),version INTEGER NOT NULL,description TEXT NOT NULL,trigger_condition TEXT DEFAULT '',outcome TEXT DEFAULT '',exceptions TEXT DEFAULT '',technical_implementation TEXT DEFAULT '',evidence_reference TEXT DEFAULT '',effective_from TEXT DEFAULT '',effective_to TEXT DEFAULT '',limitations TEXT DEFAULT '',changed_by TEXT NOT NULL,changed_at TEXT NOT NULL,UNIQUE(rule_id,version));
 CREATE TABLE IF NOT EXISTS business_rule_links(id INTEGER PRIMARY KEY,rule_id INTEGER NOT NULL REFERENCES business_rules(id),target_type TEXT NOT NULL CHECK(target_type IN ('resource','field','product','capability')),target_id TEXT NOT NULL,notes TEXT DEFAULT '',UNIQUE(rule_id,target_type,target_id));
 CREATE TABLE IF NOT EXISTS business_rule_reviews(id INTEGER PRIMARY KEY,rule_id INTEGER NOT NULL REFERENCES business_rules(id),version INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('Draft','Reviewed','Approved','Retired')),actor TEXT NOT NULL,notes TEXT DEFAULT '',reviewed_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS source_context_acknowledgements(id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL REFERENCES access_requests(id),product_id INTEGER NOT NULL REFERENCES marketplace_collections(id),actor TEXT NOT NULL,acknowledged_at TEXT NOT NULL,context_snapshot_json TEXT NOT NULL,UNIQUE(request_id,product_id));
 CREATE INDEX IF NOT EXISTS idx_rule_links_target ON business_rule_links(target_type,target_id);
 """)

def rows(c,sql,args=()):return [dict(x) for x in c.execute(sql,args)]
def required(d,key):
 v=str(d.get(key,'')).strip()
 if not v:raise ValueError(key+' is required')
 return v
def profile(c,kind,d):
 table={'system':'source_system_profiles','process':'business_process_profiles'}[kind]
 allowed=({'name','description','owner','steward','creation_process','update_triggers','validation_behavior','history_behavior','limitations','status'} if kind=='system' else {'name','source_system_id','description','creation_triggers','actors','steps','exceptions','status'})
 required(d,'name');status=d.get('status','Draft')
 if status not in ('Draft','Reviewed','Approved','Retired'):raise ValueError('Invalid status')
 vals={k:d[k] for k in allowed if k in d};vals['updated_at']=stamp()
 if d.get('id'):
  rid=int(d['id']);cur=c.execute('UPDATE '+table+' SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),rid))
  if not cur.rowcount:raise ValueError('Profile not found')
 else:rid=c.execute('INSERT INTO '+table+'('+','.join(vals)+') VALUES('+','.join('?' for _ in vals)+')',tuple(vals.values())).lastrowid
 return {'id':rid}
def rule(c,d):
 name=required(d,'name');actor=required(d,'actor');description=required(d,'description')
 if d.get('id'):
  rid=int(d['id']);old=c.execute('SELECT * FROM business_rules WHERE id=?',(rid,)).fetchone()
  if not old:raise ValueError('Rule not found')
  version=old['current_version']+1
  c.execute('UPDATE business_rules SET name=?,rule_type=?,source_system_id=?,business_process_id=?,owner=?,steward=?,status=?,current_version=?,updated_at=? WHERE id=?',(name,d.get('rule_type','Other'),d.get('source_system_id') or None,d.get('business_process_id') or None,d.get('owner',''),d.get('steward',''),'Draft',version,stamp(),rid))
 else:
  code=required(d,'rule_code');version=1
  rid=c.execute('INSERT INTO business_rules(rule_code,name,rule_type,source_system_id,business_process_id,owner,steward,current_version,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(code,name,d.get('rule_type','Other'),d.get('source_system_id') or None,d.get('business_process_id') or None,d.get('owner',''),d.get('steward',''),version,stamp())).lastrowid
 fields=['description','trigger_condition','outcome','exceptions','technical_implementation','evidence_reference','effective_from','effective_to','limitations']
 c.execute('INSERT INTO business_rule_versions(rule_id,version,'+','.join(fields)+',changed_by,changed_at) VALUES('+','.join('?' for _ in range(len(fields)+4))+')',(rid,version,*(str(d.get(f,'')) for f in fields),actor,stamp()))
 c.execute('INSERT INTO business_rule_reviews(rule_id,version,status,actor,notes,reviewed_at) VALUES(?,?,?,?,?,?)',(rid,version,'Draft',actor,'New rule version',stamp()))
 return {'id':rid,'version':version,'status':'Draft'}
def link(c,d):
 rid=int(d['rule_id']);kind=required(d,'target_type');tid=required(d,'target_id')
 if kind not in ('resource','field','product','capability'):raise ValueError('Invalid target type')
 if not c.execute('SELECT 1 FROM business_rules WHERE id=?',(rid,)).fetchone():raise ValueError('Rule not found')
 if kind in ('resource','field','product'):
  table={'resource':'assets','field':'resource_fields','product':'marketplace_collections'}[kind]
  if not tid.isdecimal() or not c.execute('SELECT 1 FROM '+table+' WHERE id=?',(int(tid),)).fetchone():raise ValueError('Target not found')
 else:
  if not c.execute('SELECT 1 FROM capability_identifiers WHERE capability_code=?',(tid,)).fetchone():raise ValueError('Capability not found')
 c.execute('INSERT OR IGNORE INTO business_rule_links(rule_id,target_type,target_id,notes) VALUES(?,?,?,?)',(rid,kind,tid,str(d.get('notes',''))))
 return {'linked':True}
def review(c,d):
 rid=int(d['rule_id']);actor=required(d,'actor');target=required(d,'status')
 r=c.execute('SELECT * FROM business_rules WHERE id=?',(rid,)).fetchone()
 if not r:raise ValueError('Rule not found')
 allowed={'Draft':['Reviewed'],'Reviewed':['Approved','Draft'],'Approved':['Retired'],'Retired':[]}
 if target not in allowed[r['status']]:raise ValueError('Invalid review transition')
 c.execute('UPDATE business_rules SET status=?,updated_at=? WHERE id=?',(target,stamp(),rid))
 c.execute('INSERT INTO business_rule_reviews(rule_id,version,status,actor,notes,reviewed_at) VALUES(?,?,?,?,?,?)',(rid,r['current_version'],target,actor,str(d.get('notes','')),stamp()))
 return {'status':target,'version':r['current_version']}
def get_rules(c):
 result=rows(c,'SELECT r.*,v.description,v.trigger_condition,v.outcome,v.exceptions,v.technical_implementation,v.evidence_reference,v.effective_from,v.effective_to,v.limitations,v.changed_by,v.changed_at FROM business_rules r JOIN business_rule_versions v ON v.rule_id=r.id AND v.version=r.current_version ORDER BY r.name')
 for r in result:
  r['links']=rows(c,'SELECT target_type,target_id,notes FROM business_rule_links WHERE rule_id=?',(r['id'],))
  r['versions']=rows(c,'SELECT version,description,changed_by,changed_at FROM business_rule_versions WHERE rule_id=? ORDER BY version DESC',(r['id'],))
  r['reviews']=rows(c,'SELECT version,status,actor,notes,reviewed_at FROM business_rule_reviews WHERE rule_id=? ORDER BY id',(r['id'],))
 return result
def product_context(c,product_id,approved_only=True):
 pid=int(product_id)
 product=c.execute('SELECT * FROM marketplace_collections WHERE id=?',(pid,)).fetchone()
 if not product:raise ValueError('Data product not found')
 resources=rows(c,'SELECT a.id,a.name,a.system FROM marketplace_collection_assets m JOIN assets a ON a.id=m.asset_id WHERE m.collection_id=?',(pid,))
 systems=rows(c,'SELECT DISTINCT s.* FROM source_system_profiles s WHERE s.name IN (SELECT a.system FROM marketplace_collection_assets m JOIN assets a ON a.id=m.asset_id WHERE m.collection_id=?)',(pid,))
 system_ids=[s['id'] for s in systems]
 processes=rows(c,'SELECT * FROM business_process_profiles WHERE source_system_id IN ('+(','.join('?' for _ in system_ids) or 'NULL')+')',system_ids)
 rules=get_rules(c)
 asset_ids={str(a['id']) for a in resources}
 product_caps={r[0] for r in c.execute('SELECT capability_code FROM marketplace_collection_capabilities WHERE collection_id=?',(pid,))}
 field_ids={str(r[0]) for r in c.execute('SELECT id FROM resource_fields WHERE resource_id IN ('+(','.join('?' for _ in resources) or 'NULL')+')',[a['id'] for a in resources])} if resources else set()
 selected=[]
 for r in rules:
  if approved_only and r['status']!='Approved':continue
  if any((l['target_type']=='product' and l['target_id']==str(pid)) or (l['target_type']=='resource' and l['target_id'] in asset_ids) or (l['target_type']=='field' and l['target_id'] in field_ids) or (l['target_type']=='capability' and l['target_id'] in product_caps) for l in r['links']):selected.append(r)
 return {'product':dict(product),'resources':resources,'source_systems':systems,'business_processes':processes,'rules':selected,'context_generated_at':stamp()}
def acknowledge(c,d):
 request_id=int(d['request_id']);product_id=int(d['product_id']);actor=required(d,'actor')
 if not d.get('acknowledged'):raise ValueError('Affirmative acknowledgement required')
 request=c.execute('SELECT status FROM access_requests WHERE id=?',(request_id,)).fetchone()
 if not request:raise ValueError('Access request not found')
 if request['status'] not in ('Draft','Submitted','Returned for Revision'):raise ValueError('Request is not editable for acknowledgement')
 snapshot=product_context(c,product_id,True)
 if not snapshot['rules']:raise ValueError('No approved, linked business rules available for acknowledgement')
 snapshot['acknowledgement_statement']='Requestor reviewed the approved source context and business rules for the selected product. This is not an authorization to access data.'
 c.execute('INSERT INTO source_context_acknowledgements(request_id,product_id,actor,acknowledged_at,context_snapshot_json) VALUES(?,?,?,?,?) ON CONFLICT(request_id,product_id) DO UPDATE SET actor=excluded.actor,acknowledged_at=excluded.acknowledged_at,context_snapshot_json=excluded.context_snapshot_json',(request_id,product_id,actor,stamp(),json.dumps(snapshot)))
 return {'recorded':True,'request_id':request_id,'product_id':product_id,'rule_versions':[{'rule_code':r['rule_code'],'version':r['current_version']} for r in snapshot['rules']]}
def get_ack(c,request_id):
 out=rows(c,'SELECT id,request_id,product_id,actor,acknowledged_at,context_snapshot_json FROM source_context_acknowledgements WHERE request_id=?',(int(request_id),))
 for x in out:x['context_snapshot']=json.loads(x.pop('context_snapshot_json'))
 return out
