"""Sleevr — serveur Flask + SQLite.

Pokémon (TCGdex), Yu-Gi-Oh! (YGOPRODeck), Magic (Scryfall) : prix Cardmarket en EUR ;
One Piece (optcgapi.com) : prix TCGplayer converti en EUR.
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
from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "cartes.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# code à donner pour créer un compte (par défaut l'ancien mot de passe de l'appli) ; vide = inscription libre
SIGNUP_CODE = os.environ.get("SIGNUP_CODE", APP_PASSWORD)
SECRET_KEY = os.environ.get("SECRET_KEY", "")
POKEMON_API_KEY = os.environ.get("POKEMONTCG_API_KEY", "")
REFRESH_HOUR = int(os.environ.get("REFRESH_HOUR", "6"))  # heure locale du relevé quotidien
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))  # nombre de sauvegardes quotidiennes de la base gardées
HTTP_TIMEOUT = 25
# Reconnaissance des cartes par l'IA (facultatif) : sans clé, l'appli garde la lecture de texte sur le téléphone
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CARD_AI_MODEL = os.environ.get("CARD_AI_MODEL", "claude-opus-5-5")

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
    game TEXT NOT NULL,              -- 'pokemon' | 'yugioh' | 'magic' | 'onepiece'
    api_id TEXT NOT NULL,            -- id chez l'API (unique par jeu)
    name TEXT NOT NULL,
    set_name TEXT,
    number TEXT,                     -- n° dans l'extension / code (ex. LOB-FR001)
    rarity TEXT,
    image TEXT,
    UNIQUE (game, api_id)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_public INTEGER NOT NULL DEFAULT 1,   -- 1 = les autres membres peuvent voir ta collection
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL : ligne d'avant les comptes, reprise par le 1er compte
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 1,
    condition TEXT NOT NULL DEFAULT 'NM',
    notes TEXT DEFAULT '',
    added_at TEXT NOT NULL,
    custom_price REAL,                -- prix fixé par toi (EUR), pour cette ligne
    use_custom INTEGER NOT NULL DEFAULT 0,  -- 1 = on utilise ton prix au lieu de celui du marché
    UNIQUE (user_id, card_id, condition)
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
    # migration : comptes. La contrainte d'unicité change (une même carte peut être chez plusieurs membres),
    # SQLite oblige à reconstruire la table ; les lignes existantes iront au premier compte créé.
    if "user_id" not in kcols:
        db.execute("ALTER TABLE collection RENAME TO collection_old")
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO collection (id, user_id, card_id, quantity, condition, notes, added_at, custom_price, use_custom) "
            "SELECT id, NULL, card_id, quantity, condition, notes, added_at, custom_price, use_custom FROM collection_old"
        )
        db.execute("DROP TABLE collection_old")
    db.commit()
    db.close()


# ----------------------------------------------------------------- authentification
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return get_db().execute("SELECT id, username, is_public FROM users WHERE id=?", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify(error="non connecté"), 401
        g.uid = user["id"]
        return fn(*args, **kwargs)

    return wrapper


def user_json(u):
    return {"id": u["id"], "username": u["username"], "is_public": bool(u["is_public"])}


USERNAME_RE = re.compile(r"^[\w.\-]{3,30}$")


def _start_session(uid):
    session.clear()
    session.permanent = True
    session["uid"] = uid


@app.post("/api/register")
def register():
    p = request.get_json(silent=True) or {}
    username = (p.get("username") or "").strip()
    password = p.get("password") or ""
    if SIGNUP_CODE and not hmac.compare_digest(str(p.get("code") or ""), SIGNUP_CODE):
        return jsonify(error="code d'invitation incorrect"), 403
    if not USERNAME_RE.match(username):
        return jsonify(error="pseudo : 3 à 30 caractères, lettres, chiffres, point, tiret ou _"), 400
    if len(password) < 8:
        return jsonify(error="mot de passe : 8 caractères minimum"), 400
    db = get_db()
    first = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    try:
        cur = db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                         (username, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")))
    except sqlite3.IntegrityError:
        return jsonify(error="ce pseudo est déjà pris"), 409
    if first:  # le premier compte récupère la collection d'avant les comptes
        db.execute("UPDATE collection SET user_id=? WHERE user_id IS NULL", (cur.lastrowid,))
    db.commit()
    _start_session(cur.lastrowid)
    return jsonify(ok=True, user=user_json(current_user()))


@app.post("/api/login")
def login():
    p = request.get_json(silent=True) or {}
    u = get_db().execute("SELECT * FROM users WHERE username=?", ((p.get("username") or "").strip(),)).fetchone()
    if not u or not check_password_hash(u["password_hash"], p.get("password") or ""):
        time.sleep(0.5)  # freine les essais à la chaîne
        return jsonify(error="pseudo ou mot de passe incorrect"), 403
    _start_session(u["id"])
    return jsonify(ok=True, user=user_json(u))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/session")
def session_state():
    u = current_user()
    has_users = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    return jsonify(ok=bool(u), user=user_json(u) if u else None, signup_code=bool(SIGNUP_CODE), has_users=has_users,
                   ai=bool(ANTHROPIC_API_KEY))


@app.patch("/api/me")
@login_required
def update_me():
    p = request.get_json(silent=True) or {}
    db = get_db()
    if "is_public" in p:
        db.execute("UPDATE users SET is_public=? WHERE id=?", (int(bool(p["is_public"])), g.uid))
    if p.get("new_password"):
        u = db.execute("SELECT password_hash FROM users WHERE id=?", (g.uid,)).fetchone()
        if not check_password_hash(u["password_hash"], p.get("password") or ""):
            return jsonify(error="mot de passe actuel incorrect"), 403
        if len(p["new_password"]) < 8:
            return jsonify(error="mot de passe : 8 caractères minimum"), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(p["new_password"]), g.uid))
    db.commit()
    return jsonify(ok=True, user=user_json(current_user()))


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


def fetch_pokemon_ptcgio(name=None, number=None, api_id=None):
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


# ----------------------------------------------------------------- Pokémon : TCGdex (source principale)
# TCGdex est gratuit, sans clé, connaît les noms français et donne les prix Cardmarket en euros.
# pokemontcg.io ne sert plus que de repli, pour relever les cartes enregistrées avec un identifiant qu'il est seul à connaître.
TCGDEX = "https://api.tcgdex.net/v2"
_tcgdex_sets = {"at": 0.0, "sets": []}


def _tcgdex_get(path, params=None, lang="fr"):
    r = get_with_retry(f"{TCGDEX}/{lang}{path}", "TCGdex", params=params)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _tcgdex_normalize(c):
    """Fiche TCGdex complète -> format commun. Carte uniquement holo/reverse : on prend le prix holo."""
    cm = (c.get("pricing") or {}).get("cardmarket") or {}
    variants = c.get("variants") or {}
    holo_only = variants and not variants.get("normal", True)
    sfx = "-holo" if holo_only and cm.get("trend-holo") is not None else ""
    s = c.get("set") or {}
    total = (s.get("cardCount") or {}).get("official")
    img = c.get("image")
    return {
        "game": "pokemon",
        "api_id": c["id"],
        "name": c.get("name", "?"),
        "set_name": s.get("name"),
        "number": f'{c.get("localId", "")}/{total}' if total else c.get("localId"),
        "rarity": c.get("rarity"),
        "image": f"{img}/low.webp" if img else None,
        "price": _num(cm.get("trend" + sfx)) or _num(cm.get("avg" + sfx)),
        "avg7": _num(cm.get("avg7" + sfx)),
        "avg30": _num(cm.get("avg30" + sfx)),
        "src_date": (cm.get("updated") or "")[:10] or None,
    }


def _tcgdex_details(ids, lang="fr"):
    """Fiches complètes en parallèle (la liste de recherche ne contient ni prix ni extension)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(cid):
        try:
            return _tcgdex_get(f"/cards/{cid}", lang=lang)
        except (SourceError, requests.RequestException):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [c for c in pool.map(one, ids) if c]


