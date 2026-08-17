# Agent SEO autonome pour idgarages.com

Un agent Python qui crawle idgarages.com, détecte les erreurs SEO (404, 403, 5xx, chaînes de redirection, timeouts) **en gardant trace de la page qui contient chaque lien cassé**, les stocke en local, et envoie un **rapport hebdomadaire intelligent** synthétisé par **Claude (Anthropic)**.

> ## ▶️ Planification réactivée — une condition à connaître
>
> Les blocs `schedule:` sont rétablis dans les deux workflows : crawl quotidien à 03h00 UTC, rapport le vendredi à 10h00 UTC.
>
> **Mais un cron GitHub Actions ne se déclenche que depuis la branche par défaut du dépôt.** Tant que ces workflows vivent sur une branche de travail, le `schedule:` est inerte et seul le lancement manuel (*Run workflow*) fonctionne. Il faut fusionner dans la branche par défaut pour que l'agent tourne réellement.

---

## 1. Vue d'ensemble du fonctionnement

Les horaires ci-dessous sont ceux **configurés**. Ils s'appliquent dès que les workflows sont sur la branche par défaut (voir l'encadré ci-dessus), ou en mode démon local (§4).

```
┌─────────────────────────────────────────────────────────────┐
│  Tous les jours à 03h00                                     │
│  ─────────────────────────                                  │
│  1. Sélectionne jusqu'à 500 URLs (jamais crawlées d'abord) │
│  2. Charge robots.txt AVEC le navigateur (donc via le CDN) │
│  3. Crawle chaque page (1 req/sec), max 20 min par cycle   │
│  4. Détecte 4xx, 5xx, redirections en chaîne, timeouts     │
│  5. Stocke tout dans SQLite (data/seo_monitor.db)          │
│  6. Découvre de nouvelles URLs → file pour les cycles +1   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Vendredi à 11h00                                           │
│  ────────────────                                           │
│  1. Lit toutes les erreurs des 7 derniers jours            │
│  2. Génère 3 graphiques PNG (charts.py)                    │
│  3. Envoie à Claude (claude-opus-5) pour synthèse          │
│  4. Claude identifie patterns, priorités, recommandations  │
│  5. Envoi du HTML + graphiques inline (Gmail SMTP)         │
└─────────────────────────────────────────────────────────────┘
```

**Un cycle qui ne visite aucune page est un échec, pas un succès à 0 erreur.** Si le site est injoignable ou si `robots.txt` interdit tout, le cycle est marqué `failed`, `python main.py crawl` sort en code ≠ 0 et le workflow passe au rouge — au lieu de laisser le rapport du vendredi annoncer « tout va bien » après n'avoir regardé aucune page.

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
- Va sur https://platform.claude.com/settings/keys
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
├── database.py          # SQLite : schéma, migrations, requêtes
├── crawler.py           # Crawl Playwright + extraction de liens
├── analyzer.py          # Synthèse via Claude (Anthropic)
├── charts.py            # Graphiques PNG du rapport (matplotlib)
├── mailer.py            # Envoi SMTP Gmail + images inline
├── main.py              # Orchestration + boucle schedule
├── .github/workflows/   # daily-crawl.yml + weekly-report.yml
├── docs/samples/        # Aperçus PNG des 3 graphiques (+ _generate.py)
└── data/
    ├── seo_monitor.db   # Base SQLite (créée automatiquement)
    └── agent.log        # Journal (mode local uniquement)
