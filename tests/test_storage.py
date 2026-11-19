import ast, sqlite3, threading, tempfile, os
from pathlib import Path
from datetime import date, datetime, timedelta
source=Path('FocusMeter.pyw').read_text(encoding='utf-8')
tree=ast.parse(source)
ns=dict(sqlite3=sqlite3,threading=threading,os=os,date=date,datetime=datetime,timedelta=timedelta)
with tempfile.TemporaryDirectory() as folder:
    ns.update(DB_PATH=folder+'/test.db',ICON_DIR=folder+'/icons')
    nodes=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in ('Store','csv_text')]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'FocusMeter.pyw','exec'),ns)
    s=ns['Store']()
    s.add('test','title','test.exe',2.5)
    s.db.execute("CREATE TRIGGER fail_hour BEFORE INSERT ON hourly_usage BEGIN SELECT RAISE(ABORT,'test failure'); END")
    s.db.commit()
    try: s.flush()
    except sqlite3.IntegrityError: pass
    else: raise AssertionError('failure expected')
    assert s.db.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert sum(s.buf.values())==2.5
    s.db.execute('DROP TRIGGER fail_hour'); s.db.commit()
    s.flush(); s.flush()
    assert s.db.execute('SELECT SUM(seconds) FROM usage').fetchone()[0]==2
    assert s.db.execute('SELECT SUM(seconds) FROM hourly_usage').fetchone()[0]==2
    s.add('test','title','test.exe',0.5); s.flush()
    assert s.db.execute('SELECT SUM(seconds) FROM usage').fetchone()[0]==3
    assert ns['csv_text']('=1+1')=="'=1+1"
    assert ns['csv_text']('обычный текст')=='обычный текст'
    s.db.close()
print('PASS: rollback, retry, no duplicates, fractional seconds, CSV protection')
