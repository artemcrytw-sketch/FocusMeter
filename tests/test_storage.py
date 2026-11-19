import sqlite3, tempfile


import runpy
module = runpy.run_path("FocusMeter.pyw",run_name="focusmeter_test")
ns = module["Store"].__init__.__globals__
with tempfile.TemporaryDirectory() as folder:
    ns.update(DB_PATH=folder+'/test.db',ICON_DIR=folder+'/icons')
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