def _tcgdex_sets_with_total(total):
    if not _tcgdex_sets["sets"] or time.time() - _tcgdex_sets["at"] > 24 * 3600:
        sets = _tcgdex_get("/sets", lang="en") or []
        if sets:
            _tcgdex_sets.update(at=time.time(), sets=sets)
    return [s["id"] for s in _tcgdex_sets["sets"] if (s.get("cardCount") or {}).get("official") == total]


def fetch_pokemon_tcgdex(name=None, number=None, api_id=None):
    if api_id:
        c = _tcgdex_get(f"/cards/{api_id}") or _tcgdex_get(f"/cards/{api_id}", lang="en")
        return [_tcgdex_normalize(c)] if c else []

    name = (name or "").strip()
    num, _, total = (number or "").partition("/")
    num, total = num.strip().lstrip("0") or num.strip(), total.strip()
    if num.isdigit() and total.isdigit():
        # « 58/102 » : les extensions de 102 cartes officielles, puis la carte n° 58 de chacune
        set_ids = _tcgdex_sets_with_total(int(total))[:6]
        cards = []
        for sid in set_ids:
            c = _tcgdex_get(f"/sets/{sid}/{num}") or _tcgdex_get(f"/sets/{sid}/{num}", lang="en")
            if c:
                cards.append(c)
        if cards:
            if name:  # le nom (souvent mal lu sur la photo) ne sert qu'à classer
                n = _norm(name)
                cards.sort(key=lambda c: 0 if n[:4] in _norm(c.get("name")) else 1)
            return [_tcgdex_normalize(c) for c in cards]
    if len(name) < 3:
        return []

    # nom français d'abord, puis anglais ; et en dernier recours le début du nom (faute de lecture en fin de mot)
    brief, lang = [], "fr"
    en_name = pokemon_name_to_english(name)
    for lang, q in (("fr", name), ("en", en_name), ("fr", name[:max(4, len(name) - 2)])):
        params = {"name": q, "pagination:page": 1, "pagination:itemsPerPage": 40}
        brief = _tcgdex_get("/cards", params=params, lang=lang) or []
        if num:
            brief = [b for b in brief if (b.get("localId") or "").lstrip("0") == num] or brief
        if brief:
            break
    cards = _tcgdex_details([b["id"] for b in brief[:24]], lang=lang)
    cards.sort(key=lambda c: (c.get("set") or {}).get("id", ""), reverse=True)
    return [_tcgdex_normalize(c) for c in cards]


