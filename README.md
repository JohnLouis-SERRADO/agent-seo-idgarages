# Agent SEO autonome pour idgarages.com

Un agent Python qui crawl idgarages.com chaque jour, détecte les erreurs SEO (404, 500, redirects, liens cassés, timeouts), les stocke en local, et envoie un **rapport hebdomadaire intelligent** chaque vendredi à 11h, synthétisé par **Claude (Anthropic)**.

---

## 1. Vue d'ensemble du fonctionnement

```
┌─────────────────────────────────────────────────────────────┐
│  Tous les jours à 03h00                                     │
│  ─────────────────────────                                  │
│  1. Sélectionne ~500 URLs (priorité aux jamais crawlées)   │
│  2. Crawle chaque page (respect robots.txt, 1 req/sec)     │
│  3. Détecte erreurs HTTP, redirects, timeouts              │
│  4. Stocke tout dans SQLite (data/seo_monitor.db)          │
│  5. Découvre de nouvelles URLs → file pour les jours +1    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Vendredi à 11h00                                           │
│  ────────────────                                           │
│  1. Lit toutes les erreurs des 7 derniers jours            │
│  2. Envoie à Claude (claude-opus-4-7) pour synthèse        │
│  3. Claude identifie patterns, priorités, recommandations  │
│  4. Envoi du rapport HTML par email (Gmail SMTP)           │
└─────────────────────────────────────────────────────────────┘
```

**Rotation des URLs** : à chaque cycle, on priorise les pages **jamais vues**, puis les plus **anciennement** crawlées. Une page crawlée hier ne sera pas re-crawlée aujourd'hui tant qu'il reste d'autres pages à voir. Cela permet de couvrir tout le site progressivement sans gaspillage.

---

## 2. Phase de préparation

### 2.1 Outils à installer

| Outil      | Version  | Pourquoi                                |
| ---------- | -------- | --------------------------------------- |
| Python     | 3.10+    | Le langage de l'agent                   |
| pip        | livré avec Python | Gestionnaire de paquets        |
| git        | optionnel | Pour récupérer le code                 |

### 2.2 Clés API à obtenir

