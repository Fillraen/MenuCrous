# MenuCrous — bot Discord des menus CROUS (Lyon) et des notes INSA

Bot Discord qui :

- poste chaque matin (heure réglable) le menu des restaurants CROUS **Manufacture des Tabacs**
  et **Puvis de Chavannes** dans un salon, et répond à `/menu` ;
- prévient quand une **nouvelle note** apparaît sur Mon Dossier Web (INSA Lyon) : seulement
  le nom de la matière, jamais la note ;
- poste l'**emploi du temps** (ADE INSA) : récap de la semaine le lundi et/ou cours du jour,
  et prévient quand un cours des 14 prochains jours est déplacé, ajouté ou annulé ;
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
| `/edt jour [quand]` | partout | Cours d'un jour (aujourd'hui, demain, un jour de la semaine) |
| `/edt semaine [quand]` | partout | **Image** de la semaine (grille lundi → vendredi), cette semaine, la suivante ou dans 2 semaines |
| `/config edt-semaine salon: heure: jour:` | serveur | Image de la semaine, un jour par semaine (samedi/dimanche = semaine suivante) |
| `/config edt-jour salon: heure:` | serveur | Cours du jour, chaque matin où il y a cours |
| `/config edt-modifs salon:` | serveur | Cours déplacés / ajoutés / annulés (14 prochains jours) |
| `/config edt-stop quoi:` | serveur | Coupe l'une des trois (ou tout l'EDT) |
| `/config voir` | serveur | **Toutes** les configs du serveur : actives, arrêtées, pas encore faites |
| `/notes verifier` | partout | Relève les notes maintenant et annonce les nouvelles (matière seulement) |
| `/notes statut` | partout | Dernier relevé, prochain, dernière erreur |
| `/notes code <code>` | partout, **propriétaire** | Donne le code 2FA demandé en MP |
| `/config menu salon: heure: restaurants: complet:` | serveur, *Gérer le serveur* | Envoi automatique quotidien |
| `/config menu-stop`, `/config notes salon:`, `/config notes-stop`, `/config voir`, `/config test` | serveur | Réglages |
| `/aide` | partout | Aide |

Les jours sans menu (week-end, fermeture), rien n'est posté. Si le bot redémarre peu après
l'heure prévue, l'envoi est rattrapé (jusqu'à 3 h de retard).

## Emploi du temps

`EDT_ICS_URL` = lien iCal personnel d'ADE (`https://ade-outils.insa-lyon.fr/ADE-Cal:~login!2026-2027:<jeton>`),
pas de connexion à faire : **le jeton est dans l'URL, à garder secret**. Le flux est relu
(cache 30 min) ; toutes les 30 min entre 7 h et 23 h, les cours des 14 prochains jours sont
comparés au relevé précédent (horaire, salle, intitulé) et les changements annoncés. Une
semaine sans cours est affichée « période en entreprise ».

## SoWeSign (émargement)

Chacun ne signe **que pour soi** : toute commande ou tout bouton agit sur le compte de la
personne qui l'utilise.

| Commande | Effet |
|---|---|
| `/sws inscription` | Fenêtre privée : établissement, identifiant, PIN. Connexion testée, codes chiffrés en base (Fernet, `SWS_SECRET`) |
| `/sws signature [photo]` | Ta signature : lien vers une page pour la dessiner (même cadre que SoWeSign) ou photo (stylo foncé, papier blanc). Jusqu'à 5, une prise au hasard à chaque fois |
| `/sws attente` | Ce qui est à signer (jetons posés par le formateur, rattrapages), boutons « Signer » / « Tout signer » |
| `/sws code <code>` | Signe la séance ouverte en ce moment avec le code du formateur |
| `/sws notifications`, `/sws statut`, `/sws signatures-effacer`, `/sws desinscription` | Réglages, suppression de tout |
| `/config sws salon:` / `/config sws-stop` | (serveur) Rappels dans un salon : mention des membres inscrits qui ont une signature à faire, avec les boutons — chaque bouton signe pour la personne qui clique |

Toutes les 20 min (7 h–22 h), le bot regarde ce qui est en attente et envoie un MP avec les
boutons quand une nouvelle signature apparaît, plus un rappel le soir s'il en reste.

Fonctionnement : le bot fait ce que fait l'appli web étudiante (`app.sowesign.com/student`) —
connexion `JBAuth` par code, lecture de `presenttokens` / `recoveries`, signature PNG du cadre
(trait noir, 3:2), corps chiffré comme l'appli (AES CryptoJS). Les constantes de chiffrement sont
relues dans le JavaScript public de l'appli à chaque démarrage. Les cours avec un document à
signer en plus ne sont pas gérés (à faire dans l'appli).

## Notes INSA

Mon Dossier Web est une appli Vaadin derrière le SSO de l'INSA : le bot ouvre un Chromium
headless (Playwright), se connecte avec `INSA_USERNAME` / `INSA_PASSWORD`, va dans
*Parcours → Notes et résultats* (chaque année d'inscription), déplie l'arborescence
(semestre › UE › matière › épreuve) et la compare au relevé précédent. Relevé toutes les
`NOTES_INTERVAL_MIN` minutes entre 7 h et 23 h. Le premier relevé ne déclenche aucune alerte.

Seules les matières et épreuves comptent (pas les moyennes d'UE / semestre), et l'annonce
liste les **matières** concernées, sans doublon. Les notes ne sont ni affichées ni stockées :
la base ne garde qu'une empreinte (SHA-256 tronquée) de chaque note pour détecter un changement.
Sans salon configuré (`/config notes`), l'annonce part en MP au propriétaire (`NOTES_OWNER_ID`).

**2FA** : les cookies de session sont gardés dans `data/insa-session.json`, le 2FA n'est donc
redemandé qu'à l'expiration de la session SSO. À ce moment-là : si `INSA_TOTP_SECRET` (clé du
QR code) est fourni, le bot génère le code seul ; sinon il envoie un MP au propriétaire et
attend `/notes code 123456` (10 min), puis met le relevé automatique en pause jusqu'au
prochain `/notes verifier`.


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