def fetch_pokemon(name=None, number=None, api_id=None):
    if api_id:
        res = fetch_pokemon_tcgdex(api_id=api_id)
        return res or fetch_pokemon_ptcgio(api_id=api_id)  # carte enregistrée avec un identifiant pokemontcg.io
    return fetch_pokemon_tcgdex(name=name, number=number)


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


# ----------------------------------------------------------------- Magic: The Gathering (Scryfall)
# Scryfall demande un User-Agent et un Accept explicites, et au plus ~10 requêtes/s.
SCRYFALL_HEADERS = {"User-Agent": "Sleevr/1.0", "Accept": "application/json"}


def _mtg_image(c):
    if c.get("image_uris"):
        return c["image_uris"].get("small")
    faces = c.get("card_faces") or [{}]
    return (faces[0].get("image_uris") or {}).get("small")


def _mtg_entries(c, display_name=None):
    """Une entrée pour la version normale et une pour la version foil (prix Cardmarket différents)."""
    prices = c.get("prices") or {}
    base = {
        "game": "magic",
        "name": display_name or c.get("name", "?"),
        "set_name": c.get("set_name"),
        "number": f'{(c.get("set") or "").upper()} {c.get("collector_number", "")}'.strip(),
        "image": _mtg_image(c),
        "avg7": None,
        "avg30": None,
    }
    out = []
    finishes = c.get("finishes") or ["nonfoil"]
    if "nonfoil" in finishes or prices.get("eur") is not None:
        out.append({**base, "api_id": c["id"], "rarity": c.get("rarity"), "price": _num(prices.get("eur"))})
    if "foil" in finishes or prices.get("eur_foil") is not None:
        out.append({**base, "api_id": f'{c["id"]}|foil', "rarity": f'{c.get("rarity") or ""} · foil'.strip(" ·"),
                    "price": _num(prices.get("eur_foil"))})
    return out


def _mtg_search(q):
    r = get_with_retry("https://api.scryfall.com/cards/search", "Scryfall", headers=SCRYFALL_HEADERS,
                       params={"q": q, "unique": "prints", "order": "released", "dir": "desc"})
    if r.status_code == 404:  # « aucune carte » chez Scryfall
        return []
    r.raise_for_status()
    return r.json().get("data", [])


