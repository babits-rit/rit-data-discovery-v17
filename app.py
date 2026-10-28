#!/usr/bin/env python3
"""Local-only RIT data discovery prototype. Not for network deployment."""
import csv, io, json, sqlite3, pathlib, re, datetime, urllib.parse, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from openpyxl import load_workbook
ROOT=pathlib.Path(__file__).resolve().parent
CAPS=json.loads((ROOT/'herm_capabilities.json').read_text())
BYCODE={c['code']:c for c in CAPS}
BYID={c['id']:c for c in CAPS}
DB=ROOT/'catalog.db'
STATES=['Created','Reviewed','Approved by Steward','Published']
TYPES=['Table','Dataset','Dashboard','Report','File','Data Product','Model']
def connect():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
def init():
 with connect() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS assets(id INTEGER PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,description TEXT DEFAULT '',system TEXT DEFAULT '',owner TEXT DEFAULT '',refresh_frequency TEXT DEFAULT '',classification TEXT DEFAULT 'Internal',access_url TEXT DEFAULT '',access_instructions TEXT DEFAULT '',is_published INTEGER DEFAULT 0,fields_json TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS asset_capabilities(asset_id INTEGER,capability_code TEXT,PRIMARY KEY(asset_id,capability_code));
CREATE TABLE IF NOT EXISTS glossary(id INTEGER PRIMARY KEY,term TEXT NOT NULL,definition TEXT NOT NULL,context TEXT DEFAULT '',submitted_by TEXT NOT NULL,steward TEXT DEFAULT '',status TEXT NOT NULL DEFAULT 'Created',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS glossary_capabilities(term_id INTEGER,capability_code TEXT,PRIMARY KEY(term_id,capability_code));
CREATE TABLE IF NOT EXISTS glossary_history(id INTEGER PRIMARY KEY,term_id INTEGER NOT NULL,old_status TEXT,new_status TEXT NOT NULL,actor TEXT NOT NULL,changed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stewards(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT '', department TEXT DEFAULT '', role TEXT DEFAULT 'Data Steward', active INTEGER NOT NULL DEFAULT 1, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS steward_capabilities(steward_id INTEGER NOT NULL REFERENCES stewards(id) ON DELETE CASCADE,capability_code TEXT NOT NULL, PRIMARY KEY(steward_id,capability_code));
CREATE TABLE IF NOT EXISTS access_requests(id INTEGER PRIMARY KEY, title TEXT NOT NULL, requestor_name TEXT NOT NULL, requestor_email TEXT DEFAULT '', department TEXT DEFAULT '', purpose TEXT NOT NULL, use_cases TEXT NOT NULL, data_requested TEXT NOT NULL, intended_audience TEXT DEFAULT '', sharing_details TEXT DEFAULT '', retention TEXT DEFAULT '', start_date TEXT DEFAULT '', end_date TEXT DEFAULT '', business_owner TEXT NOT NULL, source_system TEXT DEFAULT '', creation_process TEXT NOT NULL, source_fields TEXT DEFAULT '', source_limitations TEXT NOT NULL, usage_restrictions TEXT NOT NULL, classification TEXT DEFAULT '', steward_id INTEGER REFERENCES stewards(id), status TEXT NOT NULL DEFAULT 'Draft', created_at TEXT NOT NULL, review_date TEXT DEFAULT '', reviewed_by TEXT DEFAULT '', review_notes TEXT DEFAULT '', owner_ack INTEGER NOT NULL DEFAULT 0, owner_ack_name TEXT DEFAULT '', owner_ack_at TEXT DEFAULT '', requestor_ack INTEGER NOT NULL DEFAULT 0, requestor_ack_name TEXT DEFAULT '', requestor_ack_at TEXT DEFAULT '', policy_reference TEXT NOT NULL DEFAULT 'Applicable RIT policies (confirm current policy references)', policy_version TEXT DEFAULT '', decision_date TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS access_request_capabilities(request_id INTEGER NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,capability_code TEXT NOT NULL, PRIMARY KEY(request_id,capability_code));
CREATE TABLE IF NOT EXISTS access_request_history(id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,old_status TEXT,new_status TEXT NOT NULL,actor TEXT NOT NULL,changed_at TEXT NOT NULL,notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS marketplace_collections(id INTEGER PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL,description TEXT DEFAULT '',business_owner TEXT DEFAULT '',steward_id INTEGER,classification TEXT DEFAULT 'Internal',quality_notes TEXT DEFAULT '',refresh_frequency TEXT DEFAULT '',permitted_uses TEXT DEFAULT '',restrictions TEXT DEFAULT '',delivery_method TEXT DEFAULT 'Contact steward',status TEXT NOT NULL DEFAULT 'Draft',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS marketplace_collection_assets(collection_id INTEGER NOT NULL REFERENCES marketplace_collections(id) ON DELETE CASCADE,asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,PRIMARY KEY(collection_id,asset_id));
CREATE TABLE IF NOT EXISTS marketplace_collection_capabilities(collection_id INTEGER NOT NULL REFERENCES marketplace_collections(id) ON DELETE CASCADE,capability_code TEXT NOT NULL,PRIMARY KEY(collection_id,capability_code));
CREATE TABLE IF NOT EXISTS glossary_asset_links(id INTEGER PRIMARY KEY,term_id INTEGER NOT NULL REFERENCES glossary(id) ON DELETE CASCADE,asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,field_path TEXT NOT NULL DEFAULT '',relationship TEXT NOT NULL DEFAULT 'Describes',notes TEXT NOT NULL DEFAULT '',UNIQUE(term_id,asset_id,field_path,relationship));''')
# Stable enterprise identifiers are additive: existing numeric keys and relationships remain intact.
ENTITY_TABLES={'assets':'DATA','glossary':'TERM','stewards':'STEW','access_requests':'REQ','marketplace_collections':'COLL','glossary_asset_links':'LINK','glossary_history':'GHIST','access_request_history':'RHIST'}
def migrate_identifiers():
 with connect() as c:
  for table,prefix in ENTITY_TABLES.items():
   cols={r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
   if 'uuid' not in cols:c.execute('ALTER TABLE '+table+' ADD COLUMN uuid TEXT')
   if 'display_id' not in cols:c.execute('ALTER TABLE '+table+' ADD COLUMN display_id TEXT')
   for r in c.execute("SELECT id FROM "+table+" WHERE uuid IS NULL OR uuid='' OR display_id IS NULL OR display_id=''").fetchall():
    old=c.execute('SELECT uuid,display_id FROM '+table+' WHERE id=?',(r['id'],)).fetchone()
    c.execute('UPDATE '+table+' SET uuid=?,display_id=? WHERE id=?',(old['uuid'] or str(uuid.uuid4()),old['display_id'] or (prefix+'-'+str(r['id']).zfill(6)),r['id']))
   c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_'+table+'_uuid ON '+table+'(uuid)')
   c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_'+table+'_display ON '+table+'(display_id)')
  c.execute("""CREATE TABLE IF NOT EXISTS resource_fields (
    id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, display_id TEXT NOT NULL UNIQUE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    field_path TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '',
    UNIQUE(asset_id,field_path))""")
  # HERM capability codes are source-model keys; these deterministic UUIDs survive imports.
  c.execute("""CREATE TABLE IF NOT EXISTS capability_identifiers (
    code TEXT PRIMARY KEY,uuid TEXT NOT NULL UNIQUE,display_id TEXT NOT NULL UNIQUE)""")
  for cap in CAPS:
   code=cap['code']
   c.execute('INSERT OR IGNORE INTO capability_identifiers(code,uuid,display_id) VALUES(?,?,?)',(code,str(uuid.uuid5(uuid.NAMESPACE_URL,'rit:herm-brm-v3.2:'+code)),code))
  for a in c.execute('SELECT id,fields_json FROM assets').fetchall():
   sync_resource_fields(c,a['id'],json.loads(a['fields_json'] or '[]'))

def sync_resource_fields(c,asset_id,fields):
 # Preserve identifiers for existing field paths when metadata changes.
 paths=set()
 for i,f in enumerate(fields):
  if isinstance(f,dict):
   name=str(f.get('name') or f.get('field') or f.get('column') or f.get('field_path') or '').strip()
   path=str(f.get('field_path') or name).strip()
   description=str(f.get('description') or '')
  else:name=str(f).strip();path=name;description=''
  if not path or path in paths:continue
  paths.add(path)
  existing=c.execute('SELECT id FROM resource_fields WHERE asset_id=? AND field_path=?',(asset_id,path)).fetchone()
  if existing:c.execute('UPDATE resource_fields SET name=?,description=? WHERE id=?',(name,description,existing['id']))
  else:
   field_id=c.execute('INSERT INTO resource_fields(uuid,display_id,asset_id,field_path,name,description) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),'TEMP-'+str(uuid.uuid4()),asset_id,path,name,description)).lastrowid
   c.execute('UPDATE resource_fields SET display_id=? WHERE id=?',('FIELD-'+str(field_id).zfill(6),field_id))
 # Keep removed fields in the registry to preserve identity and historical links.

def enrich_fields(c,asset_id,fields):
 rows={r['field_path']:dict(r) for r in c.execute('SELECT uuid,display_id,field_path FROM resource_fields WHERE asset_id=?',(asset_id,))}
 result=[]
 for f in fields:
  d=dict(f) if isinstance(f,dict) else {'name':str(f)}
  path=str(d.get('field_path') or d.get('name') or d.get('field') or d.get('column') or '').strip()
  if path in rows:d.update({'uuid':rows[path]['uuid'],'display_id':rows[path]['display_id']})
  result.append(d)
 return result

def entity_identifiers():
 migrate_identifiers()
 with connect() as c:
  result={table:[dict(r) for r in c.execute('SELECT id,uuid,display_id FROM '+table+' ORDER BY id')] for table in ENTITY_TABLES}
  result['capabilities']=[dict(r) for r in c.execute('SELECT code,uuid,display_id FROM capability_identifiers ORDER BY code')]
  result['fields']=[dict(r) for r in c.execute('SELECT id,uuid,display_id,asset_id,field_path,name FROM resource_fields ORDER BY id')]
  return result

# Unified metadata graph. Canonical links are derived from existing tables, while
# user-created relationships are stored separately; existing workflows remain authoritative.
NODE_TABLES={'resource':'assets','field':'resource_fields','term':'glossary','steward':'stewards','collection':'marketplace_collections','request':'access_requests','capability':'capability_identifiers'}
REL_TYPES={'Describes','Contains','Supports','Stewarded by','Related to','Requests','Derived from','Uses','Governs'}
def graph_init():
 with connect() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS metadata_relationships (
    id INTEGER PRIMARY KEY,uuid TEXT NOT NULL UNIQUE,source_uuid TEXT NOT NULL,
    target_uuid TEXT NOT NULL,relationship TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,UNIQUE(source_uuid,target_uuid,relationship))""")
  c.execute('CREATE INDEX IF NOT EXISTS idx_graph_src ON metadata_relationships(source_uuid)')
  c.execute('CREATE INDEX IF NOT EXISTS idx_graph_dst ON metadata_relationships(target_uuid)')
def metadata_graph():
 migrate_identifiers();graph_init()
 with connect() as c:
  nodes=[];by_key={};by_uuid={}
  for typ,table in NODE_TABLES.items():
   rows=c.execute('SELECT * FROM '+table).fetchall()
   for r in rows:
    d=dict(r);key=d.get('code') if typ=='capability' else d['id'];uid=d['uuid']
    name=(BYCODE.get(key,{}).get('name') or key) if typ=='capability' else (d.get('name') or d.get('term') or d.get('title') or d.get('field_path') or d.get('requestor_name') or str(key))
    node={'uuid':uid,'display_id':d['display_id'],'type':typ,'name':str(name),'status':d.get('status',''),'classification':d.get('classification','')}
    nodes.append(node);by_key[(typ,key)]=uid;by_uuid[uid]=node
  edges=[];seen=set()
  def add(source,target,rel,origin='existing',notes='',eid=None):
   a=by_key.get(source) if isinstance(source,tuple) else source
   b=by_key.get(target) if isinstance(target,tuple) else target
   if not a or not b or a not in by_uuid or b not in by_uuid:return
   key=(a,b,rel)
   if key in seen:return
   seen.add(key);edges.append({'id':eid,'source_uuid':a,'target_uuid':b,'relationship':rel,'origin':origin,'notes':notes})
  for r in c.execute('SELECT id,asset_id FROM resource_fields'):add(('resource',r['asset_id']),('field',r['id']),'Contains')
  for table,src_type,src_col in [('asset_capabilities','resource','asset_id'),('glossary_capabilities','term','term_id'),('steward_capabilities','steward','steward_id'),('marketplace_collection_capabilities','collection','collection_id'),('access_request_capabilities','request','request_id')]:
   for r in c.execute('SELECT * FROM '+table):add((src_type,r[src_col]),('capability',r['capability_code']),'Supports')
  for r in c.execute('SELECT * FROM marketplace_collection_assets'):add(('collection',r['collection_id']),('resource',r['asset_id']),'Contains')
  for r in c.execute('SELECT * FROM glossary_asset_links'):
   field=c.execute('SELECT id FROM resource_fields WHERE asset_id=? AND field_path=?',(r['asset_id'],r['field_path'])).fetchone() if r['field_path'] else None
   add(('term',r['term_id']),('field',field['id']) if field else ('resource',r['asset_id']),r['relationship'] if r['relationship'] in REL_TYPES else 'Describes','existing',r['notes'])
  for r in c.execute('SELECT id,steward_id FROM marketplace_collections WHERE steward_id IS NOT NULL'):add(('collection',r['id']),('steward',r['steward_id']),'Stewarded by')
  for r in c.execute('SELECT id,steward_id FROM access_requests WHERE steward_id IS NOT NULL'):add(('request',r['id']),('steward',r['steward_id']),'Stewarded by')
  for r in c.execute('SELECT * FROM metadata_relationships'):add(r['source_uuid'],r['target_uuid'],r['relationship'],'manual',r['notes'],r['id'])
  return {'nodes':nodes,'edges':edges,'relationship_types':sorted(REL_TYPES)}
def add_graph_link(d):
 graph=metadata_graph();ids={x['uuid'] for x in graph['nodes']}
 a=str(d.get('source_uuid',''));b=str(d.get('target_uuid',''));rel=str(d.get('relationship',''))
 if a not in ids or b not in ids or a==b:raise ValueError('Choose two different existing metadata items')
 if rel not in REL_TYPES:raise ValueError('Unsupported relationship type')
 notes=str(d.get('notes',''))[:2000]
 with connect() as c:
  c.execute('INSERT OR IGNORE INTO metadata_relationships(uuid,source_uuid,target_uuid,relationship,notes,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),a,b,rel,notes,now()))
  r=c.execute('SELECT id FROM metadata_relationships WHERE source_uuid=? AND target_uuid=? AND relationship=?',(a,b,rel)).fetchone()
 return {'id':r['id']}
def remove_graph_link(d):
 with connect() as c:
  c.execute('DELETE FROM metadata_relationships WHERE id=?',(int(d['id']),))
  if not c.execute('SELECT changes()').fetchone()[0]:raise ValueError('Relationship not found or managed by existing metadata workflow')
 return {'deleted':True}

def now():return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
def codes(value):
 if isinstance(value,list):v=value
 else:v=re.split(r'[;,|]',str(value or ''))
 return list(dict.fromkeys(str(x).strip().upper() for x in v if str(x).strip()))
def validate_codes(v):
 out=codes(v);bad=[x for x in out if x not in BYCODE]
 if bad:raise ValueError('Unknown capability code(s): '+', '.join(bad))
 return out
def all_terms(public=False):
 migrate_identifiers()
 with connect() as c:
  rows=c.execute('SELECT * FROM glossary '+('WHERE status=? ' if public else '')+'ORDER BY term COLLATE NOCASE',('Published',) if public else ()).fetchall();out=[]
  for r in rows:
   d=dict(r);d['capability_codes']=[x[0] for x in c.execute('SELECT capability_code FROM glossary_capabilities WHERE term_id=?',(r['id'],))];d['capability_ids']=[BYCODE[x]['id'] for x in d['capability_codes'] if x in BYCODE];d['linked_resources']=[dict(x) for x in c.execute('SELECT l.id AS link_id,l.asset_id,a.name AS asset_name,a.type AS asset_type,a.system AS source_system,a.is_published,l.field_path,l.relationship,l.notes FROM glossary_asset_links l JOIN assets a ON a.id=l.asset_id WHERE l.term_id=? ORDER BY a.name,l.field_path',(r['id'],)) if (not public or x['is_published'])];d['history']=[dict(x) for x in c.execute('SELECT old_status,new_status,actor,changed_at FROM glossary_history WHERE term_id=? ORDER BY id',(r['id'],))];out.append(d)
  return out
def all_assets(public=False):
 migrate_identifiers()
 with connect() as c:
  rows=c.execute('SELECT * FROM assets '+('WHERE is_published=1 ' if public else '')+'ORDER BY name COLLATE NOCASE').fetchall();out=[]
  for r in rows:
   d=dict(r);d['is_published']=bool(d['is_published']);d['fields']=enrich_fields(c,r['id'],json.loads(d.pop('fields_json') or '[]'));d['linked_terms']=[dict(x) for x in c.execute('SELECT l.id AS link_id,l.term_id,g.term,g.definition,g.status,l.field_path,l.relationship,l.notes FROM glossary_asset_links l JOIN glossary g ON g.id=l.term_id WHERE l.asset_id=?'+(" AND g.status='Published'" if public else '')+' ORDER BY g.term',(r['id'],))];d['capability_codes']=[x[0] for x in c.execute('SELECT capability_code FROM asset_capabilities WHERE asset_id=?',(r['id'],))];d['capability_code']=d['capability_codes'][0] if d['capability_codes'] else '';d['capability_id']=BYCODE[d['capability_code']]['id'] if d['capability_code'] in BYCODE else None;out.append(d)
  return out
def all_stewards():
 migrate_identifiers()
 with connect() as c:
  rows=[dict(x) for x in c.execute('SELECT * FROM stewards ORDER BY name COLLATE NOCASE')]
  for r in rows:r['capability_codes']=[x[0] for x in c.execute('SELECT capability_code FROM steward_capabilities WHERE steward_id=?',(r['id'],))]
  return rows

def migrate_request_use_cases():
 with connect() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS access_request_use_cases (
    id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, display_id TEXT NOT NULL UNIQUE,
    request_id INTEGER NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,
    name TEXT NOT NULL, description TEXT NOT NULL, integration_hub_topic TEXT DEFAULT '',
    population_scope TEXT DEFAULT '', frequency TEXT DEFAULT '', retention TEXT DEFAULT '',
    permitted_uses TEXT DEFAULT '', prohibited_uses TEXT DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0
  )""")
  # Legacy free-text requests are retained as one named use case, without loss of source text.
  for r in c.execute('SELECT id,use_cases,retention FROM access_requests WHERE NOT EXISTS (SELECT 1 FROM access_request_use_cases WHERE request_id=access_requests.id)').fetchall():
   if not (r['use_cases'] or '').strip():continue
   uid=str(uuid.uuid4())
   new_id=c.execute('INSERT INTO access_request_use_cases(uuid,display_id,request_id,name,description,retention) VALUES(?,?,?,?,?,?)',
    (uid,'TEMP-'+uid,r['id'],'Legacy use case',r['use_cases'],r['retention'] or '')).lastrowid
   c.execute('UPDATE access_request_use_cases SET display_id=? WHERE id=?',('USE-'+str(new_id).zfill(6),new_id))

def save_request_use_cases(c,request_id,items):
 if not isinstance(items,list) or not items:raise ValueError('Add at least one use case')
 if len(items)>50:raise ValueError('Maximum 50 use cases per record')
 clean=[]
 for i,item in enumerate(items):
  if not isinstance(item,dict):raise ValueError('Invalid use case')
  name=str(item.get('name','')).strip();description=str(item.get('description','')).strip()
  if not name or not description:raise ValueError(f'Use case {i+1}: name and description required')
  vals=[str(item.get(k,'')).strip() for k in ('integration_hub_topic','population_scope','frequency','retention','permitted_uses','prohibited_uses')]
  if any(len(v)>4000 for v in [name,description,*vals]):raise ValueError('Use case value too long')
  clean.append((name,description,*vals,i))
 c.execute('DELETE FROM access_request_use_cases WHERE request_id=?',(request_id,))
 for row in clean:
  uid=str(uuid.uuid4())
  new_id=c.execute('INSERT INTO access_request_use_cases(uuid,display_id,request_id,name,description,integration_hub_topic,population_scope,frequency,retention,permitted_uses,prohibited_uses,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
   (uid,'TEMP-'+uid,request_id,*row)).lastrowid
  c.execute('UPDATE access_request_use_cases SET display_id=? WHERE id=?',('USE-'+str(new_id).zfill(6),new_id))

def all_requests():
 migrate_identifiers()
 migrate_request_use_cases()
 with connect() as c:
  rows=[dict(x) for x in c.execute('SELECT * FROM access_requests ORDER BY id DESC')]
  for r in rows:
   r['use_case_records']=[dict(x) for x in c.execute('SELECT * FROM access_request_use_cases WHERE request_id=? ORDER BY sort_order,id',(r['id'],))]
   r['capability_codes']=[x[0] for x in c.execute('SELECT capability_code FROM access_request_capabilities WHERE request_id=?',(r['id'],))]
   r['history']=[dict(x) for x in c.execute('SELECT old_status,new_status,actor,changed_at,notes FROM access_request_history WHERE request_id=? ORDER BY id',(r['id'],))]
  return rows

def all_collections(public=True):
 migrate_identifiers()
 with connect() as c:
  rows=c.execute('SELECT * FROM marketplace_collections '+("WHERE status='Published' " if public else '')+'ORDER BY title COLLATE NOCASE').fetchall()
  result=[]
  for r in rows:
   d=dict(r)
   d['capability_codes']=[x[0] for x in c.execute('SELECT capability_code FROM marketplace_collection_capabilities WHERE collection_id=?',(r['id'],))]
   d['asset_ids']=[x[0] for x in c.execute('SELECT asset_id FROM marketplace_collection_assets WHERE collection_id=?',(r['id'],))]
   d['resources']=[dict(x) for x in c.execute('SELECT a.id,a.name,a.type,a.system,a.description,a.is_published FROM marketplace_collection_assets m JOIN assets a ON a.id=m.asset_id WHERE m.collection_id=?'+(' AND a.is_published=1' if public else ''),(r['id'],))]
   result.append(d)
  return result

def save_collection(d):
 title=str(d.get('title','')).strip();summary=str(d.get('summary','')).strip()
 if not title or not summary:raise ValueError('Title and summary are required')
 caps=validate_codes(d.get('capability_codes',[]));assets=[int(x) for x in d.get('asset_ids',[])]
 if not caps:raise ValueError('Select at least one HERM capability')
 status=d.get('status','Draft')
 if status not in ('Draft','Published'):raise ValueError('Invalid collection status')
 fields=['title','summary','description','business_owner','steward_id','classification','quality_notes','refresh_frequency','permitted_uses','restrictions','delivery_method','status']
 vals=[int(d[k]) if k=='steward_id' and d.get(k) else None if k=='steward_id' else str(d.get(k,'')).strip() for k in fields]
 with connect() as c:
  for aid in assets:
   if not c.execute('SELECT 1 FROM assets WHERE id=?',(aid,)).fetchone():raise ValueError('Unknown asset ID '+str(aid))
  if d.get('id'):
   cid=int(d['id'])
   if not c.execute('SELECT 1 FROM marketplace_collections WHERE id=?',(cid,)).fetchone():raise ValueError('Collection not found')
   c.execute('UPDATE marketplace_collections SET '+','.join(k+'=?' for k in fields)+',updated_at=? WHERE id=?',(*vals,now(),cid))
   c.execute('DELETE FROM marketplace_collection_assets WHERE collection_id=?',(cid,));c.execute('DELETE FROM marketplace_collection_capabilities WHERE collection_id=?',(cid,))
  else:
   cid=c.execute('INSERT INTO marketplace_collections('+','.join(fields)+',created_at,updated_at) VALUES('+','.join('?' for _ in fields)+',?,?)',(*vals,now(),now())).lastrowid
  c.executemany('INSERT INTO marketplace_collection_assets VALUES(?,?)',[(cid,x) for x in set(assets)])
  c.executemany('INSERT INTO marketplace_collection_capabilities VALUES(?,?)',[(cid,x) for x in caps])
 return cid

def collection_import(data,filename):
 rows=parse_upload(data,filename);prepared=[];errors=[]
 with connect() as c:
  asset_ids={x[0] for x in c.execute('SELECT id FROM assets')}
 for line,row in enumerate(rows,2):
  try:
   r=keyrow(row)
   d={'title':r.get('title',''),'summary':r.get('summary',''),'description':r.get('description',''),'business_owner':r.get('business owner',''),'classification':r.get('classification','Internal'),'quality_notes':r.get('quality notes',''),'refresh_frequency':r.get('refresh frequency',''),'permitted_uses':r.get('permitted uses',''),'restrictions':r.get('restrictions',''),'delivery_method':r.get('delivery method','Contact steward'),'status':'Draft','capability_codes':validate_codes(r.get('capability codes','')),'asset_ids':[int(x) for x in codes(r.get('asset ids',''))]}
   if not d['title'] or not d['summary'] or not d['capability_codes']:raise ValueError('Title, Summary and Capability Codes required')
   if any(x not in asset_ids for x in d['asset_ids']):raise ValueError('Unknown asset ID')
   prepared.append(d)
  except (ValueError,TypeError) as e:errors.append('Row '+str(line)+': '+str(e))
 if errors:raise ValueError('; '.join(errors[:15]))
 for d in prepared:save_collection(d)
 return len(prepared)

def record_request(c,rid,old,new,actor,notes=''):
 c.execute('INSERT INTO access_request_history(request_id,old_status,new_status,actor,changed_at,notes) VALUES(?,?,?,?,?,?)',(rid,old,new,actor,now(),notes))

def parse_upload(data,filename):
 if len(data)>8_000_000:raise ValueError('Maximum upload size is 8 MB')
 if filename.lower().endswith('.csv'):
  text=data.decode('utf-8-sig');rows=list(csv.DictReader(io.StringIO(text)))
 elif filename.lower().endswith('.xlsx'):
  wb=load_workbook(io.BytesIO(data),read_only=True,data_only=True);sheet=wb.active;itr=iter(sheet.values);headers=[str(v or '').strip() for v in next(itr)];rows=[dict(zip(headers,row)) for row in itr if any(v is not None and str(v).strip() for v in row)]
 else:raise ValueError('Upload a .xlsx or .csv file')
 if len(rows)>3000:raise ValueError('Maximum 3,000 rows per import')
 return rows
def keyrow(row):return {str(k).strip().lower().replace('_',' ').replace('-',' '):str(v).strip() if v is not None else '' for k,v in row.items() if k is not None}
def glossary_import(data,filename):
 raw=parse_upload(data,filename);clean=[];errors=[]
 for idx,row in enumerate(raw,2):
  r=keyrow(row)
  try:
   term=r.get('term','');definition=r.get('definition','');submitter=r.get('submitted by') or r.get('submitter') or 'Bulk import'
   if not term or not definition:raise ValueError('Term and Definition are required')
   cs=validate_codes(r.get('capability codes') or r.get('capabilities'))
   if not cs:raise ValueError('At least one Capability Code is required')
   clean.append((term,definition,r.get('context',''),submitter,r.get('steward',''),cs))
  except ValueError as e:errors.append(f'Row {idx}: {e}')
 if errors:raise ValueError('; '.join(errors[:20]))
 with connect() as c:
  for term,definition,context,submitter,steward,cs in clean:
   cur=c.execute('INSERT INTO glossary(term,definition,context,submitted_by,steward,status,created_at) VALUES(?,?,?,?,?,?,?)',(term,definition,context,submitter,steward,'Created',now()));tid=cur.lastrowid
   c.executemany('INSERT INTO glossary_capabilities VALUES(?,?)',[(tid,x) for x in cs]);c.execute('INSERT INTO glossary_history(term_id,old_status,new_status,actor,changed_at) VALUES(?,?,?,?,?)',(tid,None,'Created',submitter,now()))
 return len(clean)
def glossary_mapping_import(data,filename):
 raw=parse_upload(data,filename)
 if not raw:raise ValueError('Import file has no data rows')
 prepared=[];errors=[];seen=set()
 with connect() as c:
  terms=[dict(r) for r in c.execute('SELECT id,term FROM glossary')]
  assets=[dict(r) for r in c.execute('SELECT id,name,type,system FROM assets')]
  for line,row in enumerate(raw,2):
   r=keyrow(row)
   try:
    term_id=r.get('term id','');term_name=r.get('term','') or r.get('glossary term','')
    if term_id:
     matches=[x for x in terms if str(x['id'])==term_id]
    else:
     if not term_name:raise ValueError('Term ID or Term required')
     matches=[x for x in terms if x['term'].casefold()==term_name.casefold()]
    if len(matches)!=1:raise ValueError('Glossary term not found or ambiguous; supply Term ID')
    asset_id=r.get('resource id','') or r.get('asset id','');asset_name=r.get('resource name','') or r.get('asset name','')
    if asset_id:amatches=[x for x in assets if str(x['id'])==asset_id]
    else:
     if not asset_name:raise ValueError('Resource ID or Resource Name required')
     amatches=[x for x in assets if x['name'].casefold()==asset_name.casefold()]
     if r.get('resource type'):amatches=[x for x in amatches if x['type'].casefold()==r['resource type'].casefold()]
     if r.get('source system'):amatches=[x for x in amatches if x['system'].casefold()==r['source system'].casefold()]
    if len(amatches)!=1:raise ValueError('Resource not found or ambiguous; supply Resource ID')
    relationship=r.get('relationship') or 'Describes'
    if relationship not in ('Describes','Calculates','Displayed in','Derived from','Related to'):raise ValueError('Invalid relationship: '+relationship)
    field=r.get('field path') or r.get('column or measure') or ''
    notes=r.get('notes','')
    if len(field)>250 or len(notes)>1000:raise ValueError('Field Path or Notes too long')
    key=(matches[0]['id'],amatches[0]['id'],field,relationship)
    if key in seen:raise ValueError('Duplicate mapping within import file')
    seen.add(key);prepared.append((*key,notes))
   except ValueError as e:errors.append(f'Row {line}: {e}')
  if errors:raise ValueError('; '.join(errors[:25]))
  before=c.total_changes
  c.executemany('INSERT OR IGNORE INTO glossary_asset_links(term_id,asset_id,field_path,relationship,notes) VALUES(?,?,?,?,?)',prepared)
  added=c.total_changes-before
 return {'processed':len(prepared),'imported':added,'existing':len(prepared)-added}

def resource_import(data,filename):
 rows=parse_upload(data,filename);clean=[];errors=[]
 for i,row in enumerate(rows,2):
  r=keyrow(row)
  try:
   if not r.get('name'):raise ValueError('Name required')
   typ=r.get('type','Dataset');
   if typ not in TYPES:raise ValueError('Invalid type: '+typ)
   cs=validate_codes(r.get('capability codes'))
   if not cs:raise ValueError('Capability Codes required')
   clean.append((r,typ,cs))
  except ValueError as e:errors.append(f'Row {i}: {e}')
 if errors:raise ValueError('; '.join(errors[:20]))
 with connect() as c:
  for r,typ,cs in clean:
   cur=c.execute('INSERT INTO assets(name,type,description,system,owner,refresh_frequency,classification,access_url,access_instructions) VALUES(?,?,?,?,?,?,?,?,?)',(r['name'],typ,r.get('description',''),r.get('source system',''),r.get('owner',''),r.get('refresh frequency',''),r.get('classification','Internal'),r.get('access url',''),r.get('access instructions','')))
   c.executemany('INSERT INTO asset_capabilities VALUES(?,?)',[(cur.lastrowid,x) for x in cs])
 return len(clean)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def send(self,body,status=200,mime='application/json'):
  if isinstance(body,(dict,list)):body=json.dumps(body).encode()
  elif isinstance(body,str):body=body.encode()
  self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
 def fail(self,msg,status=400):self.send({'error':str(msg)},status)
 def do_GET(self):
  path=urllib.parse.urlsplit(self.path).path
  if path in ('/','/admin','/glossary','/glossary-admin','/herm_capabilities.json','/api/template','/api/glossary/template','/api/glossary-mappings/template','/stewards','/access-requests','/marketplace','/marketplace-admin','/api/marketplace/template','/relationships','/menu'):
   name={'/':'index.html','/admin':'admin.html','/glossary':'glossary.html','/glossary-admin':'glossary-admin.html','/herm_capabilities.json':'herm_capabilities.json','/api/template':'resource-import-template.xlsx','/api/glossary/template':'glossary-import-template.xlsx','/api/glossary-mappings/template':'glossary-mapping-import-template.xlsx','/stewards':'stewards.html','/access-requests':'access-requests.html','/marketplace':'marketplace.html','/marketplace-admin':'marketplace-admin.html','/api/marketplace/template':'marketplace-import-template.xlsx','/relationships':'relationships.html','/menu':'menu.html'}[path]
   mime='text/html; charset=utf-8' if name.endswith('.html') else 'application/json' if name.endswith('.json') else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
   self.send((ROOT/name).read_bytes(),mime=mime);return
  if path=='/api/marketplace':self.send(all_collections(True));return
  if path=='/api/marketplace/admin':self.send(all_collections(False));return
  if path=='/api/stewards':self.send(all_stewards());return
  if path=='/api/access-requests':self.send(all_requests());return
  if path=='/api/catalog':self.send({'capabilities':[{**cap,'uuid':str(uuid.uuid5(uuid.NAMESPACE_URL,'rit:herm-brm-v3.2:'+cap['code'])),'display_id':cap['code']} for cap in CAPS],'assets':all_assets(True),'terms':all_terms(True)});return
  if path=='/api/identifiers':self.send(entity_identifiers());return
  if path=='/api/relationships':self.send(metadata_graph());return
  if path=='/api/admin/assets':self.send(all_assets());return
  if path=='/api/glossary':self.send(all_terms());return
  self.fail('Not found',404)
 def do_POST(self):
  try:
   n=int(self.headers.get('Content-Length','0'))
   if n>8_000_000:raise ValueError('Upload too large')
   data=self.rfile.read(n);path=urllib.parse.urlsplit(self.path).path
   if path in ('/api/admin/import','/api/glossary/import','/api/glossary-mappings/import','/api/marketplace/import'):
    filename=pathlib.Path(self.headers.get('X-Filename','')).name
    if path=='/api/marketplace/import':self.send({'imported':collection_import(data,filename),'status':'Draft'});return
    if path=='/api/glossary-mappings/import':self.send(glossary_mapping_import(data,filename));return
    count=resource_import(data,filename) if path=='/api/admin/import' else glossary_import(data,filename)
    self.send({'imported':count,'status':'Created' if 'glossary' in path else 'Unpublished'});return
   d=json.loads(data.decode('utf-8'))
   if path=='/api/relationships/add':self.send(add_graph_link(d));return
   if path=='/api/relationships/delete':self.send(remove_graph_link(d));return
   if path=='/api/marketplace/admin':self.send({'id':save_collection(d)});return
   if path=='/api/stewards':
    name=str(d.get('name','')).strip();email=str(d.get('email','')).strip();cs=validate_codes(d.get('capability_codes',[]))
    if not name or not cs:raise ValueError('Steward name and at least one capability required')
    if email and ('@' not in email or len(email)>254):raise ValueError('Invalid email')
    with connect() as c:
     vals=(name,email,str(d.get('department','')),str(d.get('role','Data Steward')),int(bool(d.get('active',True))),str(d.get('notes','')))
     if d.get('id'):
      sid=int(d['id']);c.execute('UPDATE stewards SET name=?,email=?,department=?,role=?,active=?,notes=? WHERE id=?',(*vals,sid));c.execute('DELETE FROM steward_capabilities WHERE steward_id=?',(sid,))
     else:sid=c.execute('INSERT INTO stewards(name,email,department,role,active,notes) VALUES(?,?,?,?,?,?)',vals).lastrowid
     c.executemany('INSERT INTO steward_capabilities VALUES(?,?)',[(sid,x) for x in cs])
    self.send({'id':sid});return
   if path=='/api/access-requests':
    migrate_request_use_cases()
    title=str(d.get('title','')).strip();requestor=str(d.get('requestor_name','')).strip();owner=str(d.get('business_owner','')).strip()
    if not title or not requestor or not owner:raise ValueError('Title, requestor, and business owner required')
    cs=validate_codes(d.get('capability_codes',[]));sid=int(d['steward_id']) if d.get('steward_id') else None
    if not cs:raise ValueError('At least one capability required')
    cases=d.get('use_case_records')
    if not isinstance(cases,list) or not cases:raise ValueError('At least one named use case is required')
    d['use_cases']='\n'.join(str(x.get('name',''))+': '+str(x.get('description','')) for x in cases if isinstance(x,dict))
    required=['purpose','use_cases','data_requested','creation_process','source_limitations','usage_restrictions']
    if any(not str(d.get(k,'')).strip() for k in required):raise ValueError('Complete purpose, use cases, requested data, creation process, limitations and restrictions')
    with connect() as c:
     if sid and not c.execute('SELECT 1 FROM stewards WHERE id=? AND active=1',(sid,)).fetchone():raise ValueError('Choose an active steward')
     fields=['title','requestor_name','requestor_email','department','purpose','use_cases','data_requested','intended_audience','sharing_details','retention','start_date','end_date','business_owner','source_system','creation_process','source_fields','source_limitations','usage_restrictions','classification','steward_id','policy_reference','policy_version']
     vals=[sid if k=='steward_id' else str(d.get(k,'')).strip() for k in fields]
     if d.get('id'):
      rid=int(d['id']);old=c.execute('SELECT status FROM access_requests WHERE id=?',(rid,)).fetchone()
      if not old or old['status']!='Draft':raise ValueError('Only draft requests can be edited')
      c.execute('UPDATE access_requests SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*vals,rid));c.execute('DELETE FROM access_request_capabilities WHERE request_id=?',(rid,))
     else:
      rid=c.execute('INSERT INTO access_requests('+','.join(fields)+',created_at) VALUES('+','.join('?' for _ in fields)+',?)',(*vals,now())).lastrowid
      record_request(c,rid,None,'Draft',requestor)
     c.executemany('INSERT INTO access_request_capabilities VALUES(?,?)',[(rid,x) for x in cs])
     save_request_use_cases(c,rid,cases)
    self.send({'id':rid,'status':'Draft'});return
   if path=='/api/access-requests/ack':
    rid=int(d['id']);who=d.get('party');actor=str(d.get('actor','')).strip()
    if who not in ('requestor','owner') or not actor or not d.get('acknowledged'):raise ValueError('Party, name and affirmative acknowledgement required')
    with connect() as c:
     r=c.execute('SELECT status FROM access_requests WHERE id=?',(rid,)).fetchone()
     if not r or r['status'] not in ('Draft','Submitted'):raise ValueError('Acknowledgement only permitted before review')
     c.execute('UPDATE access_requests SET '+who+'_ack=1,'+who+'_ack_name=?,'+who+'_ack_at=? WHERE id=?',(actor,now(),rid))
     record_request(c,rid,r['status'],r['status'],actor,who+' policy acknowledgement')
    self.send({'acknowledged':True});return
   if path=='/api/access-requests/transition':
    rid=int(d['id']);actor=str(d.get('actor','')).strip();target=d.get('status');notes=str(d.get('notes','')).strip()
    if not actor:raise ValueError('Actor name required')
    allowed={'Draft':['Submitted'],'Submitted':['Under Steward Review'],'Under Steward Review':['Approved','Returned for Revision','Denied'],'Returned for Revision':['Submitted']}
    with connect() as c:
     r=c.execute('SELECT * FROM access_requests WHERE id=?',(rid,)).fetchone()
     if not r or target not in allowed.get(r['status'],[]):raise ValueError('Invalid status transition')
     if target=='Submitted' and (not r['requestor_ack'] or not r['owner_ack']):raise ValueError('Requestor and business owner must acknowledge policy before submission')
     if target in ('Approved','Denied','Returned for Revision') and not notes:raise ValueError('Review notes are required for a decision')
     if target=='Under Steward Review' and not r['steward_id']:raise ValueError('Assign a steward before review')
     if target in ('Approved','Denied','Returned for Revision'):
      c.execute('UPDATE access_requests SET status=?,review_date=?,reviewed_by=?,review_notes=?,decision_date=? WHERE id=?',(target,now(),actor,notes,now() if target!='Returned for Revision' else '',rid))
     else:c.execute('UPDATE access_requests SET status=? WHERE id=?',(target,rid))
     record_request(c,rid,r['status'],target,actor,notes)
    self.send({'id':rid,'status':target});return
   if path=='/api/glossary':
    cs=validate_codes(d.get('capability_codes') or [BYID[int(x)]['code'] for x in d.get('capability_ids',[])])
    if not cs or not d.get('term','').strip() or not d.get('definition','').strip() or not d.get('submitted_by','').strip():raise ValueError('Term, definition, submitter and capabilities required')
    with connect() as c:
     cur=c.execute('INSERT INTO glossary(term,definition,context,submitted_by,steward,status,created_at) VALUES(?,?,?,?,?,?,?)',(d['term'].strip(),d['definition'].strip(),d.get('context',''),d['submitted_by'].strip(),d.get('steward',''),'Created',now()));tid=cur.lastrowid
     c.executemany('INSERT INTO glossary_capabilities VALUES(?,?)',[(tid,x) for x in cs]);c.execute('INSERT INTO glossary_history(term_id,old_status,new_status,actor,changed_at) VALUES(?,?,?,?,?)',(tid,None,'Created',d['submitted_by'],now()))
    self.send({'id':tid,'status':'Created'});return
   if path=='/api/glossary/link':
    tid=int(d['term_id']);aid=int(d['asset_id']);field=str(d.get('field_path','')).strip();relationship=str(d.get('relationship','Describes')).strip();notes=str(d.get('notes','')).strip()
    if relationship not in ('Describes','Calculates','Displayed in','Derived from','Related to'):raise ValueError('Invalid relationship')
    if len(field)>250 or len(notes)>1000:raise ValueError('Field or notes too long')
    with connect() as c:
     if not c.execute('SELECT 1 FROM glossary WHERE id=?',(tid,)).fetchone():raise ValueError('Glossary term not found')
     if not c.execute('SELECT 1 FROM assets WHERE id=?',(aid,)).fetchone():raise ValueError('Resource not found')
     c.execute('INSERT OR IGNORE INTO glossary_asset_links(term_id,asset_id,field_path,relationship,notes) VALUES(?,?,?,?,?)',(tid,aid,field,relationship,notes))
    self.send({'linked':True});return
   if path=='/api/glossary/unlink':
    lid=int(d['link_id']);tid=int(d['term_id'])
    with connect() as c:
     result=c.execute('DELETE FROM glossary_asset_links WHERE id=? AND term_id=?',(lid,tid))
    self.send({'removed':result.rowcount>0});return
   if path=='/api/glossary/transition':
    tid=int(d['id']);actor=str(d.get('actor','')).strip();target=d.get('status');
    if not actor:raise ValueError('Actor required')
    with connect() as c:
     old=c.execute('SELECT status FROM glossary WHERE id=?',(tid,)).fetchone()
     if not old:raise ValueError('Term not found')
     idx=STATES.index(old['status'])
     if idx>=3 or target!=STATES[idx+1]:raise ValueError('Invalid workflow transition')
     c.execute('UPDATE glossary SET status=? WHERE id=?',(target,tid));c.execute('INSERT INTO glossary_history(term_id,old_status,new_status,actor,changed_at) VALUES(?,?,?,?,?)',(tid,old['status'],target,actor,now()))
    self.send({'id':tid,'status':target});return
   if path=='/api/admin/assets':
    name=str(d.get('name','')).strip();typ=d.get('type','Dataset')
    if not name or typ not in TYPES:raise ValueError('Name and valid type required')
    cs=validate_codes(d.get('capability_codes') or ([BYID[int(d['capability_id'])]['code']] if d.get('capability_id') else []))
    fields=d.get('fields',[])
    if not isinstance(fields,list):raise ValueError('Invalid fields')
    vals=(name,typ,d.get('description',''),d.get('system',''),d.get('owner',''),d.get('refresh_frequency',''),d.get('classification','Internal'),d.get('access_url',''),d.get('access_instructions',''),int(bool(d.get('is_published'))),json.dumps(fields))
    with connect() as c:
     if d.get('id'):
      aid=int(d['id']);c.execute('UPDATE assets SET name=?,type=?,description=?,system=?,owner=?,refresh_frequency=?,classification=?,access_url=?,access_instructions=?,is_published=?,fields_json=? WHERE id=?',(*vals,aid));c.execute('DELETE FROM asset_capabilities WHERE asset_id=?',(aid,))
     else:aid=c.execute('INSERT INTO assets(name,type,description,system,owner,refresh_frequency,classification,access_url,access_instructions,is_published,fields_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',vals).lastrowid
     c.executemany('INSERT INTO asset_capabilities VALUES(?,?)',[(aid,x) for x in cs]);sync_resource_fields(c,aid,fields)
    self.send({'id':aid});return
   self.fail('Not found',404)
  except (ValueError,KeyError,IndexError,TypeError,json.JSONDecodeError) as e:self.fail(e)
  except Exception as e:self.fail('Internal error: '+str(e),500)
if __name__=='__main__':
 init()
 migrate_identifiers()
 graph_init()
 print('RIT Data Discovery Portal v1.6 — local-only prototype: http://127.0.0.1:8765')
 print('Press Control+C to stop. Do not expose this server to a network.')
 ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