```

### 3.1 Le crawler (`crawler.py`)
- **Playwright / Chromium**, pas `requests` : le CDN du site bloque toute requête non-navigateur.
- **`robots.txt` est chargé avec le même navigateur**, donc il passe le CDN comme le reste. Le lire avec `urllib` se faisait bloquer, et un 403 sur `robots.txt` est interprété par `RobotFileParser` comme « tout est interdit » — ce qui transformait silencieusement le cycle en no-op.
- **Un `robots.txt` illisible n'interdit rien** : warning dans les logs, et le crawl continue. Seule une règle réellement lue et parsée peut bloquer une URL.
- Classe les erreurs : `not_found_404`, `forbidden_403`, `server_error_5xx`, `client_error_<code>`, `long_redirect_chain` (≥ `MAX_REDIRECT_HOPS`, compté via la chaîne `redirected_from` de Playwright), `timeout`, `navigation_error`.
- **Chaque erreur retient sa page d'origine** (`found_on`) : c'est la page qui contient le lien cassé, donc celle à corriger.
- Parse le HTML avec BeautifulSoup, extrait les `<a href>`, garde uniquement le même domaine.
- Politesse : 1 seconde entre chaque requête (configurable).
- **Budget temps** (`MAX_CRAWL_MINUTES`, 20 min) : le cycle s'arrête proprement, sauvegarde ce qu'il a trouvé, et la rotation reprend au cycle suivant. Sans ça, 500 pages peuvent dépasser le timeout du runner, qui tue le job et fait perdre tout le travail.
- Anti-détection `playwright-stealth` (API v1 **et** v2 supportées) ; si elle ne peut pas s'appliquer, **un warning est émis** au lieu d'un échec silencieux.

### 3.2 La base de données (`database.py`)
- 3 tables : `urls`, `errors`, `crawl_runs`
- Rotation via `ORDER BY last_crawled IS NULL DESC, last_crawled ASC` — les URLs jamais vues d'abord, puis les plus anciennes
- **Tous les timestamps sont en UTC**, comme le `CURRENT_TIMESTAMP` de SQLite, et les bornes de fenêtre utilisent exactement le format `YYYY-MM-DD HH:MM:SS`. Avec `isoformat()` (qui insère un `T`), la comparaison de chaînes fait passer tout le jour-limite sous le seuil et la fenêtre « 7 jours » en perd un.
- `init_db()` applique les migrations manquantes (`ALTER TABLE`) : `CREATE TABLE IF NOT EXISTS` ne touche pas une table existante, or la base vit sur la branche `data`.

### 3.3 L'analyzer Claude (`analyzer.py`)
- Modèle : `claude-opus-5` avec **adaptive thinking** (Claude décide combien réfléchir)
- Effort : `high` pour un raisonnement approfondi
- **Streaming activé** car le rapport peut être long
- `CLAUDE_MAX_TOKENS` (32000) plafonne le thinking **et** le HTML : trop bas, le rapport est coupé en plein milieu d'une balise
- **Vérifie `stop_reason`** : un refus ou une réponse vide bascule sur le rapport de secours, une troncature ajoute un bandeau d'avertissement visible dans le mail — jamais d'envoi d'un rapport tronqué qui a l'air complet
- Pré-agrège les données (compteurs par type, par section, par code HTTP) → moins de tokens, plus de pertinence
- Transmet `found_on` à Claude pour qu'il regroupe les liens cassés par page fautive
- Mode de secours : si l'API Anthropic est down, génère un rapport brut local (jamais de blackout)

### 3.3 bis Les graphiques (`charts.py`)
- 3 PNG générés en mémoire : évolution sur 7 jours, top sections, répartition par type
- Backend matplotlib `Agg` (headless, compatible CI)
- Attachés au mail en `Content-ID` et référencés par `<img src="cid:…">`
- Générés **avant** l'appel à Claude : ils sont présents même si l'API est down
- Aperçus dans [`docs/samples/`](docs/samples/), régénérables avec `python docs/samples/_generate.py`

### 3.4 Le mailer (`mailer.py`)
- SMTP Gmail en TLS sur port 587
- Email 100% HTML avec styles inline (compatible Gmail/Outlook)
- Structure `multipart/related` (RFC 2387) pour les graphiques inline
- Objet et pied de page **dérivés de la config** (`TARGET_SITE`, `WEEKLY_REPORT_DAY/TIME`) : changer de site ne laisse plus « idgarages.com » figé dans l'objet

### 3.5 Le scheduler (`main.py`)
- Utilise la lib `schedule` (légère, pas de cron requis)
- 2 jobs : `daily_crawl_job` + `weekly_report_job`, aux jour/heure configurés
- **Robustesse** : un crash dans un job n'arrête jamais la boucle principale
- **Codes de sortie** : `main.py crawl` et `main.py report` sortent en ≠ 0 en cas d'échec, pour que GitHub Actions passe au rouge au lieu d'échouer en silence

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
[INFO] Cycle : 1 pages à visiter (budget 20 min)
[INFO] robots.txt chargé (42 lignes)
[INFO] === Cycle terminé : 1 pages crawlées, 0 erreurs, 47 nouvelles URLs (0 ignorées par robots.txt) ===
```
Le premier cycle crawle uniquement la racine (`/`) et découvre des dizaines de liens. Les cycles suivants exploreront ces nouvelles URLs.

