# MenuCrous — Bot Discord · Manufacture des Tabacs (Lyon 3)

Bot Discord qui envoie automatiquement chaque matin à **7h00** le menu du restaurant CROUS Manufacture des Tabacs (Lyon 3).

---

## Prérequis

- Python **3.10+**
- Un compte développeur Discord

---

## Installation

### 1. Cloner / copier le projet

```bash
cd MenuCrous
```

### 2. Créer un environnement virtuel

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Configurer les variables d'environnement

Copier `.env.example` en `.env` et remplir les valeurs :

```bash
copy .env.example .env
```

Éditer `.env` :

```
DISCORD_TOKEN=votre_token_ici
CHANNEL_ID=id_du_channel_ici
```

---

## Créer le bot Discord

1. Aller sur [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. **New Application** → donner un nom
3. Onglet **Bot** → **Reset Token** → copier le token dans `.env`
4. Onglet **Bot** → activer l'intent **Message Content Intent**
5. Onglet **OAuth2 > URL Generator** →
   - Scopes : `bot`
   - Bot Permissions : `Send Messages`, `Read Message History`
6. Copier l'URL générée, l'ouvrir dans un navigateur et inviter le bot sur votre serveur

---

## Récupérer l'ID du channel

1. Dans Discord : **Paramètres utilisateur > Avancé** → activer le **Mode développeur**
2. Clic droit sur le channel cible → **Copier l'identifiant**
3. Coller dans `.env` → `CHANNEL_ID=...`

---

## Lancer le bot

```bash
python bot.py
```

Le bot enverra le menu chaque matin à **7h00 (heure de Paris)**.

### Commande manuelle

Dans n'importe quel channel où le bot est présent :

```
!menu
```

---

## Structure du projet

```
MenuCrous/
├── bot.py          # Bot Discord (tâche planifiée + commande !menu)
├── scraper.py      # Scraping du menu via Playwright + BeautifulSoup
├── .env            # Variables d'environnement (ne pas commit !)
├── .env.example    # Modèle de configuration
├── requirements.txt
└── README.md
```

---

## Lancer en continu (optionnel)

Pour garder le bot actif en arrière-plan sous Windows, tu peux créer une tâche planifiée Windows ou utiliser un outil comme [NSSM](https://nssm.cc/) pour l'exécuter comme un service.

Exemple simple avec PowerShell en arrière-plan :

```powershell
Start-Process python -ArgumentList "bot.py" -WindowStyle Hidden
```
