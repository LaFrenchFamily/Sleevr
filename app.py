"""Ma Collection de cartes — serveur Flask + SQLite.

Pokémon (pokemontcg.io) et Yu-Gi-Oh! (YGOPRODeck), prix Cardmarket en EUR,
historique quotidien des prix et variations 1j / 7j / 30j.
"""
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from functools import wraps

import requests
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "cartes.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
POKEMON_API_KEY = os.environ.get("POKEMONTCG_API_KEY", "")
REFRESH_HOUR = int(os.environ.get("REFRESH_HOUR", "6"))  # heure locale du relevé quotidien
HTTP_TIMEOUT = 25

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = SECRET_KEY or os.urandom(32).hex()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # une photo de carte (JPEG réduit) fait quelques centaines de Ko

# photos recto/verso de tes cartes : data/photos/<n° de ligne de collection>_recto.jpg (ou _verso.jpg)
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
os.makedirs(PHOTO_DIR, exist_ok=True)
SIDES = ("recto", "verso")


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="Photo trop volumineuse"), 413

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,              -- 'pokemon' | 'yugioh'
    api_id TEXT NOT NULL,            -- id chez l'API (unique par jeu)
    name TEXT NOT NULL,
    set_name TEXT,
    number TEXT,                     -- n° dans l'extension / code (ex. LOB-FR001)
    rarity TEXT,
    image TEXT,
    UNIQUE (game, api_id)
);
CREATE TABLE IF NOT EXISTS collection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 1,
    condition TEXT NOT NULL DEFAULT 'NM',
    notes TEXT DEFAULT '',
    added_at TEXT NOT NULL,
    custom_price REAL,                -- prix fixé par toi (EUR), pour cette ligne
    use_custom INTEGER NOT NULL DEFAULT 0,  -- 1 = on utilise ton prix au lieu de celui du marché
    UNIQUE (card_id, condition)
);
CREATE TABLE IF NOT EXISTS prices (
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    day TEXT NOT NULL,               -- AAAA-MM-JJ
    price REAL,                      -- prix « tendance » Cardmarket (EUR)
    avg7 REAL,
    avg30 REAL,
    src_date TEXT,                   -- date de mise à jour du prix chez la source (Cardmarket)
    PRIMARY KEY (card_id, day)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ----------------------------------------------------------------- base de données
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def standalone_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    db = standalone_db()
    db.executescript(SCHEMA)
    # migration : base créée avant l'ajout de src_date
    cols = [r["name"] for r in db.execute("PRAGMA table_info(prices)")]
    if "src_date" not in cols:
        db.execute("ALTER TABLE prices ADD COLUMN src_date TEXT")
    # migration : prix personnel
    kcols = [r["name"] for r in db.execute("PRAGMA table_info(collection)")]
    if "custom_price" not in kcols:
        db.execute("ALTER TABLE collection ADD COLUMN custom_price REAL")
    if "use_custom" not in kcols:
        db.execute("ALTER TABLE collection ADD COLUMN use_custom INTEGER NOT NULL DEFAULT 0")
    db.commit()
    db.close()


# ----------------------------------------------------------------- authentification
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if APP_PASSWORD and not session.get("ok"):
            return jsonify(error="non connecté"), 401
        return fn(*args, **kwargs)

    return wrapper


@app.post("/api/login")
def login():
    pwd = (request.get_json(silent=True) or {}).get("password", "")
    if not APP_PASSWORD or hmac.compare_digest(pwd, APP_PASSWORD):
        session.permanent = True
        session["ok"] = True
        return jsonify(ok=True)
    return jsonify(error="mot de passe incorrect"), 403


@app.get("/api/session")
def session_state():
    return jsonify(ok=bool(not APP_PASSWORD or session.get("ok")), protected=bool(APP_PASSWORD))


# ----------------------------------------------------------------- sources de prix
def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def parse_money(v):
    """Prix saisi à la main : accepte « 12,50 », « 12.5 », « 12,50 € ». Renvoie un float >= 0, None si vide ; ValueError si invalide."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
    else:
        t = str(v).replace("€", "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
        if t == "":
            return None
        f = float(t)
    if f != f or f in (float("inf"), float("-inf")) or f < 0 or f > 10_000_000:
        raise ValueError("prix invalide")
    return round(f, 2)


class SourceError(Exception):
    """Source de prix en panne ou trop lente (message lisible pour l'utilisateur)."""


RETRY_DELAY = 1.5  # secondes, multiplié par le n° de tentative


def get_with_retry(url, source, attempts=3, **kwargs):
    """GET avec relances : les API gratuites répondent de temps en temps 500/502/503/504 ou expirent."""
    last = "pas de réponse"
    for n in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
            if r.status_code < 500 and r.status_code != 429:
                return r
            last = f"erreur {r.status_code}"
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = "délai dépassé" if isinstance(exc, requests.Timeout) else "connexion impossible"
        if n < attempts:
            time.sleep(RETRY_DELAY * n)
    raise SourceError(f"{source} ne répond pas correctement ({last}). Réessaie dans un instant.")


def _pokemon_normalize(c):
    cmk = c.get("cardmarket") or {}
    cm = cmk.get("prices") or {}
    return {
        "game": "pokemon",
        "api_id": c["id"],
        "name": c.get("name", "?"),
        "set_name": (c.get("set") or {}).get("name"),
        "number": f'{c.get("number", "")}/{(c.get("set") or {}).get("printedTotal", "")}'.strip("/"),
        "rarity": c.get("rarity"),
        "image": (c.get("images") or {}).get("small"),
        "price": _num(cm.get("trendPrice")) or _num(cm.get("averageSellPrice")),
        "avg7": _num(cm.get("avg7")),
        "avg30": _num(cm.get("avg30")),
        "src_date": (cmk.get("updatedAt") or "").replace("/", "-") or None,
    }


def _norm(s):
    """Minuscules, sans accents ni ponctuation : « Salamèche » -> « salameche », « M. Mime » -> « m mime »."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _load_fr_names():
    """Dictionnaire nom français -> nom anglais des Pokémon (pokemontcg.io ne connaît que l'anglais)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pokemon_fr.json"), encoding="utf-8") as f:
            return {_norm(fr): en for fr, en in json.load(f)["pairs"]}
    except (OSError, ValueError, KeyError):
        return {}


FR_TO_EN = _load_fr_names()


def pokemon_name_to_english(name):
    """« Dracaufeu GX » -> « Charizard GX ». Les mots qui ne sont pas un nom de Pokémon restent inchangés."""
    words = (name or "").split()
    out, i = [], 0
    while i < len(words):
        for size in (3, 2, 1):  # noms composés d'abord (« Mime Jr. », « Poussifeu »…)
            chunk = _norm(" ".join(words[i:i + size]))
            if len(words[i:i + size]) == size and chunk in FR_TO_EN:
                out.append(FR_TO_EN[chunk])
                i += size
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def _load_en_names():
    """Dictionnaire nom anglais normalisé -> nom français des Pokémon (pour l'affichage)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pokemon_fr.json"), encoding="utf-8") as f:
            out = {}
            for fr, en in json.load(f)["pairs"]:
                out.setdefault(_norm(en), fr)
            return out
    except (OSError, ValueError, KeyError):
        return {}


EN_TO_FR = _load_en_names()
# préfixes anglais -> (texte placé APRÈS le nom du Pokémon). « Dark Charizard » -> « Dracaufeu obscur ».
NAME_MODIFIERS = [
    ("team rocket s", "de la Team Rocket"), ("rocket s", "de la Team Rocket"),
    ("alolan", "d'Alola"), ("galarian", "de Galar"), ("hisuian", "de Hisui"), ("paldean", "de Paldea"),
    ("dark", "obscur"), ("light", "lumineux"), ("shining", "brillant"), ("radiant", "Radieux"),
]


def pokemon_name_to_french(name):
    """« Charizard VMAX » -> « Dracaufeu VMAX ». Les cartes sans nom de Pokémon (Dresseur, Énergie) restent inchangées."""
    words = (name or "").split()
    if not words:
        return name
    suffix, mega = "", False
    for _ in range(3):  # plusieurs préfixes possibles, ex. « Dark Alolan … »
        if words and words[0].lower() == "mega" and len(words) > 1:
            mega, words = True, words[1:]
            continue
        for key, txt in NAME_MODIFIERS:
            n = next((k for k in (1, 2) if len(words) > k and _norm(" ".join(words[:k])) == key), 0)
            if n:
                words = words[n:]
                suffix = (suffix + " " + txt).strip()
                break
        else:
            break
    for i in range(len(words)):
        for size in (3, 2, 1):
            chunk = " ".join(words[i:i + size])
            if len(words[i:i + size]) == size and _norm(chunk) in EN_TO_FR:
                fr = EN_TO_FR[_norm(chunk)]
                if mega:
                    fr = "Méga-" + fr
                parts = words[:i] + [fr] + ([suffix] if suffix else []) + words[i + size:]
                return " ".join(parts)
    return name


# on ne demande que les champs utiles : réponse bien plus légère, donc moins de 500 côté pokemontcg.io
POKEMON_FIELDS = "id,name,number,rarity,set,images,cardmarket"


def fetch_pokemon(name=None, number=None, api_id=None):
    headers = {"X-Api-Key": POKEMON_API_KEY} if POKEMON_API_KEY else {}
    src = "pokemontcg.io"
    if api_id:
        r = get_with_retry(f"https://api.pokemontcg.io/v2/cards/{api_id}", src, headers=headers,
                           params={"select": POKEMON_FIELDS})
        r.raise_for_status()
        return [_pokemon_normalize(r.json()["data"])]

    name = (name or "").replace('"', "").strip()
    en_name = pokemon_name_to_english(name)  # « Dracaufeu » -> « Charizard » (la base est en anglais)
    num, _, total = (number or "").partition("/")
    num = num.strip().lstrip("0") or ("0" if num.strip() else "")
    total = total.strip()

    suffix = f" number:{num}" if num else ""
    if num and total.isdigit():
        # « 58/102 » : numéro + total imprimé identifient l'extension à coup sûr ;
        # le nom (souvent mal lu sur la photo) ne sert qu'à classer les résultats.
        queries = [f"number:{num} set.printedTotal:{total}"]
    elif name:
        # nom entier (traduit), puis seulement son début : tolère une faute de lecture en fin de mot
        starts = []
        for n in (en_name, name):
            for cut in (len(n), max(4, len(n) - 2), 4):
                s = n[:cut].strip()
                if len(s) >= 3 and s not in starts:
                    starts.append(s)
        queries = [f'name:"{s}*"{suffix}' for s in starts[:4]]
        if not queries and num:  # nom trop court pour être fiable : on s'appuie sur le numéro
            queries = [f"number:{num}"]
    elif num:
        queries = [f"number:{num}"]
    else:
        return []

    cards = []
    for q in queries:
        r = get_with_retry("https://api.pokemontcg.io/v2/cards", src, headers=headers,
                           params={"q": q, "pageSize": 30, "select": POKEMON_FIELDS})
        r.raise_for_status()
        cards = r.json().get("data", [])
        if cards:
            break
    cards.sort(key=lambda c: (c.get("set") or {}).get("releaseDate", ""), reverse=True)  # plus récentes d'abord
    if name:
        wanted = {en_name.lower(), name.lower()}
        cards.sort(key=lambda c: 0 if any(w in c.get("name", "").lower() for w in wanted) else 1)  # noms proches en tête
    return [_pokemon_normalize(c) for c in cards]


def _ygo_entries(c, display_name=None):
    """Une entrée par impression (extension) de la carte, car le prix varie selon l'extension."""
    img = ((c.get("card_images") or [{}])[0]).get("image_url_small")
    c = {**c, "name": display_name or c.get("name", "?")}  # nom français si on l'a
    out = []
    for s in c.get("card_sets") or []:
        out.append(
            {
                "game": "yugioh",
                "api_id": f'{c["id"]}|{s.get("set_code", "")}|{s.get("set_rarity_code", "") or s.get("set_rarity", "")}',
                "name": c.get("name", "?"),
                "set_name": s.get("set_name"),
                "number": s.get("set_code"),
                "rarity": s.get("set_rarity"),
                "image": img,
                "price": _num(s.get("set_price")) or _num(((c.get("card_prices") or [{}])[0]).get("cardmarket_price")),
                "avg7": None,
                "avg30": None,
            }
        )
    if not out:
        out.append(
            {
                "game": "yugioh",
                "api_id": f'{c["id"]}||',
                "name": c.get("name", "?"),
                "set_name": None,
                "number": None,
                "rarity": None,
                "image": img,
                "price": _num(((c.get("card_prices") or [{}])[0]).get("cardmarket_price")),
                "avg7": None,
                "avg30": None,
            }
        )
    return out


def _ygo_clean(code):
    """Majuscules, et une espace lue à la place du tiret (« LOB FR005 ») redevient un tiret."""
    return re.sub(r"[\s_]+", "-", (code or "").strip().upper())


def _ygo_code_key(code):
    """LOB-FR005, LOB-EN005 et LOB-005 désignent la même carte : on compare sans la langue."""
    c = _ygo_clean(code)
    m = re.match(r"^([A-Z0-9]{2,6})-(?:[A-Z]{2})?(\d{2,4})$", c)
    return f"{m.group(1)}-{m.group(2)}" if m else c


def _ygo_get(params):
    r = get_with_retry("https://db.ygoprodeck.com/api/v7/cardinfo.php", "YGOPRODeck", params=params)
    if r.status_code == 400:  # « aucune carte trouvée » chez YGOPRODeck
        return []
    r.raise_for_status()
    return r.json().get("data", [])


def fetch_yugioh(name=None, number=None, api_id=None):
    fr_names = {}
    if api_id:
        cards = _ygo_get({"id": api_id.split("|")[0]})
    elif name:
        # 1) recherche sur les noms français (language=fr) ; 2) sinon recherche classique en anglais.
        # Les prix et extensions sont toujours relus sur la fiche anglaise, la plus complète.
        try:
            fr_cards = _ygo_get({"fname": name, "language": "fr"})[:15]
        except (SourceError, requests.RequestException):
            fr_cards = []
        if fr_cards:
            fr_names = {c["id"]: c.get("name") for c in fr_cards}
            cards = _ygo_get({"id": ",".join(str(i) for i in fr_names)}) or fr_cards
        else:
            cards = _ygo_get({"fname": name})
    elif number:
        # un code type LOB-FR005 : on retrouve la carte via l'endpoint des extensions (le code anglais LOB-EN005 en repli)
        code = _ygo_clean(number)
        lookups = [code]
        m = re.match(r"^([A-Z0-9]{2,6})-[A-Z]{2}(\d{2,4})$", code)
        if m:
            lookups.append(f"{m.group(1)}-EN{m.group(2)}")
        card_name = None
        for setcode in lookups:
            r = get_with_retry("https://db.ygoprodeck.com/api/v7/cardsetsinfo.php", "YGOPRODeck",
                               params={"setcode": setcode})
            if r.status_code == 200 and r.json().get("name"):
                card_name = r.json()["name"]
                break
        if not card_name:
            return []
        cards = _ygo_get({"name": card_name})
    else:
        return []

    entries = []
    for c in cards[:15]:
        entries.extend(_ygo_entries(c, fr_names.get(c.get("id"))))
    if api_id:  # on ne garde que l'impression demandée
        entries = [e for e in entries if e["api_id"] == api_id]
    elif number:
        wanted = _ygo_code_key(number)
        exact = [e for e in entries if _ygo_code_key(e["number"]) == wanted]
        entries = exact or entries
    return entries[:60]


FETCHERS = {"pokemon": fetch_pokemon, "yugioh": fetch_yugioh}


# ----------------------------------------------------------------- prix & variations
def store_price(db, card_id, price, avg7=None, avg30=None, day=None, src_date=None):
    if price is None:
        return
    day = day or date.today().isoformat()
    db.execute(
        "INSERT INTO prices (card_id, day, price, avg7, avg30, src_date) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(card_id, day) DO UPDATE SET price=excluded.price, avg7=excluded.avg7, "
        "avg30=excluded.avg30, src_date=excluded.src_date",
        (card_id, day, price, avg7, avg30, src_date),
    )


def variation(db, card_id, days, current, fallback_avg=None):
    """Variation en % entre le prix actuel et le prix le plus proche d'il y a `days` jours.

    On utilise d'abord notre propre historique ; à défaut, la moyenne Cardmarket sur 7/30 jours
    (disponible tout de suite pour Pokémon), pour ne pas attendre un mois de relevés.
    """
    if current is None:
        return None
    target = (date.today() - timedelta(days=days)).isoformat()
    row = db.execute(
        "SELECT price FROM prices WHERE card_id=? AND day<=? AND price IS NOT NULL ORDER BY day DESC LIMIT 1",
        (card_id, target),
    ).fetchone()
    ref = row["price"] if row else fallback_avg
    if not ref:
        return None
    return round((current - ref) / ref * 100, 1)


def photo_path(cid, side):
    return os.path.join(PHOTO_DIR, f"{int(cid)}_{side}.jpg")


def photo_stamp(cid, side):
    try:
        return int(os.path.getmtime(photo_path(cid, side)))
    except OSError:
        return None


def card_row_with_prices(db, row):
    last = db.execute(
        "SELECT price, avg7, avg30, day, src_date FROM prices WHERE card_id=? AND price IS NOT NULL "
        "ORDER BY day DESC LIMIT 1",
        (row["card_id"],),
    ).fetchone()
    price = last["price"] if last else None
    avg7 = avg30 = None
    if last:
        avg7, avg30 = last["avg7"], last["avg30"]
        try:
            # Les moyennes 7 j / 30 j de la source sont calculées à SA date de mise à jour. Si ce prix date de
            # plusieurs semaines, elles décrivent une période révolue : les afficher comme variation récente
            # serait trompeur (la variation viendra alors de ton propre historique, ou restera vide).
            if last["src_date"] and (date.fromisoformat(last["day"]) - date.fromisoformat(last["src_date"])).days > 3:
                avg7 = avg30 = None
        except ValueError:
            pass
    d = dict(row)
    d["name_fr"] = pokemon_name_to_french(row["name"]) if row["game"] == "pokemon" else row["name"]
    market_price = price
    use_custom = bool(row["use_custom"]) and row["custom_price"] is not None
    if use_custom:
        price = row["custom_price"]
        avg7 = avg30 = None
    d.update(
        photo_recto=photo_stamp(row["id"], "recto"),  # None = pas de photo ; sinon horodatage (sert à rafraîchir l'image)
        photo_verso=photo_stamp(row["id"], "verso"),
        price=price,
        market_price=market_price,
        use_custom=use_custom,
        price_source="perso" if use_custom else "marche",
        price_day=last["day"] if last else None,
        src_date=last["src_date"] if last else None,
        total=round(price * row["quantity"], 2) if price is not None else None,
        var1=None if use_custom else variation(db, row["card_id"], 1, price),
        var7=None if use_custom else variation(db, row["card_id"], 7, price, avg7),
        var30=None if use_custom else variation(db, row["card_id"], 30, price, avg30),
    )
    return d


def refresh_prices():
    """Relève les prix de toutes les cartes de la collection. Renvoie (ok, erreurs)."""
    db = standalone_db()
    ok, errors = 0, 0
    try:
        cards = db.execute(
            "SELECT DISTINCT c.id, c.game, c.api_id FROM cards c JOIN collection k ON k.card_id=c.id"
        ).fetchall()
        for c in cards:
            try:
                res = FETCHERS[c["game"]](api_id=c["api_id"])
                if res:
                    store_price(db, c["id"], res[0]["price"], res[0]["avg7"], res[0]["avg30"],
                                src_date=res[0].get("src_date"))
                    ok += 1
                else:
                    errors += 1
            except Exception as exc:  # une carte en erreur ne bloque pas les autres
                app.logger.warning("prix %s %s: %s", c["game"], c["api_id"], exc)
                errors += 1
            time.sleep(0.15)  # on reste poli avec les API
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('last_refresh', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        db.commit()
    finally:
        db.close()
    return ok, errors


_refresh_lock = threading.Lock()


def refresh_guarded():
    if not _refresh_lock.acquire(blocking=False):
        return None
    try:
        return refresh_prices()
    finally:
        _refresh_lock.release()


def scheduler_loop():
    """Une fois par jour, après REFRESH_HOUR, si le relevé du jour n'est pas fait."""
    while True:
        try:
            now = datetime.now()
            db = standalone_db()
            row = db.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
            db.close()
            last = datetime.fromisoformat(row["value"]).date() if row else None
            if now.hour >= REFRESH_HOUR and last != now.date():
                refresh_guarded()
        except Exception as exc:
            app.logger.warning("planificateur: %s", exc)
        time.sleep(1800)


# ----------------------------------------------------------------- API
@app.get("/api/search")
@login_required
def search():
    game = request.args.get("game", "pokemon")
    if game not in FETCHERS:
        return jsonify(error="jeu inconnu"), 400
    name = (request.args.get("q") or "").strip()
    number = (request.args.get("number") or "").strip()
    try:
        results = FETCHERS[game](name=name or None, number=number or None)
    except SourceError as exc:
        return jsonify(error=str(exc)), 502
    except requests.RequestException:
        return jsonify(error="Source de prix injoignable. Réessaie dans un instant."), 502
    if game == "pokemon":
        for r in results:
            r["name_fr"] = pokemon_name_to_french(r.get("name"))
    return jsonify(results=results)


@app.post("/api/collection")
@login_required
def add_to_collection():
    p = request.get_json(force=True)
    if p.get("game") not in FETCHERS or not p.get("api_id") or not p.get("name"):
        return jsonify(error="carte invalide"), 400
    qty = max(1, int(p.get("quantity", 1)))
    cond = p.get("condition", "NM")
    try:
        custom = parse_money(p.get("custom_price"))
    except ValueError:
        return jsonify(error="prix personnel invalide"), 400
    use_custom = 1 if p.get("use_custom") else 0
    if use_custom and custom is None:
        return jsonify(error="indique ton prix ou désactive l'option"), 400
    db = get_db()
    db.execute(
        "INSERT INTO cards (game, api_id, name, set_name, number, rarity, image) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(game, api_id) DO NOTHING",
        (p["game"], p["api_id"], p["name"], p.get("set_name"), p.get("number"), p.get("rarity"), p.get("image")),
    )
    card_id = db.execute("SELECT id FROM cards WHERE game=? AND api_id=?", (p["game"], p["api_id"])).fetchone()["id"]
    # si la ligne existe déjà (même carte, même état), on ajoute la quantité ; le prix perso n'est écrasé que s'il est fourni
    db.execute(
        "INSERT INTO collection (card_id, quantity, condition, added_at, custom_price, use_custom) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(card_id, condition) DO UPDATE SET quantity = quantity + excluded.quantity"
        + (", custom_price = excluded.custom_price, use_custom = excluded.use_custom" if use_custom else ""),
        (card_id, qty, cond, datetime.now().isoformat(timespec="seconds"), custom, use_custom),
    )
    store_price(db, card_id, _num(p.get("price")), _num(p.get("avg7")), _num(p.get("avg30")),
                src_date=p.get("src_date"))
    db.commit()
    entry_id = db.execute("SELECT id FROM collection WHERE card_id=? AND condition=?", (card_id, cond)).fetchone()["id"]
    return jsonify(ok=True, card_id=card_id, entry_id=entry_id)


COLLECTION_SQL = (
    "SELECT k.id, k.card_id, k.quantity, k.condition, k.notes, k.added_at, k.custom_price, k.use_custom, "
    "c.game, c.name, c.set_name, c.number, c.rarity, c.image "
    "FROM collection k JOIN cards c ON c.id = k.card_id"
)


@app.get("/api/collection")
@login_required
def list_collection():
    db = get_db()
    game = request.args.get("game")
    sql = COLLECTION_SQL + (" WHERE c.game=?" if game in FETCHERS else "") + " ORDER BY c.name COLLATE NOCASE"
    rows = db.execute(sql, (game,) if game in FETCHERS else ()).fetchall()
    items = [card_row_with_prices(db, r) for r in rows]
    last = db.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
    return jsonify(items=items, last_refresh=last["value"] if last else None)


@app.patch("/api/collection/<int:cid>")
@login_required
def update_entry(cid):
    p = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT * FROM collection WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="introuvable"), 404
    qty = max(1, int(p.get("quantity", row["quantity"])))
    cond = p.get("condition", row["condition"])
    notes = p.get("notes", row["notes"])
    custom = row["custom_price"]
    if "custom_price" in p:
        try:
            custom = parse_money(p["custom_price"])
        except ValueError:
            return jsonify(error="prix personnel invalide"), 400
    use_custom = int(bool(p["use_custom"])) if "use_custom" in p else row["use_custom"]
    if use_custom and custom is None:
        return jsonify(error="indique ton prix ou désactive l'option"), 400
    try:
        db.execute("UPDATE collection SET quantity=?, condition=?, notes=?, custom_price=?, use_custom=? WHERE id=?",
                   (qty, cond, notes, custom, use_custom, cid))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="cette carte existe déjà dans cet état"), 409
    return jsonify(ok=True)


@app.delete("/api/collection/<int:cid>")
@login_required
def delete_entry(cid):
    db = get_db()
    db.execute("DELETE FROM collection WHERE id=?", (cid,))
    db.commit()
    for side in SIDES:  # les photos de cette carte partent avec elle
        try:
            os.remove(photo_path(cid, side))
        except OSError:
            pass
    return jsonify(ok=True)


@app.put("/api/collection/<int:cid>/photo/<side>")
@login_required
def put_photo(cid, side):
    if side not in SIDES:
        return jsonify(error="côté inconnu (recto ou verso)"), 404
    if not get_db().execute("SELECT 1 FROM collection WHERE id=?", (cid,)).fetchone():
        return jsonify(error="carte introuvable"), 404
    data = request.get_data(cache=False)
    if len(data) < 100 or data[:3] != b"\xff\xd8\xff":  # signature d'un fichier JPEG
        return jsonify(error="Photo JPEG attendue"), 415
    path = photo_path(cid, side)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)  # remplacement d'un coup : jamais de photo à moitié écrite
    return jsonify(ok=True, stamp=photo_stamp(cid, side))


@app.get("/api/collection/<int:cid>/photo/<side>")
@login_required
def get_photo(cid, side):
    if side not in SIDES or not os.path.exists(photo_path(cid, side)):
        return jsonify(error="pas de photo"), 404
    return send_file(photo_path(cid, side), mimetype="image/jpeg", max_age=86400)


@app.delete("/api/collection/<int:cid>/photo/<side>")
@login_required
def delete_photo(cid, side):
    if side not in SIDES:
        return jsonify(error="côté inconnu (recto ou verso)"), 404
    try:
        os.remove(photo_path(cid, side))
    except OSError:
        pass
    return jsonify(ok=True)


@app.get("/api/card/<int:card_id>/history")
@login_required
def history(card_id):
    rows = get_db().execute(
        "SELECT day, price FROM prices WHERE card_id=? AND price IS NOT NULL ORDER BY day", (card_id,)
    ).fetchall()
    return jsonify(points=[dict(r) for r in rows])


@app.get("/api/summary")
@login_required
def summary():
    """Valeur totale de la collection et son évolution jour par jour."""
    db = get_db()
    rows = db.execute(
        "SELECT k.quantity, k.card_id, k.custom_price, k.use_custom FROM collection k"
    ).fetchall()
    n_cards = sum(r["quantity"] for r in rows)
    n_unique = len({r["card_id"] for r in rows})
    # lignes au prix personnel : montant fixe ; les autres suivent le marché
    fixed = round(sum(r["custom_price"] * r["quantity"] for r in rows
                      if r["use_custom"] and r["custom_price"] is not None), 2)
    qty = {}
    for r in rows:
        if not (r["use_custom"] and r["custom_price"] is not None):
            qty[r["card_id"]] = qty.get(r["card_id"], 0) + r["quantity"]
    if not qty:
        return jsonify(total=fixed, cards=n_cards, unique=n_unique, history=[], var1=None, var7=None, var30=None)
    marks = ",".join("?" * len(qty))
    price_rows = db.execute(
        f"SELECT card_id, day, price FROM prices WHERE card_id IN ({marks}) AND price IS NOT NULL ORDER BY day",
        list(qty),
    ).fetchall()
    days = sorted({r["day"] for r in price_rows})
    last_price = {}
    by_day = {d: {} for d in days}
    for r in price_rows:
        by_day[r["day"]][r["card_id"]] = r["price"]
    history_pts = []
    for d in days:  # report du dernier prix connu pour que la courbe ne chute pas si une carte manque un jour
        last_price.update(by_day[d])
        history_pts.append({"day": d, "total": round(fixed + sum(last_price.get(c, 0) * q for c, q in qty.items()), 2)})
    total = history_pts[-1]["total"] if history_pts else fixed

    def total_var(n):
        target = (date.today() - timedelta(days=n)).isoformat()
        past = [p for p in history_pts if p["day"] <= target]
        if not past or not past[-1]["total"]:
            return None
        return round((total - past[-1]["total"]) / past[-1]["total"] * 100, 1)

    return jsonify(
        total=total,
        cards=n_cards,
        unique=n_unique,
        history=history_pts,
        var1=total_var(1),
        var7=total_var(7),
        var30=total_var(30),
    )


@app.post("/api/refresh")
@login_required
def manual_refresh():
    res = refresh_guarded()
    if res is None:
        return jsonify(error="un relevé est déjà en cours"), 409
    return jsonify(ok=res[0], errors=res[1])


@app.get("/api/export.csv")
@login_required
def export_csv():
    import csv
    import io

    db = get_db()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Jeu", "Nom", "Extension", "Numéro", "Rareté", "État", "Quantité", "Prix unitaire (EUR)", "Total (EUR)", "Prix"])
    for r in db.execute(COLLECTION_SQL + " ORDER BY c.game, c.name COLLATE NOCASE").fetchall():
        d = card_row_with_prices(db, r)
        w.writerow([d["game"], d["name_fr"], d["set_name"], d["number"], d["rarity"], d["condition"],
                    d["quantity"], d["price"], d["total"],
                    "perso" if d["use_custom"] else "marché"])
    return app.response_class(
        "﻿" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ma-collection.csv"},
    )


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


init_db()

if os.environ.get("DISABLE_SCHEDULER") != "1":
    threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
