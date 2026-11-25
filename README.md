# MenuCrous — bot Discord des menus CROUS (Lyon) et des notes INSA

Bot Discord qui :

- poste chaque matin (heure réglable) le menu des restaurants CROUS **Manufacture des Tabacs**
  et **Puvis de Chavannes** dans un salon, et répond à `/menu` ;
- prévient quand une **nouvelle note** apparaît sur Mon Dossier Web (INSA Lyon) : la matière
  dans un salon, la note en message privé au propriétaire du compte ;
- sert une petite page web des menus de la semaine (+ API JSON).

Il s'installe **sur un serveur** (envois automatiques, `/config`) **et sur un compte
utilisateur** (commandes `/menu`, `/semaine`… utilisables partout, même en MP).

## Sources des menus

1. Page du restaurant sur [crous-lyon.fr](https://www.crous-lyon.fr/restaurant/restaurant-manufacture-des-tabacs/)
   (HTML statique, lu avec httpx + BeautifulSoup) ;
2. en secours, l'API publique de [CROUStillant](https://croustillant.menu)
   (`api.croustillant.menu/v1/restaurants/<id>/menu`).

CROUStillant analyse chaque plat (entrée / plat / dessert) : le bot s'en sert pour remettre
dans la bonne catégorie les plats mal saisis par certains restaurants (Puvis range parfois
les desserts sous « Plat végétarien »).

## Commandes

| Commande | Où | Effet |
|---|---|---|
| `/menu [restaurant] [jour] [complet]` | partout | Menu d'un jour (par défaut : aujourd'hui, les deux restos, sans entrées/desserts) |
| `/semaine [restaurant]` | partout | Les plats des prochains jours |
| `/restaurants` | partout | Adresse, horaires, accès |
| `/notes verifier` | partout | Relève les notes maintenant et annonce les nouvelles (matière seulement) |
| `/notes statut` | partout | Dernier relevé, prochain, dernière erreur |
| `/notes liste` | partout, **propriétaire** | Toutes les notes, en privé |
| `/notes code <code>` | partout, **propriétaire** | Donne le code 2FA demandé en MP |
| `/config menu salon: heure: restaurants: complet:` | serveur, *Gérer le serveur* | Envoi automatique quotidien |
| `/config menu-stop`, `/config notes salon:`, `/config notes-stop`, `/config voir`, `/config test` | serveur | Réglages |
| `/aide` | partout | Aide |

Les jours sans menu (week-end, fermeture), rien n'est posté. Si le bot redémarre peu après
l'heure prévue, l'envoi est rattrapé (jusqu'à 3 h de retard).

## Notes INSA

Mon Dossier Web est une appli Vaadin derrière le SSO de l'INSA : le bot ouvre un Chromium
headless (Playwright), se connecte avec `INSA_USERNAME` / `INSA_PASSWORD`, lit le tableau des
notes et le compare au relevé précédent (SQLite). Relevé toutes les `NOTES_INTERVAL_MIN`
minutes entre 7 h et 23 h. Le premier relevé ne déclenche aucune alerte.

**2FA** : les cookies de session sont gardés dans `data/insa-session.json`, le 2FA n'est donc
redemandé qu'à l'expiration de la session SSO. À ce moment-là : si `INSA_TOTP_SECRET` (clé du
QR code) est fourni, le bot génère le code seul ; sinon il envoie un MP au propriétaire et
attend `/notes code 123456` (10 min), puis met le relevé automatique en pause jusqu'au
prochain `/notes verifier`.

Un seul compte est surveillé (celui du propriétaire, `NOTES_OWNER_ID`) : les salons ne voient
que la matière, jamais la note.

Test manuel (enregistre une capture et le HTML dans `data/debug/`) :

```bash
python -m menucrous.notes
```

## Configuration

Copier `.env.example` en `.env`. Seul `DISCORD_TOKEN` est obligatoire ; `MENU_CHANNEL_ID`,
`MENU_TIME` et `NOTES_CHANNEL_ID` ne servent qu'au premier démarrage, ensuite tout se règle
avec `/config` (stocké dans `data/menucrous.db`).

### Application Discord

1. [Developer Portal](https://discord.com/developers/applications) → l'application → **Bot** → *Reset Token*.
2. **Installation** → cocher **Guild Install** *et* **User Install** ;
   Guild Install : scopes `applications.commands` + `bot`, permissions *View Channels*,
   *Send Messages*, *Embed Links* ; User Install : scope `applications.commands`.
3. Utiliser le lien d'installation pour l'ajouter à un serveur, ou à son compte.

Aucun intent privilégié n'est nécessaire (plus de commande `!menu`).

## Lancer

```bash
# Docker (recommandé)
docker build -t menucrous . && docker run --env-file .env -v ./data:/data -p 8080:8080 menucrous

# ou en local
python -m venv .venv && .venv/Scripts/activate   # Windows (source .venv/bin/activate ailleurs)
pip install -r requirements.txt && playwright install chromium
python -m menucrous
```

Page web : `http://localhost:8080/` · API : `/api/menu?jour=AAAA-MM-JJ` · santé : `/health`.

## Hébergement

En production sur le homelab (`menu.filraen.fr`) : stack Docker Compose derrière Traefik,
redéployée automatiquement à chaque push sur `main` (toutes les 5 min).
