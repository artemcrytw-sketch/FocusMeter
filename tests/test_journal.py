import runpy, tempfile, tkinter as tk
from pathlib import Path
from types import SimpleNamespace
m=runpy.run_path('FocusMeter.pyw',run_name='focusmeter_test')
g=m['Store'].__init__.__globals__
with tempfile.TemporaryDirectory() as folder:
    g.update(DB_PATH=folder+'/ui.db',ICON_DIR=folder+'/icons',APP_DIR=folder,LOG_PATH=folder+'/error.log')
    root=tk.Tk(); root.withdraw()
    store=m['Store']()
    app=SimpleNamespace(root=root,store=store,icons=m['IconCache'](),logo_tk=m['ImageTk'].PhotoImage(m['make_logo']()),ico_path=None,tracker=SimpleNamespace(paused=False,idle=False),toggle_pause=lambda:None,dash=None)
    for theme in ('light','dark'):
        store.set('theme',theme)
        dash=m['Dashboard'](app); app.dash=dash
        for page in dash.pages:
            dash.show(page); root.update()
            assert dash.pages[page].winfo_exists()
        dash.show("today")
        assert not dash.duration_labels
        store.add("ChatGPT","journal test","",65)
        dash.refresh()
        root.update()
        hour = m["datetime"].now().hour
        label = dash.duration_labels[hour,"ChatGPT"]
        assert label.cget("text") == "1 мин"
        assert label.winfo_ismapped() and label.winfo_width() > 20
        store.add("ChatGPT","journal test","",60)
        dash.refresh()
        assert label.cget("text") == "2 мин"
        assert dash.hour_seconds[hour] == 125
        for h, name, secs in [(0,"Early",60),(23,"Late",61),(23,"Brief",59)]:
            store.db.execute("INSERT INTO hourly_usage VALUES(?,?,?,?)",
                             (m["date"].today().isoformat(),h,name,secs))
        store.db.commit()
        dash.refresh()
        assert (23,"Brief") not in dash.duration_labels
        assert (0,"Early") in dash.duration_labels
        assert list(dash.duration_labels)[0][0] == 23
        assert list(dash.duration_labels)[-1][0] == 0
        assert dash.hour_seconds[23] >= 120
        for key in ("wrapped","settings","today"):
            dash.nav[key][0].invoke()
            assert dash.page == key
        yesterday = m["date"].today() - m["timedelta"](days=1)
        store.db.execute("INSERT INTO usage VALUES(?,?,?,?)",(yesterday.isoformat(),"Past","",180))
        store.db.execute("INSERT INTO hourly_usage VALUES(?,?,?,?)",(yesterday.isoformat(),9,"Past",180))
        store.db.commit()
        dash.show("week")
        root.update()
        x0,y0,x1,y1,_ = dash._hits[-2]
        dash._chart_day(SimpleNamespace(x=(x0+x1)/2,y=(y0+y1)/2))
        assert dash.page == "today" and dash.journal_day == yesterday
        assert list(dash.duration_labels) == [(9,"Past")]
        dash._shift_day(1)
        assert dash.journal_day is None and str(dash.next_day["state"]) == "disabled"
        store.db.execute("DELETE FROM hourly_categories")
        store.db.execute("DELETE FROM hourly_usage")
        store.db.commit()
        dash.refresh()
        assert "общий итог" in dash.timeline.inner.winfo_children()[0].cget("text")
        store.db.execute("DELETE FROM usage")
        store.db.commit()
        dash.close()
    root.destroy(); store.db.close()
print('PASS: journal, minute threshold, hour order and navigation verified in both themes')