**1. Clé API Anthropic (Claude)**
- Va sur https://console.claude.com/settings/keys
- Crée un compte (carte bancaire requise mais facturation à l'usage uniquement)
- Crée une nouvelle clé API : elle commence par `sk-ant-api03-…`
- Coût estimé : **~0,05 à 0,30 € par rapport** (un seul appel par semaine)

**2. Mot de passe d'application Gmail**
- Active la validation en 2 étapes sur https://myaccount.google.com/security
- Va sur https://myaccount.google.com/apppasswords
- Crée un mot de passe d'application nommé `agent-seo`
- Note les 16 caractères affichés (à coller dans `.env`, **sans espaces**)

### 2.3 Installation

```bash
# 1. Récupère le code
git clone <ce-repo>
cd agent-seo-idgarages

# 2. Crée un environnement virtuel (recommandé)
python3 -m venv .venv
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate            # Windows

# 3. Installe les dépendances
pip install -r requirements.txt

# 4. Configure tes secrets
cp .env.example .env
# Édite .env avec ton éditeur préféré : nano, vim, code, etc.
```

---

## 3. Architecture du code

```
agent-seo-idgarages/
├── .env                 # Tes secrets (NE PAS COMMITER)
├── .env.example         # Template à copier
├── requirements.txt     # Dépendances Python
├── config.py            # Charge .env → constantes typées
├── database.py          # SQLite : schéma + requêtes
├── crawler.py           # Crawl HTTP + extraction de liens
├── analyzer.py          # Synthèse via Claude (Anthropic)
├── mailer.py            # Envoi SMTP Gmail
├── main.py              # Orchestration + boucle schedule
└── data/
    └── seo_monitor.db   # Base SQLite (créée automatiquement)
```

### 3.1 Le crawler (`crawler.py`)
- Charge `robots.txt` une fois par cycle
- Pour chaque URL : `GET` avec timeout 15s, follow_redirects activé
- Classe les erreurs : `404`, `5xx`, `client_error_4xx`, `long_redirect_chain` (≥3 hops), `timeout`, `connection_error`
- Parse le HTML avec BeautifulSoup, extrait les liens `<a href>`, garde uniquement ceux du même domaine
- Politesse : 1 seconde entre chaque requête (configurable)

### 3.2 La base de données (`database.py`)
- 3 tables : `urls`, `errors`, `crawl_runs`
- Stratégie de rotation via `ORDER BY last_crawled IS NULL DESC, last_crawled ASC` — les URLs jamais vues d'abord, puis les plus anciennes

### 3.3 L'analyzer Claude (`analyzer.py`)
- Modèle : `claude-opus-4-7` avec **adaptive thinking** (Claude décide combien réfléchir)
- Effort : `high` pour un raisonnement approfondi
- **Streaming activé** car le rapport peut être long
- Pré-agrège les données (compteurs par type, par section, par code HTTP) avant de les envoyer à Claude → moins de tokens, plus de pertinence
- Mode de secours : si l'API Anthropic est down, génère un rapport brut local (jamais de blackout)

### 3.4 Le mailer (`mailer.py`)
- SMTP Gmail en TLS sur port 587
- Email 100% HTML avec styles inline (compatible Gmail/Outlook)

### 3.5 Le scheduler (`main.py`)
- Utilise la lib `schedule` (légère, pas de cron requis)
- 2 jobs : `daily_crawl_job` (tous les jours) + `weekly_report_job` (vendredi 11h)
- **Robustesse** : un crash dans un job n'arrête jamais la boucle principale

---

## 4. Planification (le "cerveau" temporel)

L'agent utilise la lib Python `schedule` pour gérer la planification. Configuration dans `.env` :

```env
DAILY_CRAWL_TIME=03:00       # Crawl tous les jours à 3h du matin
WEEKLY_REPORT_DAY=friday     # Jour d'envoi du rapport
WEEKLY_REPORT_TIME=11:00     # Heure d'envoi
```

**Pourquoi 3h du matin ?** Trafic faible sur idgarages.com → moins de risque de perturber le site, et le rapport du vendredi a déjà les données du vendredi matin.

### Alternative 1 : Cron (Linux/Mac)
Si tu préfères cron au lieu du démon Python :

```cron
# /etc/crontab ou crontab -e
0 3 * * *   cd /chemin/vers/agent-seo-idgarages && /chemin/vers/.venv/bin/python main.py crawl
0 11 * * 5  cd /chemin/vers/agent-seo-idgarages && /chemin/vers/.venv/bin/python main.py report
```

### Alternative 2 : Planificateur de tâches (Windows)
1. Ouvre le **Planificateur de tâches**
2. Crée une tâche → Déclencheur : *Quotidien à 03:00*
3. Action : *Démarrer un programme* → `C:\chemin\vers\.venv\Scripts\python.exe`
4. Arguments : `C:\chemin\vers\main.py crawl`
5. Répète l'opération pour le rapport vendredi 11h avec `main.py report`

---

## 5. Guide débutant : premier lancement

### Étape 1 — Initialise la base
```bash
python main.py init
```
Sortie attendue : `Base de données initialisée à .../data/seo_monitor.db`

### Étape 2 — Test du crawl (1 fois)
```bash
python main.py crawl
```
Tu devrais voir défiler :
```
[INFO] Cycle : 1 pages à visiter
[INFO] Cycle terminé : 1 pages crawlées, 0 erreurs, 47 nouvelles URLs
```
Le premier cycle crawl uniquement la racine (`/`) et découvre des dizaines de liens. Les cycles suivants exploreront ces nouvelles URLs.

### Étape 3 — Vérifie la base SQLite
```bash
sqlite3 data/seo_monitor.db "SELECT COUNT(*) FROM urls;"
sqlite3 data/seo_monitor.db "SELECT * FROM errors LIMIT 5;"
```

### Étape 4 — Test du rapport (force l'envoi maintenant)
```bash
python main.py report
```
Vérifie ta boîte mail. Si l'authentification SMTP échoue, c'est presque toujours qu'on a utilisé le mot de passe Google normal au lieu du **mot de passe d'application** (voir §2.2).

### Étape 5 — Lancement en arrière-plan
```bash
# Mode interactif (logs visibles, Ctrl+C pour arrêter)
python main.py

# Mode arrière-plan avec nohup (Linux/Mac)
nohup python main.py > /dev/null 2>&1 &

# Mode systemd (production Linux — recommandé)
# Voir le fichier exemple ci-dessous
```

#### Service systemd (production)
Crée `/etc/systemd/system/agent-seo.service` :
```ini
[Unit]
Description=Agent SEO idgarages.com
After=network.target

[Service]
Type=simple
User=ton_user
WorkingDirectory=/chemin/vers/agent-seo-idgarages
ExecStart=/chemin/vers/agent-seo-idgarages/.venv/bin/python main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Puis :
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agent-seo
sudo systemctl status agent-seo       # vérifie qu'il tourne
journalctl -u agent-seo -f            # suit les logs en direct
```

### Étape 6 — Vérification quotidienne (en cas de doute)
```bash
# Voir les logs récents
tail -f data/agent.log

# Compter les URLs connues
sqlite3 data/seo_monitor.db "SELECT COUNT(*) FROM urls;"

# Voir le dernier crawl
sqlite3 data/seo_monitor.db "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1;"
```

---

## 6. FAQ et dépannage

**Q : Le crawl ne trouve aucune URL.**
R : Vérifie que la racine est joignable : `curl -I https://www.idgarages.com`. Vérifie aussi `data/agent.log` pour des erreurs `robots.txt` ou `timeout`.

**Q : Erreur `SMTPAuthenticationError`.**
R : 99% du temps, tu as utilisé ton mot de passe Google au lieu d'un **mot de passe d'application** (16 caractères). Régénère-en un sur https://myaccount.google.com/apppasswords.

**Q : Le rapport hebdomadaire ne part pas.**
R : Vérifie `data/agent.log` autour de vendredi 11h. Pour forcer un envoi : `python main.py report`.

**Q : Combien coûte un rapport Claude ?**
R : Avec ~200 erreurs par semaine et le prompt actuel, on est entre **0,05 € et 0,30 €** par rapport. À l'année : **3 à 15 €**.

**Q : Comment changer de modèle Claude ?**
R : Édite `CLAUDE_MODEL` dans `config.py`. Options : `claude-sonnet-4-6` (3× moins cher, légère perte de profondeur) ou `claude-haiku-4-5` (10× moins cher, OK pour rapports simples).

**Q : Comment ajouter d'autres sites ?**
R : Change `TARGET_SITE` dans `.env`. Pour monitorer plusieurs sites en parallèle, duplique le dossier avec un `.env` différent pour chaque.

---

## 7. Checklist finale de déploiement (mode local / VPS)

> Pour le déploiement **GitHub Actions sans serveur**, voir §8 ci-dessous.


Avant de mettre en production, coche chaque case :

- [ ] Python 3.10+ installé (`python3 --version`)
- [ ] `pip install -r requirements.txt` exécuté sans erreur
- [ ] `.env` créé à partir de `.env.example`
- [ ] `ANTHROPIC_API_KEY` valide (commence par `sk-ant-api03-…`)
- [ ] Validation 2 étapes Gmail activée
- [ ] **Mot de passe d'application** Gmail généré (pas le mot de passe normal !)
- [ ] `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` renseignés dans `.env`
- [ ] `python main.py init` exécuté → fichier `data/seo_monitor.db` créé
- [ ] `python main.py crawl` testé → au moins 1 page crawlée, 0 exception
- [ ] `python main.py report` testé → email reçu dans la boîte de destination
- [ ] `.env` ajouté au `.gitignore` (jamais commiter les secrets !)
- [ ] Service systemd ou cron configuré pour le démarrage automatique
- [ ] Premier vendredi : confirmer la réception du vrai rapport hebdomadaire
- [ ] Logs surveillés pendant la première semaine (`tail -f data/agent.log`)

---

## 8. Déploiement sans serveur (GitHub Actions — recommandé si tu n'as pas de VPS)

Le repo contient déjà deux workflows GitHub Actions prêts à l'emploi dans `.github/workflows/`. Tout tourne **gratuitement** sur l'infra GitHub, sans serveur à entretenir.

### Comment ça marche

```
┌─ daily-crawl.yml ──── tous les jours 03h00 UTC ──────────────┐
│  1. checkout code + Python 3.11 + dépendances                │
│  2. pull la DB depuis la branche orpheline `data`            │
│  3. python main.py crawl                                      │
│  4. force-push la DB mise à jour vers la branche `data`      │
└──────────────────────────────────────────────────────────────┘

┌─ weekly-report.yml ── vendredi 10h00 UTC (= 11h Paris hiver) ┐
│  1. checkout + dépendances                                   │
│  2. pull la DB depuis `data`                                 │
│  3. python main.py report   →  email envoyé via Gmail        │
└──────────────────────────────────────────────────────────────┘
```

> 📌 La DB SQLite est stockée sur une **branche `data`** orpheline (créée auto au 1er run). À chaque crawl, GitHub Actions force-push la version à jour. Pas de service externe, pas de stockage à gérer.

### Configuration des secrets et variables (~3 minutes)

Va dans ton repo GitHub → **Settings → Secrets and variables → Actions**.

**Onglet "Secrets" → "New repository secret"** (à ajouter un par un) :

| Nom | Valeur |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-…` |
| `SMTP_USER` | `ton.email@gmail.com` |
| `SMTP_PASSWORD` | Le mot de passe d'application Gmail (16 caractères, sans espaces) |
| `EMAIL_FROM` | `ton.email@gmail.com` |
| `EMAIL_TO` | `destinataire@example.com` |

**Onglet "Variables" → "New repository variable"** :

| Nom | Valeur |
|---|---|
| `TARGET_SITE` | `https://www.idgarages.com` |
| `MAX_PAGES_PER_DAY` | `500` |

> 💡 Pourquoi *secrets* vs *variables* ? Les **secrets** sont chiffrés et masqués dans les logs (clé API, mots de passe). Les **variables** sont en clair (config non sensible).

### Premier lancement

1. Va dans l'onglet **Actions** de ton repo
2. Clique sur **Daily SEO Crawl** dans la barre latérale
3. Clique **Run workflow** → branche `claude/explain-functionality-Zm57g` (ou `main` une fois mergé) → **Run workflow**
4. Attends 1-2 min, ouvre le run pour vérifier les logs
5. Si tout est vert : la branche `data` est créée avec la DB initiale
6. Les exécutions suivantes seront automatiques selon le cron

### Vérification que le rapport hebdomadaire fonctionne

Avant d'attendre vendredi, lance le rapport manuellement :
1. Onglet **Actions** → **Weekly SEO Report** → **Run workflow**
2. Vérifie l'arrivée de l'email
3. Si erreur SMTP : c'est probablement le mot de passe d'application (pas le mot de passe Google normal)

### Quotas et coûts

| Élément | Limite gratuite | Ta conso estimée |
|---|---|---|
| Minutes GitHub Actions (repo privé) | 2000 / mois | ~450-500 / mois |
| Minutes GitHub Actions (repo public) | illimité | — |
| Anthropic API | facturé à l'usage | ~0,05-0,30 € / rapport (1 par semaine) |
| Gmail SMTP | 500 mails/jour | 1 mail/semaine |

**Coût total : 0 € + ~1 €/mois Anthropic.**

### Quelques notes

- Le cron GitHub Actions n'est **pas précis à la seconde** — un retard de 5-30 min en heure de pointe est possible. Pour un rapport hebdomadaire, c'est sans impact.
- Si tu veux changer l'heure : édite `.github/workflows/*.yml`, ligne `cron:`. Le format est `min h * * jour-de-semaine` (0=dim, 5=vendredi).
- La branche `data` ne doit **jamais** être supprimée — c'est ta mémoire à long terme. Elle est protégée par défaut puisqu'aucun humain ne push dessus.

---

## 9. Sécurité et bonnes pratiques

- 🔐 **Ne jamais commiter `.env`** — il contient ta clé API Anthropic et ton mot de passe Gmail
- 🐢 **Respect du site** : 1 req/sec par défaut, User-Agent identifiable, respect de robots.txt
- 📊 **Données locales** : tout est stocké en SQLite sur ta machine, rien n'est envoyé à des tiers (sauf le résumé hebdomadaire à Claude)
- 🔄 **Idempotence** : tu peux relancer `init`, `crawl` et `report` sans risque, ils ne corrompent rien

---

*Agent construit avec Claude Code · Synthèse propulsée par Claude Opus 4.7*