Deux messages à connaître :
- `robots.txt inaccessible (HTTP 403) — aucune restriction appliquée` : le CDN bloque `robots.txt`. Le crawl **continue** ; passe `RESPECT_ROBOTS_TXT=false` pour rendre ce choix explicite.
- `Cycle en échec : N URLs sélectionnées, 0 page crawlée` : rien n'a pu être visité. La commande sort en code ≠ 0. Vérifie que le site répond et ce que dit son `robots.txt`.

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
R : Édite `CLAUDE_MODEL` dans `config.py`. Tarifs par million de tokens (entrée / sortie) :

| Modèle | Entrée | Sortie | vs Opus 5 |
|---|---|---|---|
| `claude-opus-5` (défaut) | 5 $ | 25 $ | — |
| `claude-sonnet-5` | 3 $ | 15 $ | ~1,7× moins cher |
| `claude-haiku-4-5` | 1 $ | 5 $ | 5× moins cher |

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
┌─ daily-crawl.yml ──── ⏸ manuel (cron retiré) ────────────────┐
│  1. checkout code + Python 3.11 + dépendances                │
│  2. pull la DB depuis la branche orpheline `data`            │
│  3. python main.py crawl                                      │
│  4. force-push la DB mise à jour vers la branche `data`      │
└──────────────────────────────────────────────────────────────┘

┌─ weekly-report.yml ── ⏸ manuel (cron retiré) ────────────────┐
│  1. checkout + dépendances                                   │
│  2. pull la DB depuis `data`                                 │
│  3. python main.py report   →  email envoyé via Gmail        │
└──────────────────────────────────────────────────────────────┘
```

> 📌 La DB SQLite est stockée sur une **branche `data`** orpheline (créée auto au 1er run). À chaque crawl, GitHub Actions force-push la version à jour. Pas de service externe, pas de stockage à gérer.

L'étape de sauvegarde tourne en `if: always()` : même un cycle interrompu a découvert des URLs et journalisé des erreurs, et les jeter ferait repartir de zéro le lendemain.

### ▶️ Planification automatique

Les deux workflows portent leur `schedule:` — `0 3 * * *` pour le crawl, `0 10 * * 5` pour le rapport — en plus du `workflow_dispatch` qui reste disponible à tout moment.

**Condition indispensable :** GitHub Actions n'exécute les crons que depuis la **branche par défaut** du dépôt. Sur une branche de travail, le bloc `schedule:` est présent mais ne se déclenche jamais. Vérifie dans *Settings → Branches* quelle est la branche par défaut, et fusionne-y ces workflows pour que l'agent tourne réellement.

Pour remettre l'agent en pause : retire les deux lignes `schedule:` / `- cron:` en tête de chaque fichier, ou désactive le workflow depuis l'onglet *Actions*.

> ⚠️ Le rapport hebdomadaire **envoie un vrai email** et consomme du crédit API. Fais un lancement manuel de bout en bout avant de compter sur le cron.

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

Toutes optionnelles — non définie, chaque variable retombe sur son défaut dans `config.py`.

| Nom | Défaut | À quoi ça sert |
|---|---|---|
| `TARGET_SITE` | `https://www.idgarages.com` | Site à surveiller |
| `MAX_PAGES_PER_DAY` | `500` | Plafond d'URLs par cycle |
| `MAX_CRAWL_MINUTES` | `20` | Budget temps d'un cycle. **Doit rester sous le `timeout-minutes: 30` du workflow** |
| `CRAWL_DELAY_SECONDS` | `1.0` | Pause entre deux pages (politesse) |
| `MAX_REDIRECT_HOPS` | `3` | Seuil de signalement d'une chaîne de redirection |
| `RESPECT_ROBOTS_TXT` | `true` | `false` pour ignorer `robots.txt` — légitime sur ton propre site |
| `CLAUDE_MAX_TOKENS` | `32000` | Budget tokens du rapport (thinking + HTML) |
| `WEEKLY_REPORT_DAY` / `_TIME` | `friday` / `11:00` | Affichés dans le pied de page du mail |

