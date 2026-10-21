# Partager Sleevr depuis ton PC (Docker Desktop + Cloudflare Tunnel)

Sleevr tourne sur ton PC dans Docker Desktop, et un tunnel Cloudflare le rend accessible à tes amis sur
`https://sleevr.ton-domaine.fr`. Tu n'as aucun port à ouvrir sur ta box, et l'HTTPS est fourni par Cloudflare (ce qui
permet d'installer l'appli sur l'écran d'accueil du téléphone).

**Où sont les données ?** Dans le dossier `data/` à côté de ce projet, sur ton disque : la base `cartes.db`
(comptes, collections, prix), les photos dans `data/photos/`, et une sauvegarde de la base par jour dans
`data/backups/` (les 14 dernières). Rien n'est stocké chez Cloudflare ni sur les téléphones.

## Ce qu'il te faut

- Docker Desktop installé, avec « Start Docker Desktop when you sign in » coché (Settings → General).
- La mise en veille du PC désactivée (Paramètres Windows → Système → Alimentation) : PC en veille = appli injoignable.
- Un compte Cloudflare (gratuit) et un nom de domaine géré par Cloudflare (environ 10 € par an ; tu peux l'acheter
  directement chez Cloudflare, dans « Domain Registration »).

## 1. Créer le tunnel chez Cloudflare (une seule fois)

1. Dans le tableau de bord Cloudflare, ouvre **Tunnels** (selon la version de l'interface : *Networking → Tunnels*
   ou *Zero Trust → Networks → Tunnels*) et clique **Create a tunnel**.
2. Type **Cloudflared**, nom : `sleevr`.
3. Sur l'écran d'installation, choisis **Docker**. La commande affichée contient un long jeton qui commence par
   `eyJ…` : copie uniquement ce jeton. C'est un mot de passe, ne le partage pas.
4. Ajoute la route publique (onglet **Public Hostname**, ou **Routes → Add route → Published application**) :
   - Sous-domaine : `sleevr`, domaine : le tien.
   - Service : type **HTTP**, URL **`sleevr:8000`** (c'est le nom du conteneur, pas `localhost`).
5. Enregistre. Cloudflare crée l'enregistrement DNS tout seul.

## 2. Préparer le projet (une seule fois)

Dans le dossier du projet, copie `.env.example` en `.env`, puis remplis :

```
SIGNUP_CODE=un-code-que-tu-donneras-a-tes-amis
SECRET_KEY=une-longue-chaine-aleatoire-a-ne-jamais-changer
CLOUDFLARE_TUNNEL_TOKEN=eyJ...le-jeton-copie-a-l-etape-1...
ANTHROPIC_API_KEY=            (facultatif : reconnaissance des cartes par l'IA)
```

`SECRET_KEY` doit rester la même : si tu la changes, tout le monde est déconnecté.

**Tu avais déjà une collection ?** Copie ton ancien dossier `data/` dans ce projet avant le premier lancement.
La collection ira au premier compte créé : crée le tien avant d'inviter qui que ce soit.

## 3. Lancer

Double-clique sur **`partager.bat`** (ou, dans un terminal : `docker compose -f docker-compose.tunnel.yml up -d --build`).

- Sur ton PC : http://localhost:8000
- Pour tes amis : `https://sleevr.ton-domaine.fr`, avec le code d'invitation pour créer leur compte.

Ensuite, tout redémarre seul : Docker Desktop se lance à l'ouverture de ta session Windows, et les conteneurs
(`restart: unless-stopped`) repartent avec lui. Le tunnel se reconnecte tout seul après une coupure de la box.

## Au quotidien

- **Mettre à jour Sleevr** : récupère la nouvelle version du code (`git pull`), puis relance `partager.bat`.
  Le dossier `data/` n'est jamais touché.
- **Arrêter le partage** : `docker compose -f docker-compose.tunnel.yml down` (les données restent).
- **Sauvegarder ailleurs** : `data/backups/` protège contre une erreur dans l'appli, pas contre une panne de disque.
  Copie de temps en temps le dossier `data/` entier sur une clé ou dans OneDrive / Google Drive.
- **Restaurer une sauvegarde** : arrête l'appli, remplace `data/cartes.db` par le fichier voulu de `data/backups/`
  (renommé en `cartes.db`), puis relance.
- **Ça ne marche pas ?** Dans Docker Desktop, onglet *Containers*, ouvre les journaux du conteneur `tunnel` : un
  jeton mal copié y apparaît clairement. Dans Cloudflare, le tunnel doit être affiché « Healthy ».