def _mtg_get(path):
    r = get_with_retry(f"https://api.scryfall.com{path}", "Scryfall", headers=SCRYFALL_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fetch_magic(name=None, number=None, api_id=None):
    if api_id:
        c = _mtg_get(f"/cards/{api_id.split('|')[0]}")
        return [e for e in _mtg_entries(c) if e["api_id"] == api_id] if c else []

    # « MKM 123 », « mkm-123 », « MKM/123a » : extension + numéro de collection identifient la carte
    m = re.match(r"^\s*([A-Za-z0-9]{2,6})[\s\-/#•.]+([0-9]+[a-z★]?)\s*$", number or "")
    if m:
        c = _mtg_get(f"/cards/{m.group(1).lower()}/{m.group(2)}")
        if c:
            return _mtg_entries(c)

    name = (name or "").replace('"', "").strip()
    if len(name) < 2:
        return []
    # 1) nom français (impressions françaises) ; les prix ne sont relevés que sur les impressions anglaises
    fr_names = {}
    try:
        for c in _mtg_search(f'"{name}" lang:fr game:paper')[:40]:
            fr_names.setdefault(c.get("name"), c.get("printed_name") or c.get("name"))
    except (SourceError, requests.RequestException):
        pass
    if fr_names:
        wanted = list(fr_names)[:4]
        cards = _mtg_search("(" + " or ".join(f'!"{n}"' for n in wanted) + ") game:paper")
    else:
        # 2) nom anglais ; puis seulement son début (faute de lecture en fin de mot)
        cards = []
        for cut in (len(name), max(4, len(name) - 2)):
            cards = _mtg_search(f'"{name[:cut]}" game:paper')
            if cards:
                break
    entries = []
    for c in cards[:30]:
        entries.extend(_mtg_entries(c, fr_names.get(c.get("name"))))
    return entries[:60]


# ----------------------------------------------------------------- One Piece Card Game (optcgapi.com)
# Pas de source gratuite avec les prix Cardmarket : optcgapi.com donne le prix TCGplayer (marché américain, en
# dollars), converti ici en euros au taux du jour (BCE via frankfurter). C'est une estimation, l'appli le signale.
OPTCG = "https://optcgapi.com/api"
_op_cache = {"at": 0.0, "cards": []}
_fx_cache = {"day": None, "rate": None}
USD_EUR_FALLBACK = 0.9


def usd_to_eur_rate():
    today = date.today().isoformat()
    if _fx_cache["day"] != today:
        try:
            r = requests.get("https://api.frankfurter.dev/v1/latest", params={"from": "USD", "to": "EUR"},
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            _fx_cache.update(day=today, rate=float(r.json()["rates"]["EUR"]))
        except (requests.RequestException, ValueError, KeyError):
            app.logger.warning("taux USD/EUR indisponible, on garde le dernier connu")
    return _fx_cache["rate"] or USD_EUR_FALLBACK


def _op_entry(c, rate):
    usd = _num(c.get("market_price")) or _num(c.get("inventory_price"))
    return {
        "game": "onepiece",
        "api_id": c.get("card_image_id") or c.get("card_set_id"),
        "name": c.get("card_name", "?"),
        "set_name": c.get("set_name"),
        "number": c.get("card_set_id"),
        "rarity": c.get("rarity"),
        "image": c.get("card_image"),
        "price": round(usd * rate, 2) if usd is not None else None,
        "avg7": None,
        "avg30": None,
        "src_date": c.get("date_scraped"),
        "price_source": "tcgplayer",
    }


def _op_endpoints(code):
    """Les decks de démarrage (ST…) et les promos (P-…) ont leurs propres routes chez optcgapi."""
    code = code.upper()
    if code.startswith("ST"):
        return [f"{OPTCG}/decks/card/{code}/"]
    if code.startswith("P-"):
        return [f"{OPTCG}/promos/card/{code}/"]
    return [f"{OPTCG}/sets/card/{code}/", f"{OPTCG}/decks/card/{code}/"]


def _op_by_code(code):
    for url in _op_endpoints(code):
        r = get_with_retry(url, "optcgapi.com")
        if r.status_code == 200:
            data = r.json()
            data = data if isinstance(data, list) else [data]
            if data and isinstance(data[0], dict) and data[0].get("card_set_id"):
                return data
    return []


def _op_all_cards():
    """Liste complète des cartes (≈ quelques milliers), gardée 12 h en mémoire : optcgapi n'a pas de recherche par nom."""
    if _op_cache["cards"] and time.time() - _op_cache["at"] < 12 * 3600:
        return _op_cache["cards"]
    cards = []
    for path in ("allSetCards", "allSTCards", "allPromoCards"):
        r = get_with_retry(f"{OPTCG}/{path}/", "optcgapi.com")
        if r.status_code == 200:
            cards.extend(c for c in r.json() if isinstance(c, dict))
    if cards:
        _op_cache.update(at=time.time(), cards=cards)
    return cards or _op_cache["cards"]


def _op_code(text):
    """« OP01-001 », « op01 001 », « ST10-005 », « P-012 » ; les lectures « 0P01-0O1 » sont corrigées."""
    t = re.sub(r"\s+", "", (text or "").upper()).replace("_", "-")
    m = re.match(r"^([A-Z0O]{1,3}[0-9OIL]{0,2})-?([0-9OIL]{3})$", t)
    if not m:
        return None
    prefix, num = m.group(1), m.group(2)
    fix = str.maketrans("OIL", "011")
    head = re.match(r"^([A-Z0]+?)([0-9OIL]*)$", prefix)
    letters = head.group(1).replace("0", "O") if head else prefix
    digits = (head.group(2) if head else "").translate(fix)
    return f"{letters}{digits}-{num.translate(fix)}" if letters != "P" else f"P-{num.translate(fix)}"


def fetch_onepiece(name=None, number=None, api_id=None):
    rate = usd_to_eur_rate()
    if api_id:
        code = api_id.split("_")[0]
        return [_op_entry(c, rate) for c in _op_by_code(code) if (c.get("card_image_id") or c.get("card_set_id")) == api_id]
    code = _op_code(number)
    if code:
        found = _op_by_code(code)
        if found:
            return [_op_entry(c, rate) for c in found]
    wanted = _norm(name)
    if len(wanted) < 3:
        return []
    cards = _op_all_cards()
    hits = [c for c in cards if wanted in _norm(c.get("card_name"))]
    if not hits and len(wanted) > 5:  # faute de lecture en fin de nom
        hits = [c for c in cards if wanted[:-2] in _norm(c.get("card_name"))]
    hits.sort(key=lambda c: (c.get("card_set_id") or ""), reverse=True)
    return [_op_entry(c, rate) for c in hits[:60]]


FETCHERS = {"pokemon": fetch_pokemon, "yugioh": fetch_yugioh, "magic": fetch_magic, "onepiece": fetch_onepiece}


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


def backup_db(today=None):
    """Copie du jour de la base dans data/backups/ (cohérente même si l'appli écrit en même temps).
    On garde les BACKUP_KEEP plus récentes. Les photos, simples fichiers, restent dans data/photos/."""
    today = today or date.today().isoformat()
    folder = os.path.join(DATA_DIR, "backups")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, f"cartes-{today}.db")
    if os.path.exists(target):
        return None
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(target + ".tmp")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(target + ".tmp", target)
    old = sorted(f for f in os.listdir(folder) if f.startswith("cartes-") and f.endswith(".db"))
    for f in old[:-BACKUP_KEEP] if BACKUP_KEEP > 0 else []:
        os.remove(os.path.join(folder, f))
    return target


def scheduler_loop():
    """Une fois par jour, après REFRESH_HOUR, si le relevé du jour n'est pas fait ; et une sauvegarde par jour."""
    while True:
        try:
            backup_db()
        except Exception as exc:
            app.logger.warning("sauvegarde: %s", exc)
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
        "INSERT INTO collection (user_id, card_id, quantity, condition, added_at, custom_price, use_custom) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, card_id, condition) DO UPDATE SET quantity = quantity + excluded.quantity"
        + (", custom_price = excluded.custom_price, use_custom = excluded.use_custom" if use_custom else ""),
        (g.uid, card_id, qty, cond, datetime.now().isoformat(timespec="seconds"), custom, use_custom),
    )
    store_price(db, card_id, _num(p.get("price")), _num(p.get("avg7")), _num(p.get("avg30")),
                src_date=p.get("src_date"))
    db.commit()
    entry_id = db.execute("SELECT id FROM collection WHERE user_id=? AND card_id=? AND condition=?",
                          (g.uid, card_id, cond)).fetchone()["id"]
    return jsonify(ok=True, card_id=card_id, entry_id=entry_id)