> 💡 Pourquoi *secrets* vs *variables* ? Les **secrets** sont chiffrés et masqués dans les logs (clé API, mots de passe). Les **variables** sont en clair (config non sensible).

### Premier lancement

1. Va dans l'onglet **Actions** de ton repo
2. Clique sur **Daily SEO Crawl** dans la barre latérale
3. Clique **Run workflow** → sélectionne ta branche par défaut (`main`) → **Run workflow**
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
| Minutes GitHub Actions (repo privé) | 2000 / mois | jusqu'à ~680 / mois |
| Minutes GitHub Actions (repo public) | illimité | — |
| Anthropic API | facturé à l'usage | ~0,05-0,25 € / rapport (1 par semaine) |
| Gmail SMTP | 500 mails/jour | 1 mail/semaine |

Le calcul des minutes : `MAX_CRAWL_MINUTES` (20) + ~2 min d'installation (dont le téléchargement de Chromium) = ~22 min par crawl quotidien, soit ~660/mois, plus ~12 min pour les 4 rapports. **Baisse `MAX_CRAWL_MINUTES` si tu veux réduire la facture de minutes** — la rotation garantit que le site finit par être couvert quand même, juste plus lentement.

**Coût total : 0 € + ~1 €/mois Anthropic** (une fois la planification réactivée ; à l'arrêt, 0 €).

### Quelques notes

- Le cron GitHub Actions n'est **pas précis à la seconde** — un retard de 5-30 min en heure de pointe est possible. Pour un rapport hebdomadaire, c'est sans impact.
- Si tu veux changer l'heure : édite `.github/workflows/*.yml`, ligne `cron:`. Le format est `min h * * jour-de-semaine` (0=dim, 5=vendredi).
- La branche `data` ne doit **jamais** être supprimée — c'est ta mémoire à long terme. Elle est protégée par défaut puisqu'aucun humain ne push dessus.

---

## 9. Sécurité et bonnes pratiques

- 🔐 **Ne jamais commiter `.env`** — il contient ta clé API Anthropic et ton mot de passe Gmail
- 🐢 **Respect du site** : 1 req/sec par défaut, 1 page à la fois, budget temps borné, et `robots.txt` respecté dès qu'il est lisible
- 🎭 **User-Agent** : l'agent se présente comme **Chrome**, pas comme un bot. C'est ce qui lui permet de passer le CDN anti-bot — c'est acceptable ici parce qu'on surveille **son propre site**, et ça ne le serait pas sur un site tiers. Corollaire : une règle `robots.txt` visant un nom de bot précis ne s'appliquera pas ; seules les règles `User-agent: *` sont prises en compte.
- 📊 **Données locales** : tout est stocké en SQLite sur ta machine, rien n'est envoyé à des tiers (sauf le résumé hebdomadaire à Claude)
- 🔄 **Idempotence** : tu peux relancer `init`, `crawl` et `report` sans risque, ils ne corrompent rien

---

*Agent construit avec Claude Code · Synthèse propulsée par Claude Opus 5*
