from shadow.db import Database
from shadow.signals import Det
from shadow.strategies import DEFAULT_PARAMS, load_strategies, new_version, rollback


def det(*names):
    from shadow.signals import DETECTORS
    return {k: Det(k, 1, k in names, 0.8 if k in names else 0.0) for k in DETECTORS}


def test_defaults_seeded_once_and_versioned(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    st = load_strategies(db)
    assert set(st) == {"early_entry", "first_pullback", "migration_breakout"}
    assert all(s.version == 1 for s in st.values())
    assert st["early_entry"].applies("new") and not st["early_entry"].applies("mcap")
    assert st["first_pullback"].applies("mcap") and st["first_pullback"].applies("volume")
    load_strategies(db)
    assert db.con.execute("SELECT count(*) FROM strategy_params").fetchone()[0] == 3
    p = dict(DEFAULT_PARAMS["first_pullback"]["params"])
    p["mcap"] = dict(p["mcap"], tp_pct=12.0)
    v = new_version(db, "first_pullback", p, "learned", "tp 10->12")
    assert v == 2 and load_strategies(db)["first_pullback"].p("mcap")["tp_pct"] == 12.0
    row = db.active_strategy_params("first_pullback")
    assert row["parent_version"] == 1 and row["source"] == "learned"
    rollback(db, "first_pullback", 1)
    assert load_strategies(db)["first_pullback"].version == 1
    db.close()


def test_entry_conditions(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    st = load_strategies(db)
    ee, fp, mb = st["early_entry"], st["first_pullback"], st["migration_breakout"]
    assert ee.entry_conditions("new", det("early_pump", "buy_sell_pressure"), {"age_min": 10, "ret_60m": 40})[0]
    assert not ee.entry_conditions("new", det("early_pump"), {"age_min": 10})[0]            # needs pressure
    assert not ee.entry_conditions("new", det("early_pump", "buy_sell_pressure"), {"age_min": 90})[0]
    assert not ee.entry_conditions("new", det("early_pump", "buy_sell_pressure", "sudden_dump"), {"age_min": 5})[0]
    assert not ee.entry_conditions("new", det("early_pump", "buy_sell_pressure"), {"age_min": 5, "ret_60m": 400})[0]
    assert fp.entry_conditions("mcap", det("first_pullback"), {})[0]
    assert mb.entry_conditions("new", det("migration_breakout"), {"since_migration_min": 5})[0]
    assert not mb.entry_conditions("new", det("migration_breakout"), {"since_migration_min": 500})[0]
    lv = fp.levels("mcap", 1.0, {"pullback_low": 0.95})
    assert abs(lv["sl"] - 0.95 * 0.985) < 1e-9 and abs(lv["tp"] - 1.10) < 1e-9 and lv["time_stop_min"] == 120
    lv = fp.levels("mcap", 1.0, {"pullback_low": 0.5})                                        # SL capped at 8%
    assert abs(lv["sl"] - 0.92) < 1e-9
    db.close()