COLLECTION_SQL = (
    "SELECT k.id, k.card_id, k.quantity, k.condition, k.notes, k.added_at, k.custom_price, k.use_custom, "
    "c.game, c.name, c.set_name, c.number, c.rarity, c.image "
    "FROM collection k JOIN cards c ON c.id = k.card_id WHERE k.user_id = ?"
)


def collection_items(db, uid, game=None):
    sql = COLLECTION_SQL + (" AND c.game=?" if game in FETCHERS else "") + " ORDER BY c.name COLLATE NOCASE"
    rows = db.execute(sql, (uid, game) if game in FETCHERS else (uid,)).fetchall()
    return [card_row_with_prices(db, r) for r in rows]


@app.get("/api/collection")
@login_required
def list_collection():
    db = get_db()
    items = collection_items(db, g.uid, request.args.get("game"))
    last = db.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
    return jsonify(items=items, last_refresh=last["value"] if last else None)


def own_entry(cid):
    return get_db().execute("SELECT * FROM collection WHERE id=? AND user_id=?", (cid, g.uid)).fetchone()


def visible_owner(uid):
    """Membre dont on peut voir la collection : soi-même, ou un membre qui l'a rendue visible."""
    return get_db().execute("SELECT * FROM users WHERE id=? AND (is_public=1 OR id=?)", (uid, g.uid)).fetchone()


