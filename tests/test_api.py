import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from addie.api import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "bank_sample.csv"
PASSWORD = "correct horse battery"


@pytest.fixture
def make_client(tmp_path):
    def _make():
        return TestClient(create_app(tmp_path / "a.db", tmp_path / "a.salt", auto_lock_minutes=15))
    return _make


def unlocked(client, setup=True):
    r = client.post("/api/setup" if setup else "/api/unlock", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["X-Session"] = r.json()["token"]
    return client


def test_requires_auth_and_rejects_short_password(make_client):
    c = make_client()
    assert c.get("/api/summary").status_code == 401
    assert c.post("/api/setup", json={"password": "short"}).status_code == 400


def test_database_file_is_encrypted(make_client, tmp_path):
    c = unlocked(make_client())
    c.post("/api/accounts", json={"name": "חשבון סודי", "type": "CHECKING"})
    raw = (tmp_path / "a.db").read_bytes()
    assert not raw.startswith(b"SQLite format 3")
    assert "חשבון סודי".encode() not in raw


def test_wrong_password_and_reopen(make_client, monkeypatch):
    monkeypatch.setattr("addie.api.time.sleep", lambda s: None)
    c = unlocked(make_client())
    c.post("/api/accounts", json={"name": "עו\"ש", "type": "CHECKING"})
    c.post("/api/lock")
    assert c.get("/api/accounts").status_code == 401

    c2 = make_client()
    assert c2.post("/api/unlock", json={"password": "wrong password!!"}).status_code == 401
    unlocked(c2, setup=False)
    assert [a["name"] for a in c2.get("/api/accounts").json()] == ['עו"ש']


def test_import_categorize_dedup_and_summary(make_client):
    c = unlocked(make_client())
    acct = c.post("/api/accounts", json={"name": "עו\"ש", "type": "CHECKING"}).json()["id"]

    preview = c.post("/api/import/preview", files={"file": ("b.csv", FIXTURE.read_bytes())}).json()
    assert preview["detected"]

    def do_import():
        return c.post("/api/import", data={"account_id": acct, "mapping": json.dumps(preview["mapping"])},
                      files={"file": ("b.csv", FIXTURE.read_bytes())}).json()

    first = do_import()
    assert first["inserted"] == 5 and first["duplicates"] == 0  # both identical coffees kept
    assert do_import() == {"inserted": 0, "duplicates": 5, "skipped_rows": 1}

    cats = {x["name"]: x["id"] for x in c.get("/api/categories").json()}
    c.put(f"/api/budgets/{cats['מזון וסופר']}", json={"monthly_amount": 300})

    s = c.get("/api/summary?month=2026-09").json()
    assert s["income"] == 1250000
    assert s["savings"] == 200000
    assert s["uncategorized"] == 0
    food = next(x for x in s["categories"] if x["name"] == "מזון וסופר")
    assert food["spent"] == 41230 and food["budget"] == 30000
    eating = next(x for x in s["categories"] if x["name"] == "אוכל בחוץ")
    assert eating["spent"] == 3600
    assert s["expenses"] == 41230 + 3600


def test_recategorize_with_remember_creates_rule(make_client):
    c = unlocked(make_client())
    acct = c.post("/api/accounts", json={"name": "כרטיס", "type": "CARD"}).json()["id"]
    csv = "תאריך,תיאור,סכום\n01/09/2026,חנות הספרים,-50\n02/09/2026,חנות הספרים,-30\n".encode()
    mapping = c.post("/api/import/preview", files={"file": ("c.csv", csv)}).json()["mapping"]
    c.post("/api/import", data={"account_id": acct, "mapping": json.dumps(mapping)}, files={"file": ("c.csv", csv)})
    txs = c.get("/api/transactions?uncategorized=true").json()
    assert len(txs) == 2
    cat = next(x["id"] for x in c.get("/api/categories").json() if x["name"] == "קניות")
    c.patch(f"/api/transactions/{txs[0]['id']}", json={"category_id": cat, "remember": True})
    assert c.get("/api/transactions?uncategorized=true").json() == []


def test_goals_projection(make_client):
    c = unlocked(make_client())
    gid = c.post("/api/goals", json={"name": "קרן חירום", "target_amount": 30000,
                                     "current_amount": 10000, "monthly_contribution": 3000}).json()["id"]
    c.post(f"/api/goals/{gid}/contribute", json={"amount": 2000})
    g = c.get("/api/goals").json()[0]
    assert g["current_amount"] == 1200000 and g["months_left"] == 6 and g["progress"] == 0.4


def test_security_headers_and_host_check(make_client):
    c = make_client()
    r = c.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/status", headers={"host": "evil.example"}).status_code == 400
