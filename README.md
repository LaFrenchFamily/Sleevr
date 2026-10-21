# Holocarte — ta collection de cartes

Appli web (installable sur le téléphone) pour enregistrer tes cartes en les photographiant,
connaître leur valeur (prix Cardmarket, en euros) et suivre les variations à 1, 7 et 30 jours.

- **Ajout par photo** : la photo est lue sur le téléphone (OCR), le nom et le numéro pré-remplissent la recherche, tu choisis la bonne carte (image + extension + prix) et tu valides.
- **Collection** : valeur totale + courbe, filtre par jeu / nom, tri par valeur ou variation, quantité, état, notes.
- **Prix** : relevé automatique chaque jour (à 6 h par défaut) → historique à toi, courbe par carte.
- **Export CSV** de la collection.

Sources, toutes gratuites : [pokemontcg.io](https://pokemontcg.io) (Pokémon), [YGOPRODeck](https://ygoprodeck.com/api-guide/) (Yu-Gi-Oh!), [Scryfall](https://scryfall.com/docs/api) (Magic) et [optcgapi.com](https://optcgapi.com) (One Piece).

## Option A — Sur ton PC Windows (le plus simple pour commencer)

1. Installe Python depuis python.org (coche « Add python.exe to PATH »).
2. Double-clique sur `lancer.bat`, choisis un code d'invitation, puis crée ton compte dans l'appli.
3. PC : http://localhost:8000 — Téléphone (même Wi-Fi) : `http://ADRESSE-IP-DU-PC:8000` (l'adresse s'affiche dans la fenêtre).

Le PC doit rester allumé pour le relevé quotidien des prix. Sans HTTPS, l'installation « sur l'écran d'accueil » n'est pas proposée, mais la prise de photo fonctionne.

## Option B — Sur un serveur (VPS), accessible partout, prix relevés 24 h/24

Prérequis : Docker, et un nom de domaine qui pointe vers le serveur (HTTPS automatique via Caddy).

```bash
cp .env.example .env     # puis édite : SIGNUP_CODE, SECRET_KEY, DOMAIN
docker compose up -d --build
```

Ouvre `https://ton-domaine` sur le téléphone, puis « Ajouter à l'écran d'accueil ».
Les données sont dans le dossier `data/` (une seule base `cartes.db`, les photos, et une sauvegarde quotidienne de la base dans `data/backups/`, les 14 dernières) : copie aussi ce dossier ailleurs de temps en temps.

## Option C — Sur ton PC avec Docker Desktop, partagé sur Internet

Docker Desktop + un tunnel Cloudflare : tes amis accèdent à l'appli en HTTPS sans que tu ouvres de port sur ta box. Le PC doit rester allumé. Guide pas à pas : [docs/partage-docker-desktop.md](docs/partage-docker-desktop.md), puis double-clic sur `partager.bat`.

## Comptes et collectionneurs

- Chacun crée son compte (pseudo + mot de passe) avec le **code d'invitation** (`SIGNUP_CODE`) et a **sa propre collection**.
- L'onglet **Collectionneurs** montre les collections des autres membres (valeur, cartes, photos), en lecture seule. Les notes restent privées. Dans « Mon compte » (bouton rond en haut), tu peux rendre ta collection privée.
- **Mise à jour d'une ancienne installation** : la collection existante est rattachée automatiquement au **premier compte créé**. Si tu avais `APP_PASSWORD`, il sert de code d'invitation tant que `SIGNUP_CODE` n'est pas défini.
- Le relevé quotidien des prix est commun à tous : une carte présente dans plusieurs collections n'est relevée qu'une fois.

## À savoir

- **Le prix est celui de la carte « en bon état » (Cardmarket, tendance)** ; l'état que tu indiques (NM, EX…) n'ajuste pas le prix.
- **Yu-Gi-Oh!** : le prix dépend de l'extension et de la rareté, d'où une ligne par impression dans les résultats. Vérifie bien le code (ex. LOB-FR005).
- **Variations** : calculées avec ton propre historique. Les premiers jours, pour Pokémon, l'appli s'appuie sur les moyennes 7 j / 30 j de Cardmarket ; pour Yu-Gi-Oh!, les variations apparaissent au fil des relevés.
- **Reconnaissance par l'IA (recommandée)** : si `ANTHROPIC_API_KEY` est renseignée, la photo est envoyée au serveur, qui la fait reconnaître par Claude (modèle réglable avec `CARD_AI_MODEL`). L'IA lit le nom et le numéro même sur une carte brillante ou inclinée, et reconnaît le jeu toute seule (l'onglet bascule). Compte environ 1 centime par photo. Sans clé, ou si elle échoue, l'appli revient à la lecture de texte sur le téléphone décrite ci-dessous.
- **La lecture de texte sur le téléphone est approximative** (c'est normal pour de l'OCR sur des cartes brillantes) : elle propose, tu confirmes. Pour de meilleurs résultats : carte à plat, bien éclairée, sans reflet, **droite** (pas inclinée), sur un fond uni, et le plus près possible en gardant toute la carte dans le cadre. L'appli repère la carte dans la photo, puis lit séparément le haut (nom) et le bas (numéro). Sous la photo, « Texte lu par l'appareil » montre ce qui a été lu : utile pour comprendre un échec.
- **Noms français** : les bases de données sont en anglais, mais l'appli affiche les noms de Pokémon en français (« Charizard VMAX » devient « Dracaufeu VMAX », « Dark Charizard » devient « Dracaufeu obscur ») grâce à un dictionnaire (`pokemon_fr.json`, 1025 Pokémon), aussi bien dans les résultats que dans la collection et le CSV. Ta base garde le nom d'origine. Les noms de dresseurs ou d'énergies ne sont pas traduits (utilise le numéro pour les chercher, par ex. 4/102). La recherche accepte le français comme l'anglais. Pour Yu-Gi-Oh!, la recherche essaie d'abord les noms français.
- **Fautes de lecture** : si le nom lu est un peu faux (« Pikachi »), la recherche retente avec le début du nom.
- **Photos recto / verso de TES cartes** : à l'ajout, et dans la fiche d'une carte (« Photos de ta carte »), tu peux photographier le recto et le verso de ton exemplaire pour garder une trace de son état. À l'ajout, la photo du scan est proposée comme recto (tu peux la retirer avec ✕). Dans la fiche, touche une miniature pour l'agrandir, la reprendre ou la supprimer. Les photos sont réduites (environ 0,2 à 0,5 Mo chacune) et rangées dans `data/photos/`, donc comprises dans la sauvegarde du dossier `data`. Une photo est rattachée à une ligne de la collection (une carte dans un état donné) : deux exemplaires dans des états différents ont chacun leurs photos, trois exemplaires dans le même état partagent les leurs. Supprimer la carte supprime ses photos.
- **Ton propre prix** : dans la fiche d'une carte (et au moment de l'ajout), l'interrupteur « Utiliser mon propre prix » te laisse saisir le prix que tu veux (ex. 12,50). Tant qu'il est allumé, ce prix remplace celui du marché dans la liste, le total de la collection et l'export CSV (colonne « Prix » : perso / marché), et la carte affiche « perso » sans variations. Éteins l'interrupteur pour revenir au prix du marché : ton prix reste mémorisé. Le réglage est propre à chaque ligne (carte + état) et le relevé quotidien ne l'écrase jamais. Le prix du marché continue d'être relevé en arrière-plan.
- **Magic** : prix Cardmarket via Scryfall, une ligne pour la version normale et une pour la foil. La recherche accepte le nom français (« Foudre ») ou anglais, et le code « extension + numéro » (ex. MKM 123), lu en bas à gauche de la carte.
- **One Piece** : aucune source gratuite ne donne le prix Cardmarket. L'appli utilise le prix TCGplayer (marché américain, en dollars) converti en euros au taux BCE du jour : c'est une estimation, signalée dans la fiche. Les noms sont en anglais ; le plus fiable est le code (ex. OP01-001).
- Clé API Pokémon gratuite (facultative) : https://dev.pokemontcg.io → à mettre dans `POKEMONTCG_API_KEY`.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests
```

## Fichiers

- `app.py` — serveur (Flask + SQLite), sources de prix, relevé quotidien
- `pokemon_fr.json` — dictionnaire des noms de Pokémon français → anglais
- `static/` — interface (HTML/CSS/JS), service worker, manifeste
- `Dockerfile`, `docker-compose.yml`, `Caddyfile` — déploiement serveur
- `lancer.bat` — lancement sur Windows