@app.patch("/api/collection/<int:cid>")
@login_required
def update_entry(cid):
    p = request.get_json(force=True)
    db = get_db()
    row = own_entry(cid)
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
    if not db.execute("DELETE FROM collection WHERE id=? AND user_id=?", (cid, g.uid)).rowcount:
        return jsonify(error="introuvable"), 404
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
    if not own_entry(cid):
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
    row = get_db().execute("SELECT user_id FROM collection WHERE id=?", (cid,)).fetchone()
    if not row or not visible_owner(row["user_id"]):
        return jsonify(error="pas de photo"), 404
    if side not in SIDES or not os.path.exists(photo_path(cid, side)):
        return jsonify(error="pas de photo"), 404
    return send_file(photo_path(cid, side), mimetype="image/jpeg", max_age=86400)


@app.delete("/api/collection/<int:cid>/photo/<side>")
@login_required
def delete_photo(cid, side):
    if side not in SIDES:
        return jsonify(error="côté inconnu (recto ou verso)"), 404
    if not own_entry(cid):
        return jsonify(error="carte introuvable"), 404
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


def summary_for(db, uid):
    """Valeur totale de la collection d'un membre et son évolution jour par jour."""
    rows = db.execute(
        "SELECT k.quantity, k.card_id, k.custom_price, k.use_custom FROM collection k WHERE k.user_id=?", (uid,)
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
        return dict(total=fixed, cards=n_cards, unique=n_unique, history=[], var1=None, var7=None, var30=None)
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

    return dict(
        total=total,
        cards=n_cards,
        unique=n_unique,
        history=history_pts,
        var1=total_var(1),
        var7=total_var(7),
        var30=total_var(30),
    )


@app.get("/api/summary")
@login_required
def summary():
    return jsonify(summary_for(get_db(), g.uid))


# ----------------------------------------------------------------- les autres membres
@app.get("/api/members")
@login_required
def members():
    db = get_db()
    out = []
    for u in db.execute("SELECT * FROM users WHERE is_public=1 OR id=? ORDER BY username COLLATE NOCASE", (g.uid,)):
        s = summary_for(db, u["id"])
        out.append({**user_json(u), "me": u["id"] == g.uid, "total": s["total"], "cards": s["cards"],
                    "unique": s["unique"], "var7": s["var7"]})
    return jsonify(members=out)


@app.get("/api/members/<int:uid>")
@login_required
def member_collection(uid):
    db = get_db()
    u = visible_owner(uid)
    if not u:
        return jsonify(error="collection privée ou membre inconnu"), 404
    items = collection_items(db, uid)
    if uid != g.uid:  # les notes restent privées
        for it in items:
            it["notes"] = ""
    return jsonify(member=user_json(u), summary=summary_for(db, uid), items=items)


@app.post("/api/refresh")
@login_required
def manual_refresh():
    res = refresh_guarded()
    if res is None:
        return jsonify(error="un relevé est déjà en cours"), 409
    return jsonify(ok=res[0], errors=res[1])


# ----------------------------------------------------------------- reconnaissance par l'IA
IDENTIFY_PROMPT = """Cette photo montre une carte à collectionner (Pokémon, Yu-Gi-Oh!, Magic: The Gathering ou One Piece Card Game).
Identifie-la pour qu'on puisse la retrouver dans une base de données.

- game : "pokemon", "yugioh", "magic", "onepiece", ou "unknown" si ce n'est pas une carte de ces jeux.
- name : le nom de la carte tel qu'il est imprimé (dans la langue de la carte), sans PV, niveau ni symbole.
- number : l'identifiant imprimé, au format attendu par le jeu, ou "" s'il est illisible :
  Pokémon « 58/102 » (numéro/total, en bas de la carte) ;
  Yu-Gi-Oh! le code d'extension sous l'illustration, ex. « LOB-FR005 » ;
  Magic le code d'extension puis le numéro de collection, en bas à gauche, ex. « MKM 123 » ;
  One Piece le code en bas à droite, ex. « OP01-001 » ou « ST10-005 ».
- confidence : "high" si nom et numéro sont nettement lisibles, "medium" si l'un des deux est deviné, "low" sinon.

Ne devine pas un numéro que tu ne lis pas : laisse-le vide, la recherche par nom prendra le relais."""

IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "game": {"type": "string", "enum": ["pokemon", "yugioh", "magic", "onepiece", "unknown"]},
        "name": {"type": "string"},
        "number": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["game", "name", "number", "confidence"],
    "additionalProperties": False,
}

