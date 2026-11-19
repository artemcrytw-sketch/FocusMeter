import runpy
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch


module = runpy.run_path("FocusMeter.pyw", run_name="focusmeter_test")
g = module["Store"].__init__.__globals__
with tempfile.TemporaryDirectory() as folder:
    path = str(Path(folder) / "usage.db")
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE blacklist(app TEXT PRIMARY KEY, limit_min INTEGER NOT NULL)")
        old.execute("INSERT INTO blacklist VALUES('Example',1)")
    old.close()
    g.update(DB_PATH=path, ICON_DIR=str(Path(folder) / "icons"), APP_DIR=folder)
    store = g["Store"]()
    work, chat = g["CAT_LIST"][:2]
    assert store.limit_actions["Example"] == "remind"
    store.set_rule("Docs", chat)
    store.set_rule("Google Docs", work)
    store.add("Example", "Project — GOOGLE DOCS", "", 65)
    store.set_rule("Google Docs", chat)
    store.add("Example", "Project — GOOGLE DOCS", "", 70)
    store.flush()
    assert store.categories()[work] == 65
    assert store.categories()[chat] == 70
    store.set_rule("Google Docs", None)
    assert store.classify("Example", "Google Docs") == chat
    assert dict(store.q("SELECT category, SUM(seconds) FROM hourly_categories GROUP BY category")) == {work:65, chat:70}
    # Данные до обновления по-прежнему входят в итоги.
    store.db.execute("INSERT INTO usage VALUES(?,?,?,?)",(date.today().isoformat(),"Example","Old Docs",10))
    assert store.categories()[chat] == 80
    notify = Mock()
    tracker = g["Tracker"](store, notify, Mock())
    with patch.dict(g, app_windows=Mock(return_value=[])):
        tracker._enforce()
        tracker._enforce()
        notify.assert_called_once()
        g["app_windows"].assert_not_called()
        store.bl_set("Example", 1, "close")
        tracker._enforce()
        g["app_windows"].assert_called_once_with("Example")
        store.bl_set("Example", 1)
        tracker._enforce()
        g["app_windows"].assert_called_once()
    store.clear(True)
    assert not store.q("SELECT * FROM hourly_categories")
    assert not store.q("SELECT * FROM usage_categories")
    store.db.close()
print("PASS: legacy limits, reminder and close modes, rule precedence and historical categories")
