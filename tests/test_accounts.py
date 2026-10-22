"""Comptes : chacun sa collection, collections visibles par les autres membres, reprise de l'ancienne base."""
import sqlite3

import pytest

import app as holocarte

CARD = {"game": "pokemon", "api_id": "base1-4", "name": "Charizard", "set_name": "Base", "number": "4/102",
        "price": 300.0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(holocarte, "DB_PATH", str(tmp_path / "cartes.db"))
    monkeypatch.setattr(holocarte, "PHOTO_DIR", str(tmp_path))
    monkeypatch.setattr(holocarte, "SIGNUP_CODE", "invite")
    holocarte.init_db()
    holocarte.app.config["TESTING"] = True
    return holocarte.app.test_client


def signup(c, name, code="invite"):
    return c.post("/api/register", json={"username": name, "password": "motdepasse", "code": code})


def test_signup_requires_invite_code(client):
    c = client()
    assert signup(c, "alex", code="faux").status_code == 403
    assert signup(c, "alex").status_code == 200
    assert client().post("/api/login", json={"username": "ALEX", "password": "motdepasse"}).status_code == 200
    assert client().post("/api/login", json={"username": "alex", "password": "nope"}).status_code == 403


def test_each_member_has_own_collection_and_can_view_public_ones(client):
    alex, sam = client(), client()
    signup(alex, "alex")
    signup(sam, "sam")
    entry = alex.post("/api/collection", json=CARD).get_json()["entry_id"]

    assert sam.get("/api/collection").get_json()["items"] == []
    assert sam.patch(f"/api/collection/{entry}", json={"quantity": 5}).status_code == 404
    assert sam.delete(f"/api/collection/{entry}").status_code == 404

    members = {m["username"]: m for m in sam.get("/api/members").get_json()["members"]}
    assert members["alex"]["total"] == 300.0 and members["sam"]["me"]
    view = sam.get(f"/api/members/{members['alex']['id']}").get_json()
    assert [i["name"] for i in view["items"]] == ["Charizard"]

    alex.patch("/api/me", json={"is_public": False})
    assert "alex" not in {m["username"] for m in sam.get("/api/members").get_json()["members"]}
    assert sam.get(f"/api/members/{members['alex']['id']}").status_code == 404


def test_same_card_in_two_collections(client):
    alex, sam = client(), client()
    signup(alex, "alex")
    signup(sam, "sam")
    alex.post("/api/collection", json=CARD)
    sam.post("/api/collection", json=CARD)
    alex.post("/api/collection", json=CARD)
    assert alex.get("/api/collection").get_json()["items"][0]["quantity"] == 2
    assert sam.get("/api/collection").get_json()["items"][0]["quantity"] == 1


def test_old_database_goes_to_first_account(tmp_path, monkeypatch):
    db_path = tmp_path / "cartes.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL, api_id TEXT NOT NULL,
            name TEXT NOT NULL, set_name TEXT, number TEXT, rarity TEXT, image TEXT, UNIQUE (game, api_id));
        CREATE TABLE collection (id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
            condition TEXT NOT NULL DEFAULT 'NM', notes TEXT DEFAULT '', added_at TEXT NOT NULL, UNIQUE (card_id, condition));
        CREATE TABLE prices (card_id INTEGER NOT NULL, day TEXT NOT NULL, price REAL, avg7 REAL, avg30 REAL, PRIMARY KEY (card_id, day));
        INSERT INTO cards (game, api_id, name) VALUES ('pokemon', 'base1-4', 'Charizard');
        INSERT INTO collection (card_id, quantity, condition, added_at) VALUES (1, 3, 'NM', '2026-01-01');
    """)
    db.commit()
    db.close()
    monkeypatch.setattr(holocarte, "DB_PATH", str(db_path))
    monkeypatch.setattr(holocarte, "SIGNUP_CODE", "")
    holocarte.init_db()
    first, second = holocarte.app.test_client(), holocarte.app.test_client()
    signup(first, "alex")
    signup(second, "sam")
    assert first.get("/api/collection").get_json()["items"][0]["quantity"] == 3
    assert second.get("/api/collection").get_json()["items"] == []


def test_daily_backup_keeps_the_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(holocarte, "DB_PATH", str(tmp_path / "cartes.db"))
    monkeypatch.setattr(holocarte, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(holocarte, "BACKUP_KEEP", 2)
    holocarte.init_db()
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        assert holocarte.backup_db(day)
    assert holocarte.backup_db("2026-10-03") is None  # déjà faite aujourd'hui
    kept = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert kept == ["cartes-2026-10-02.db", "cartes-2026-10-03.db"]
    db = sqlite3.connect(tmp_path / "backups" / "cartes-2026-10-03.db")
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='users'").fetchone()[0] == 1
