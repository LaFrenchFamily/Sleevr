# Partager Holocarte sans nom de domaine (Docker Desktop + Tailscale Funnel)

Holocarte tourne sur ton PC dans Docker Desktop, et Tailscale Funnel le rend accessible à tes amis sur une adresse
fixe et gratuite, en HTTPS : `https://holocarte.ton-reseau.ts.net`. Pas de nom de domaine à acheter, aucun port à
ouvrir sur ta box, et tes amis n'ont rien à installer : ils ouvrent simplement le lien.

**Où sont les données ?** Dans le dossier `data/` à côté de ce projet, sur ton disque (base `cartes.db`, photos,
sauvegardes quotidiennes dans `data/backups/`). Rien n'est stocké chez Tailscale.

## Ce qu'il te faut

- Docker Desktop, avec « Start Docker Desktop when you sign in » coché (Settings → General).
- La mise en veille du PC désactivée (Paramètres Windows → Système → Alimentation).
- Un compte Tailscale gratuit (plan *Personal*) : https://login.tailscale.com (connexion avec Google, Microsoft…).

## 1. Préparer Tailscale (une seule fois)

Dans la console Tailscale (https://login.tailscale.com/admin) :

1. **DNS** : vérifie que **MagicDNS** est activé, puis active **HTTPS Certificates**. C'est sur cette page que tu
   vois aussi le nom de ton réseau (ex. `tail1234.ts.net`) : ton adresse sera `https://holocarte.tail1234.ts.net`.
2. **Funnel** : il doit être autorisé dans ton réseau. Sur les comptes récents, il l'est déjà. Sinon, la console te
   proposera de l'activer au premier lancement (bandeau « Enable Funnel »), ou ajoute-le dans *Access controls*.
3. **Settings → Keys → Generate auth key** : laisse les options par défaut, clique *Generate*, copie la clé
   (elle commence par `tskey-auth-…`). C'est un mot de passe, ne la partage pas. Elle ne sert qu'au premier
   lancement : ensuite la machine reste connectée, même après l'expiration de la clé.

## 2. Préparer le projet (une seule fois)

Copie `.env.example` en `.env`, puis remplis :

```
SIGNUP_CODE=un-code-que-tu-donneras-a-tes-amis
SECRET_KEY=une-longue-chaine-aleatoire-a-ne-jamais-changer
TS_AUTHKEY=tskey-auth-...la-cle-copiee...
ANTHROPIC_API_KEY=            (facultatif : reconnaissance des cartes par l'IA)
```

**Tu avais déjà une collection ?** Copie ton ancien dossier `data/` dans ce projet avant le premier lancement. La
collection ira au premier compte créé : crée le tien avant d'inviter qui que ce soit.

## 3. Lancer

Double-clique sur **`partager-tailscale.bat`** (ou : `docker compose -f docker-compose.funnel.yml up -d --build`).

- Dans la console Tailscale, page **Machines**, une machine `holocarte` apparaît, avec un badge **Funnel**.
- Sur ton PC : http://localhost:8000
- Pour tes amis : `https://holocarte.ton-reseau.ts.net`, avec le code d'invitation pour créer leur compte.
  Le tout premier chargement peut prendre une dizaine de secondes, le temps que le certificat HTTPS soit créé.

Ensuite tout redémarre seul avec Docker Desktop, et l'adresse ne change jamais : l'identité de la machine est
gardée dans le dossier `tailscale/state/` (ne le supprime pas, et ne le partage pas).

## Au quotidien

- **Mettre à jour** : récupère la nouvelle version du code (`git pull`), puis relance `partager-tailscale.bat`.
  Les dossiers `data/` et `tailscale/state/` ne sont jamais touchés.
- **Arrêter le partage** : `docker compose -f docker-compose.funnel.yml down` (les données restent).
- **Sauvegarder ailleurs** : copie de temps en temps le dossier `data/` entier sur une clé ou dans un cloud.
- **Ça ne marche pas ?** Dans Docker Desktop, onglet *Containers*, ouvre les journaux du conteneur `tailscale` :
  une clé invalide ou un Funnel non autorisé y sont signalés clairement.

Tu préfères ton propre nom de domaine ? Voir [partage-docker-desktop.md](partage-docker-desktop.md) (Cloudflare Tunnel).
