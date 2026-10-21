"""Sources de prix Magic et One Piece, avec des réponses d'API simulées (pas de réseau)."""
import pytest

import app as sleevr


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sleevr.requests.HTTPError(str(self.status_code))


BOLT = {
    "id": "abc", "name": "Lightning Bolt", "set": "m11", "set_name": "Magic 2011", "collector_number": "149",
    "rarity": "common", "finishes": ["nonfoil", "foil"], "image_uris": {"small": "https://img/bolt.jpg"},
    "prices": {"eur": "1.20", "eur_foil": "4.50"},
}
BOLT_FR = {**BOLT, "id": "fr1", "lang": "fr", "printed_name": "Foudre", "prices": {"eur": None}}
ZORO = {"card_set_id": "OP01-001", "card_image_id": "OP01-001", "card_name": "Roronoa Zoro (001)",
        "set_name": "Romance Dawn", "rarity": "L", "market_price": 2.0, "inventory_price": 1.9,
        "card_image": "https://img/zoro.jpg", "date_scraped": "2026-10-20"}
ZORO_P = {**ZORO, "card_image_id": "OP01-001_p1", "card_name": "Roronoa Zoro (001) (Parallel)", "market_price": 30.0}


@pytest.fixture
def http(monkeypatch):
    calls = []
    routes = {}

    def fake_get(url, source, attempts=3, **kw):
        calls.append((url, kw.get("params")))
        for key, fn in routes.items():
            if key in url:
                return fn(url, kw.get("params") or {})
        return FakeResponse(404, {})

    monkeypatch.setattr(sleevr, "get_with_retry", fake_get)
    monkeypatch.setattr(sleevr, "usd_to_eur_rate", lambda: 0.9)
    sleevr._op_cache.update(at=0.0, cards=[])
    return routes, calls


def test_magic_french_name_shows_french_and_prices_english_prints(http):
    routes, calls = http

    def search(url, params):
        if "lang:fr" in params["q"]:
            return FakeResponse(200, {"data": [BOLT_FR]})
        assert '!"Lightning Bolt"' in params["q"]
        return FakeResponse(200, {"data": [BOLT]})

    routes["/cards/search"] = search
    res = sleevr.fetch_magic(name="Foudre")
    assert [r["api_id"] for r in res] == ["abc", "abc|foil"]
    assert res[0]["name"] == "Foudre"
    assert res[0]["price"] == 1.2 and res[1]["price"] == 4.5
    assert res[0]["number"] == "M11 149"


def test_magic_set_and_number(http):
    routes, _ = http
    routes["/cards/m11/149"] = lambda u, p: FakeResponse(200, BOLT)
    res = sleevr.fetch_magic(number="M11 149")
    assert res[0]["set_name"] == "Magic 2011"


def test_magic_refresh_keeps_finish(http):
    routes, _ = http
    routes["/cards/abc"] = lambda u, p: FakeResponse(200, BOLT)
    res = sleevr.fetch_magic(api_id="abc|foil")
    assert len(res) == 1 and res[0]["price"] == 4.5


def test_onepiece_code_converts_usd_to_eur(http):
    routes, _ = http
    routes["/sets/card/OP01-001/"] = lambda u, p: FakeResponse(200, [ZORO, ZORO_P])
    res = sleevr.fetch_onepiece(number="0P01-0O1")
    assert [r["api_id"] for r in res] == ["OP01-001", "OP01-001_p1"]
    assert res[0]["price"] == 1.8 and res[1]["price"] == 27.0


def test_onepiece_refresh_by_variant(http):
    routes, _ = http
    routes["/sets/card/OP01-001/"] = lambda u, p: FakeResponse(200, [ZORO, ZORO_P])
    res = sleevr.fetch_onepiece(api_id="OP01-001_p1")
    assert len(res) == 1 and res[0]["price"] == 27.0


def test_onepiece_name_search_uses_cached_list(http):
    routes, calls = http
    routes["/allSetCards/"] = lambda u, p: FakeResponse(200, [ZORO, ZORO_P])
    routes["/allSTCards/"] = lambda u, p: FakeResponse(200, [])
    routes["/allPromoCards/"] = lambda u, p: FakeResponse(200, [])
    assert len(sleevr.fetch_onepiece(name="zoro")) == 2
    n = len(calls)
    assert len(sleevr.fetch_onepiece(name="Roronoa")) == 2
    assert len(calls) == n  # liste gardée en mémoire


@pytest.mark.parametrize("raw,code", [("OP01-001", "OP01-001"), ("op01 001", "OP01-001"), ("ST10-005", "ST10-005"),
                                      ("P-012", "P-012"), ("EB01-012", "EB01-012"), ("Luffy", None)])
def test_onepiece_code_parsing(raw, code):
    assert sleevr._op_code(raw) == code
