"""Sources de prix Magic et One Piece, avec des réponses d'API simulées (pas de réseau)."""
import pytest

import app as holocarte


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise holocarte.requests.HTTPError(str(self.status_code))


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

    monkeypatch.setattr(holocarte, "get_with_retry", fake_get)
    monkeypatch.setattr(holocarte, "usd_to_eur_rate", lambda: 0.9)
    holocarte._op_cache.update(at=0.0, cards=[])
    return routes, calls


def test_magic_french_name_shows_french_and_prices_english_prints(http):
    routes, calls = http

    def search(url, params):
        if "lang:fr" in params["q"]:
            return FakeResponse(200, {"data": [BOLT_FR]})
        assert '!"Lightning Bolt"' in params["q"]
        return FakeResponse(200, {"data": [BOLT]})

    routes["/cards/search"] = search
    res = holocarte.fetch_magic(name="Foudre")
    assert [r["api_id"] for r in res] == ["abc", "abc|foil"]
    assert res[0]["name"] == "Foudre"
    assert res[0]["price"] == 1.2 and res[1]["price"] == 4.5
    assert res[0]["number"] == "M11 149"


def test_magic_set_and_number(http):
    routes, _ = http
    routes["/cards/m11/149"] = lambda u, p: FakeResponse(200, BOLT)
    res = holocarte.fetch_magic(number="M11 149")
    assert res[0]["set_name"] == "Magic 2011"


def test_magic_refresh_keeps_finish(http):
    routes, _ = http
    routes["/cards/abc"] = lambda u, p: FakeResponse(200, BOLT)
    res = holocarte.fetch_magic(api_id="abc|foil")
    assert len(res) == 1 and res[0]["price"] == 4.5


def test_onepiece_code_converts_usd_to_eur(http):
    routes, _ = http
    routes["/sets/card/OP01-001/"] = lambda u, p: FakeResponse(200, [ZORO, ZORO_P])
    res = holocarte.fetch_onepiece(number="0P01-0O1")
    assert [r["api_id"] for r in res] == ["OP01-001", "OP01-001_p1"]
    assert res[0]["price"] == 1.8 and res[1]["price"] == 27.0


def test_onepiece_refresh_by_variant(http):
    routes, _ = http
    routes["/sets/card/OP01-001/"] = lambda u, p: FakeResponse(200, [ZORO, ZORO_P])
    res = holocarte.fetch_onepiece(api_id="OP01-001_p1")
    assert len(res) == 1 and res[0]["price"] == 27.0


def test_onepiece_name_search_uses_cached_list(http):
    routes, calls = http
    routes["/allSetCards/"] = lambda u, p: FakeResponse(200, [ZORO, ZORO_P])
    routes["/allSTCards/"] = lambda u, p: FakeResponse(200, [])
    routes["/allPromoCards/"] = lambda u, p: FakeResponse(200, [])
    assert len(holocarte.fetch_onepiece(name="zoro")) == 2
    n = len(calls)
    assert len(holocarte.fetch_onepiece(name="Roronoa")) == 2
    assert len(calls) == n  # liste gardée en mémoire


@pytest.mark.parametrize("raw,code", [("OP01-001", "OP01-001"), ("op01 001", "OP01-001"), ("ST10-005", "ST10-005"),
                                      ("P-012", "P-012"), ("EB01-012", "EB01-012"), ("Luffy", None)])
def test_onepiece_code_parsing(raw, code):
    assert holocarte._op_code(raw) == code


DRACO = {"id": "base1-4", "localId": "4", "name": "Dracaufeu", "rarity": "Rare", "image": "https://assets.tcgdex.net/fr/base/base1/4",
         "set": {"id": "base1", "name": "Set de Base", "cardCount": {"official": 102, "total": 102}},
         "variants": {"normal": False, "holo": True},
         "pricing": {"cardmarket": {"unit": "EUR", "updated": "2026-10-21T02:00:00Z", "trend": 1.0,
                                    "trend-holo": 420.0, "avg7-holo": 400.0, "avg30-holo": 380.0}}}


def test_pokemon_tcgdex_french_name_and_holo_price(http):
    routes, calls = http

    def cards(url, params):
        if "/fr/cards/base1-4" in url:
            return FakeResponse(200, DRACO)
        assert "/fr/cards" in url and params["name"] == "Dracaufeu"
        return FakeResponse(200, [{"id": "base1-4", "localId": "4", "name": "Dracaufeu"}])

    routes["tcgdex.net/v2/fr/cards"] = cards
    res = holocarte.fetch_pokemon(name="Dracaufeu")
    assert res[0]["name"] == "Dracaufeu" and res[0]["number"] == "4/102"
    assert res[0]["price"] == 420.0 and res[0]["avg7"] == 400.0  # carte holo uniquement : prix holo
    assert res[0]["image"].endswith("/low.webp") and res[0]["src_date"] == "2026-10-21"


def test_pokemon_number_with_total(http):
    routes, _ = http
    routes["/en/sets"] = lambda u, p: FakeResponse(200, [{"id": "base1", "cardCount": {"official": 102}},
                                                          {"id": "swsh3", "cardCount": {"official": 189}}])
    routes["/fr/sets/base1/4"] = lambda u, p: FakeResponse(200, DRACO)
    holocarte._tcgdex_sets.update(at=0.0, sets=[])
    res = holocarte.fetch_pokemon(number="04/102")
    assert [r["api_id"] for r in res] == ["base1-4"]


def test_pokemon_refresh_falls_back_to_pokemontcg_io(http):
    routes, _ = http
    routes["api.pokemontcg.io/v2/cards/sv3pt5-6"] = lambda u, p: FakeResponse(200, {"data": {
        "id": "sv3pt5-6", "name": "Charizard ex", "number": "6", "set": {"name": "151", "printedTotal": 165},
        "cardmarket": {"prices": {"trendPrice": 12.5}}}})
    res = holocarte.fetch_pokemon(api_id="sv3pt5-6")
    assert res[0]["price"] == 12.5