_ai_client = None


def ai_client():
    global _ai_client
    if _ai_client is None:
        import anthropic  # importé seulement si la reconnaissance par l'IA est activée

        _ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
    return _ai_client


def identify_card(jpeg):
    """Photo (JPEG) -> {game, name, number, confidence} lus par Claude. Lève SourceError en cas d'échec."""
    import base64

    import anthropic

    try:
        resp = ai_client().beta.messages.create(
            model=CARD_AI_MODEL,
            max_tokens=4000,
            # si le modèle décline la requête, l'API la relance sur le modèle de repli recommandé
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": IDENTIFY_SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(jpeg).decode()}},
                {"type": "text", "text": IDENTIFY_PROMPT},
            ]}],
        )
    except anthropic.RateLimitError:
        raise SourceError("Reconnaissance très demandée en ce moment, réessaie dans un instant.")
    except anthropic.APIStatusError as exc:
        app.logger.warning("identification: %s %s", exc.status_code, exc.message)
        raise SourceError("La reconnaissance par l'IA a échoué.")
    except anthropic.APIConnectionError:
        raise SourceError("Service de reconnaissance injoignable.")
    if resp.stop_reason != "end_turn":
        raise SourceError("La reconnaissance par l'IA n'a pas abouti.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        raise SourceError("Réponse de reconnaissance illisible.")
    return {k: (out.get(k) or "").strip() for k in ("game", "name", "number", "confidence")}


@app.post("/api/identify")
@login_required
def identify():
    if not ANTHROPIC_API_KEY:
        return jsonify(error="reconnaissance par l'IA non configurée"), 404
    data = request.get_data(cache=False)
    if len(data) < 100 or data[:3] != b"\xff\xd8\xff":
        return jsonify(error="Photo JPEG attendue"), 415
    try:
        return jsonify(identify_card(data))
    except SourceError as exc:
        return jsonify(error=str(exc)), 502


@app.get("/api/export.csv")
@login_required
def export_csv():
    import csv
    import io

    db = get_db()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Jeu", "Nom", "Extension", "Numéro", "Rareté", "État", "Quantité", "Prix unitaire (EUR)", "Total (EUR)", "Prix"])
    for r in db.execute(COLLECTION_SQL + " ORDER BY c.game, c.name COLLATE NOCASE", (g.uid,)).fetchall():
        d = card_row_with_prices(db, r)
        w.writerow([d["game"], d["name_fr"], d["set_name"], d["number"], d["rarity"], d["condition"],
                    d["quantity"], d["price"], d["total"],
                    "perso" if d["use_custom"] else "marché"])
    return app.response_class(
        "﻿" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sleevr-collection.csv"},
    )


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


init_db()

if os.environ.get("DISABLE_SCHEDULER") != "1":
    threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
