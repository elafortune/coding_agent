# Mode d'emploi — reproduire ce projet pas à pas

Ce fichier documente chaque étape avec les commandes exactes et le raisonnement, pour que le projet soit reproductible par quelqu'un d'autre (ou par moi dans 6 mois). Le suivi narratif des décisions/blocages est dans `projet_agentique_1.md` (préparation entretien) ; ici c'est le mode d'emploi technique.

Prérequis : Python 3.10+ (testé avec 3.14), Docker Desktop, une clé API Gemini gratuite ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)).

## Étape 0 — Setup de l'environnement

```bash
cd ~/swe_bench_agent
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash ; .venv/bin/activate sous Linux/Mac
pip install -r requirements.txt
```

Créer `.env` à partir de `.env.example` et y coller sa clé Gemini :

```
GEMINI_API_KEY=<ta clé>
```

**Piège local (spécifique à cette machine, à vérifier si `datasets`/`huggingface_hub` échoue avec une erreur SSL)** : si une variable d'environnement `SSL_CERT_FILE` pointe vers un chemin invalide (ex. un ancien miniconda supprimé), tout accès réseau Python casse silencieusement avec `FileNotFoundError` sur le contexte SSL. Fix : `unset SSL_CERT_FILE` avant de lancer les scripts Python de ce projet (le bundle de certificats par défaut de `certifi`, installé avec les dépendances, prend le relais).

## Étape 1 — Sélection du sous-ensemble SWE-bench Lite

```bash
unset SSL_CERT_FILE   # si le piège ci-dessus s'applique
python src/select_subset.py
```

Ce que fait le script (`src/select_subset.py`) :
1. Charge `princeton-nlp/SWE-bench_Lite` (300 instances, 12 dépôts) depuis Hugging Face.
2. Calcule pour chaque instance des statistiques sur le patch de référence (nb fichiers changés, lignes ajoutées/retirées) en parsant le diff.
3. Échantillonne **24 instances cible** (fourchette 20-30 décidée en cadrage), stratifié par dépôt : allocation proportionnelle à la fréquence de chaque dépôt dans le dataset complet, minimum 1 par dépôt pour garantir la diversité — `random.seed(42)` pour la reproductibilité.
4. Passe chaque instance sélectionnée dans un filtre de sanité (énoncé de bug trop court, tests `FAIL_TO_PASS` vides, patch anormalement gros) et signale les cas à vérifier à la main — sans les exclure automatiquement, pour ne pas biaiser l'échantillon vers les cas "faciles" (ça casserait l'argument "benchmark externe non trafiqué").
5. Écrit `data/swebench_lite_subset.json` (versionné dans le repo, pas gitignore).

Résultat obtenu : **27 instances**, 12/12 dépôts représentés, aucun cas dégénéré détecté (1 instance à 76 lignes signalée mais gardée intentionnellement pour la variété de difficulté).

Pour vérifier la reproductibilité (même sélection à chaque exécution) :
```bash
rm data/swebench_lite_subset.json
python src/select_subset.py
git diff data/swebench_lite_subset.json   # doit être vide
```

## Étape 2 — Sandbox Docker

**Décision d'architecture** : plutôt que d'écrire un système de sandbox Docker à partir de zéro (cloner chaque dépôt, deviner les commandes d'installation par dépôt/version...), ce projet s'appuie sur le harnais officiel [`swebench`](https://pypi.org/project/swebench/) (`pip install swebench`), le même outil utilisé pour le leaderboard public SWE-bench. Deux raisons : (1) c'est un travail d'infrastructure déjà résolu et non différenciant pour ce projet — la valeur ajoutée est dans l'agent, pas dans la plomberie Docker ; (2) ça garantit que le protocole d'évaluation est identique à celui utilisé pour publier des résultats officiels, donc directement comparable.

```bash
pip install swebench
swebench eval SWE-bench/SWE-bench_Lite --gold -i <instance_id> --run-id smoke_test -j 2
```

`swebench eval <dataset> --gold` : évalue le patch de référence (censé toujours résoudre l'instance — sert de test de bon fonctionnement du pipeline avant de brancher un vrai agent). Sans `--task-repo`, le harnais **récupère des images Docker pré-construites** depuis le registre (`swebench/sweb.eval.x86_64.<repo>_<id>`) plutôt que de tout construire localement — évite des heures de build sur un laptop.

Dataset : `SWE-bench/SWE-bench_Lite` (pas `princeton-nlp/SWE-bench_Lite`, qui est l'ancien hébergement sans le champ `image` requis par ce harnais — les deux contiennent les mêmes instances, vérifié par intersection des `instance_id`).

### Blocage majeur — corruption CRLF sous Windows, migration vers WSL2/Ubuntu

**Symptôme** : `swebench eval --gold` sur Windows natif donnait `Instances resolved: 0` même avec le patch de référence — alors qu'un patch de référence doit *toujours* résoudre l'instance par définition.

**Diagnostic** : lecture du `test_output.txt` de l'instance (`logs/run_evaluation/<run_id>/gold/<instance_id>/test_output.txt`) — chaque commande du script `eval.sh` échouait avec un `\r` fantôme en fin d'argument (`cd '/testbed\r'`, `git checkout <hash>\r`). Cause racine trouvée dans le code source du harnais (`swebench/harness/run_evaluation.py:359`) : `eval_file.write_text(...)` écrit le script bash en mode texte, et Windows convertit silencieusement chaque `\n` en `\r\n` — le script, une fois copié dans le conteneur Linux, devient illisible.

**Décision** : plutôt qu'un patch chirurgical sur le package installé (rapide mais qui laisse la classe de bug intacte pour d'autres fichiers écrits par le harnais), migration de tout le projet vers **WSL2 + Ubuntu 24.04 LTS** — élimine la source du problème plutôt que de la contourner instance par instance, et plus représentatif de l'environnement Linux visé par cet écosystème.

```powershell
wsl --install -d Ubuntu-24.04
```

Puis, dans un terminal interactif (WSL a besoin d'un vrai terminal pour l'assistant de création d'utilisateur — pas une commande lancée en tâche de fond sans entrée standard, sous peine de bloquer indéfiniment sur l'écran "En attente de la fin de la commande OOBE") :

```bash
wsl -d Ubuntu-24.04   # suivre l'invite "Enter new UNIX username:"
```

Activer l'intégration Docker Desktop pour cette distro : Docker Desktop → Settings → Resources → WSL Integration → activer "Ubuntu-24.04" → Apply & Restart.

**Où vivent le code et l'environnement virtuel** : après investigation, la cause du bug CRLF est l'OS de l'**interpréteur Python** (Windows traduit `\n`→`\r\n` en écriture texte, Linux non) -- pas l'emplacement des fichiers sur le disque. Le projet reste donc à un seul endroit, `C:\Users\<user>\swe_bench_agent` (accessible depuis WSL en `/mnt/c/Users/<user>/swe_bench_agent`) : c'est le chemin canonique, édité normalement depuis Windows. Seul l'environnement virtuel Python doit être natif Linux, car `python3 -m venv` échoue silencieusement sur `/mnt/c` (drvfs ne supporte pas correctement les liens symboliques utilisés par `venv`) :

```bash
sudo apt update && sudo apt install -y python3.12-venv python3-pip   # requiert un mot de passe interactif, une seule fois

mkdir -p ~/.venvs
python3 -m venv ~/.venvs/swe_bench_agent      # venv natif Linux, hors du projet
source ~/.venvs/swe_bench_agent/bin/activate

cd /mnt/c/Users/<user>/swe_bench_agent        # le code, lui, reste sur le chemin Windows
pip install -r requirements.txt swebench
```

**Résultat** : `swebench eval SWE-bench/SWE-bench_Lite --gold -i astropy__astropy-7746 -i pylint-dev__pylint-6506 --run-id smoke_test -j 2` → `Instances resolved: 2/2`, y compris avec le projet sur `/mnt/c` (confirme que seul l'interpréteur comptait). Pipeline validé de bout en bout (chargement dataset → pull image → application patch → exécution tests → grading).

**À partir d'ici, toutes les commandes Python de ce projet s'exécutent via ce venv WSL** : `wsl -d Ubuntu-24.04 -- bash -lc 'source ~/.venvs/swe_bench_agent/bin/activate && cd /mnt/c/Users/<user>/swe_bench_agent && ...'` depuis Windows, ou directement dans un terminal WSL -- plus depuis Git Bash/PowerShell natif pour tout ce qui touche `swebench`/Docker.

## Étape 3 — Outils de l'agent

`src/sandbox.py` : classe `Sandbox`, un conteneur Docker par instance (démarré depuis `instance["image"]`, sans volume monté depuis l'hôte -- l'agent ne touche jamais le vrai système de fichiers). `exec()` lance une commande avec l'environnement conda `testbed` déjà activé (`source /opt/miniconda3/bin/activate && conda activate testbed`, chemin standardisé par le harnais sur toutes les images, vérifié sur 2 dépôts différents). `write_file()` écrit un fichier dans le conteneur via une archive tar en mémoire (l'API Docker n'expose pas d'écriture directe -- c'est l'équivalent programmatique de `docker cp`).

`src/tools.py` : 5 outils construits sur `Sandbox` --
- `search_code(sandbox, pattern, path)` : `grep -rn` sur les fichiers `.py`
- `read_file(sandbox, path, start, end)` : lecture avec numéros de ligne, plage optionnelle
- `apply_patch(sandbox, diff_text)` : écrit le diff dans le conteneur, `git apply --check` puis `git apply` -- renvoie l'erreur de git en cas d'échec (pour que l'agent puisse corriger et réessayer)
- `run_tests(sandbox, test_ids)` : `pytest` sur une liste d'identifiants de test
- `run_command(sandbox, cmd)` : commande arbitraire (bash générique)
- `get_diff(sandbox)` : `git diff` courant -- c'est le patch final soumis à l'évaluation

**Piège méthodologique découvert en testant `run_tests`** : les tests listés dans `FAIL_TO_PASS` n'existent souvent pas encore au commit de base -- ce sont des tests *ajoutés* par le PR de référence (`instance["test_patch"]`, distinct du patch de code `instance["patch"]`). Le protocole correct est : (1) appliquer `test_patch` (ajoute les tests, qui échouent puisque le bug est encore là), (2) appliquer le patch de code, (3) les tests passent. **`test_patch` fait partie du harnais d'évaluation, jamais montré à l'agent en conditions réelles** -- il sert uniquement ici à vérifier mécaniquement que `run_tests`/`apply_patch` fonctionnent, pas à donner un avantage à l'agent plus tard (Étape 4).

Vérification (`src/test_tools_manual.py`, hors graphe, sur `astropy__astropy-7746`) :
```bash
python src/test_tools_manual.py
```
Résultat : `search_code`/`read_file` corrects, `apply_patch(test_patch)` → test échoue avec exactement l'erreur du rapport de bug (`InconsistentAxisTypesError`), `apply_patch(patch de référence)` → appliqué proprement, `run_tests` après → `1 passed`. Les 5 outils sont validés avant intégration dans LangGraph.

## Étape 4 — Graphe LangGraph

**Décision d'architecture** : plutôt qu'un graphe rigide à 5 nœuds forçant Planning → Exploration → Edit → Test → Reflection dans cet ordre exact, `src/agent.py` implémente une boucle contrôlée à 3 nœuds -- `agent` (appel Gemini avec les 5 outils déclarés en function-calling), `tools` (exécution de l'outil demandé dans le `Sandbox`), `nudge` (rappel si le modèle répond sans appeler d'outil) -- avec deux conditions de sortie explicites : le modèle appelle `submit()`, ou le budget d'itérations est épuisé. Raison : un agent réel alterne naturellement exploration/édition/test sans respecter un ordre de phases rigide (relire un fichier après un patch, retester après une exploration...) ; forcer 5 états stricts n'ajouterait pas de contrôle réel au-delà de ce que le graphe (nœuds + routage conditionnel géré par LangGraph, pas une boucle Python `while` libre) fournit déjà, et nuirait à la performance de l'agent. La différence avec une boucle ReAct "fragile" (le piège explicitement cité dans `projets_concrets.md`) : jeu d'outils fermé et défini à l'avance, routage géré par LangGraph plutôt qu'un `while True` implicite, et budget d'itérations strict.

**Important -- ce que l'agent ne voit jamais** : `test_patch` et les identifiants `FAIL_TO_PASS` ne sont jamais injectés dans le prompt ni exposés comme outil -- l'agent doit juger par lui-même quand son correctif est prêt, avec les tests déjà présents dans le dépôt (via `run_command`). C'est ce qui rend l'évaluation finale (Étape 5, via le harnais officiel) non triviale.

Modèle choisi : `gemini-2.5-flash` (liste des modèles disponibles vérifiée via `client.models.list()` plutôt que devinée -- l'écosystème Gemini a beaucoup évolué après la date de coupure des connaissances de l'agent qui a écrit ce code). Nom du modèle centralisé dans `agent.MODEL_NAME`, facile à changer pour comparer.

**Bug trouvé au premier test réel** (`AttributeError: 'NoneType' object has no attribute 'parts'`) : sur certaines réponses, `response.candidates[0].content` est `None` (réponse vide/bloquée -- `finish_reason` autre que STOP). Fix : `agent_node` construit un `Content` de repli avec le `finish_reason` en texte quand `content` est `None` ou sans parts, plutôt que de laisser planter le routage.

Test de câblage (`src/test_agent_manual.py <instance_id> <budget>`) sur 2 instances de dépôts différents :
```bash
python src/test_agent_manual.py astropy__astropy-7746 10
python src/test_agent_manual.py pylint-dev__pylint-6506 10
```
Résultat mécanique : les deux tournent sans planter (6 et 9 itérations), appellent `submit()` normalement, produisent un patch extrait via `get_diff`. Résultat qualitatif (noté par le harnais officiel, `swebench eval ... -p <preds.jsonl> -i <instance_id>`) : **aucune des deux instances n'est "resolved"** -- le patch astropy est incomplet (ne traite que le cas où *tous* les axes sont vides, pas le cas mixte du test réel, et introduit 7 régressions sur des tests auparavant passants) ; le patch pylint est plausible (gère la bonne exception au bon endroit conceptuellement) mais ne satisfait pas non plus le test caché. **Résultat attendu et honnête à ce stade** : le graphe fonctionne, l'agent raisonne de façon cohérente, mais un modèle "flash" gratuit sur un budget de 10 tours ne résout pas tout -- exactement le type de signal nuancé (taux de résolution, modes d'échec) que l'Étape 5 (éval complète) et l'Étape 6 (analyse qualitative) doivent quantifier plutôt que cacher.

## Étape 5 — Éval complète (27 instances)

```bash
python src/run_eval.py --max-iterations 15
# écrit evals/results/preds_full.jsonl + evals/results/agent_runs.jsonl (trace détaillée par instance)

swebench eval SWE-bench/SWE-bench_Lite -p evals/results/preds_full.jsonl \
  --run-id full_eval --report-dir evals/results -j 1
```

**Piège d'infrastructure (pas de l'agent)** : la machine hôte est tombée à moins d'1 Go de RAM libre (15,4 Go au total) pendant les runs -- deux tentatives de notation ont été tuées par le protecteur mémoire du système, laissant des conteneurs orphelins (`docker ps -a`, à nettoyer avec `docker rm -f` avant de relancer). `swebench eval` reprend intelligemment là où il s'est arrêté (`N instances already run, skipping...`), donc rien n'a été reperdu. `-j 1` (séquentiel) plutôt que `-j 4` pour limiter l'empreinte mémoire de plusieurs conteneurs simultanés.

**Résultat** (27 instances, budget 15 tours, `gemini-2.5-flash`) :

| Statut | Nombre | % |
|---|---|---|
| **Resolved** | 6 | 22,2 % |
| Unresolved (patch produit, insuffisant) | 17 | 63,0 % |
| Empty patch (budget épuisé sans jamais patcher) | 4 | 14,8 % |
| Ambiguous (`no_tests_collected`, probable souci d'environnement) | 1 | inclus dans unresolved ci-dessus |

Statistiques (`evals/results/agent_runs.jsonl`) : itérations moyenne 11,6 (médiane 13, budget max 15) ; tokens moyenne 155 835/instance (médiane 86 571, total 4,2M sur les 27) ; temps moyen 140s/instance (total 63 min).

**Signal le plus intéressant** : les 4 instances à patch vide épuisent *systématiquement* les 15/15 itérations (jamais moins) -- ce n'est pas un échec aléatoire mais un comportement reproductible (boucle de tentatives infructueuses jusqu'au budget). Les instances "resolved" ont aussi un nombre d'itérations élevé en moyenne (13,2) -- proche du budget max, pas un signe de facilité. Les "unresolved" ont la moyenne la plus basse (10,2) -- l'agent soumet souvent un patch avant d'avoir épuisé son budget, confiant à tort. Analyse détaillée des mécanismes d'échec : Étape 6.

## Étape 6 — Analyse qualitative des échecs

**Limite honnête à poser avant toute interprétation** : rejouer exactement la même instance (`src/investigate_empty_patch.py`, avec capture de transcription complète) donne un résultat **différent** à chaque fois -- `pylint-dev__pylint-6506` est passé de patch vide (15 itérations, run complet) à patch non vide (6 itérations, rejeu) ; `sympy__sympy-23262` de patch vide (15 itérations, 594 507 tokens) à patch non vide (7 itérations, 183 228 tokens). Le taux de 22,2 % est donc une estimation à un seul tirage par instance, pas un taux de réussite stable du couple agent+modèle -- une évaluation rigoureuse demanderait plusieurs tirages par instance (hors budget de ce projet). C'est signalé explicitement plutôt que présenté comme un chiffre définitif.

**Mécanisme concret trouvé derrière le coût en tokens (confirmé par transcription, pas supposé)** : `read_file` sans `start`/`end` renvoie le fichier **entier** -- un seul appel sur `sympy/utilities/lambdify.py` a renvoyé 60 937 caractères. Comme l'historique complet de la conversation est renvoyé à chaque appel API suivant (pas de résumé/troncature), ce genre de dump se reproduit à l'identique dans le contexte de tous les tours suivants -- explique directement les instances à 500K-980K tokens. **C'est un défaut d'outil, pas une limite du modèle** : `read_file` (`src/tools.py`) n'impose aucune limite de taille ni ne pousse l'agent à utiliser `start`/`end` sur de gros fichiers. Amélioration identifiée pour une itération future (non appliquée ici, casserait la comparabilité avec les 27 instances déjà notées) : tronquer `read_file` au-delà d'un seuil (ex. 200 lignes) avec message explicite invitant à préciser une plage.

**Exemples concrets de réussite** (patchs produits par l'agent, vérifiés par le harnais officiel) :
- `sympy__sympy-23117` : bug `sympy.Array([])` plante alors que `sympy.Matrix([])` fonctionne (cas limite tableau vide non géré). L'agent localise la fonction récursive de construction (`ndim_array.py`) et ajoute un garde-fou minimal (`if not pointer: return [], (0,)`) -- 4 lignes, rien de cassé. Résolu en 10 itérations / 26 644 tokens, un des runs les plus efficaces.
- `psf__requests-1963` : bug plus subtil -- `Session.resolve_redirects` recopiait toujours la requête *originale* (`req.copy()`) à chaque étape d'une chaîne de redirections au lieu de partir de la requête *précédente*, faussant le calcul de la méthode HTTP sur une chaîne POST→303→GET→307. L'agent identifie qu'il faut chaîner (`prepared_request = prepared_request.copy()`) et lire la méthode depuis l'état courant. Demande de comprendre la logique temporelle du code, pas un pattern syntaxique -- patch en substance identique à ce qu'écrirait un développeur expérimenté.
- Point commun aux 6 réussites : corrections courtes et ciblées (3-10 lignes), bug bien circonscrit à une seule fonction.

**Modes d'échec qualitatifs observés** (voir aussi Étape 4 pour le détail des cas astropy/pylint) :
1. **Patch plausible mais incomplet** (ex. `astropy__astropy-7746` à l'Étape 4) : traite le cas général du rapport de bug mais pas un cas particulier du test caché (ex. axes partiellement vides vs tous vides) -- l'agent comprend le problème mais sous-généralise sa correction.
2. **Patch plausible mais avec régression** (même exemple) : introduit 7 échecs sur des tests auparavant passants -- corrige le symptôme sans comprendre l'invariant plus large de la fonction touchée.
3. **Budget épuisé sans patch** (4/27, 14,8 %) : boucle de recherche/lecture sans jamais atteindre un `apply_patch` réussi avant la limite de 15 tours -- corrélé avec un contexte gonflé (voir mécanisme ci-dessus), l'agent semble se noyer dans son propre historique plutôt que de converger.
4. **Cas ambigu** (`pytest-dev__pytest-5692`, `no_tests_collected`) : signalé par le harnais lui-même comme un problème d'environnement de test plutôt qu'un vrai échec de correction -- à ne pas compter comme un échec de raisonnement de l'agent.

**Conclusion honnête de l'Étape 6** : le taux de 22,2 % (probablement plus proche de 25-30 % sur plusieurs tirages, vu la variance observée) est un résultat solide pour un modèle gratuit sans fine-tuning et un harnais construit en quelques jours, mais le vrai insight n'est pas le chiffre -- c'est que **le principal facteur limitant identifié est l'ingénierie du contexte** (outils sans troncature, historique qui grossit sans borne) plutôt que le raisonnement du modèle lui-même, qui produit des patchs conceptuellement sensés même quand ils échouent.

## Itération d'amélioration -- 3 versions, un résultat plus nuancé qu'espéré

Décision (discussion avec l'utilisateur) : tester 3 pistes d'amélioration par ordre de priorité, mesurées par re-run complet des 27 instances à chaque étape --
1. Troncature `read_file` (corrige la cause prouvée des patchs vides)
2. Nœuds déterministes de garde-fou dans LangGraph : `baseline`/`verify` (non-régression sur les tests PASS_TO_PASS), `submit_blocked` (bloque une soumission non vérifiée une première fois), `checkpoint` (rappel à 70 % du budget)
3. Best-of-N (non implémenté, voir "pistes non retenues" plus bas)

Base de règles mathématiques/algorithmiques (4e piste proposée) écartée dès la discussion : n'aurait aidé que la frange Sympy, pas les 25 autres instances (Django, Flask, etc.) -- généralisée en "s'appuyer sur la suite de tests déjà existante du dépôt" (= Priorité 2).

### v1 (baseline) -- 22,2 % résolues, 4 patchs vides
Résultats déjà documentés ci-dessus.

### v2 (Priorité 1 + 2 implémentées) -- 18,5 % résolues, 11 patchs vides (régression apparente)

Implémentation : `read_file` plafonné à 200 lignes (`tools.py`) ; graphe étendu à 6 nœuds avec `verify` qui rejoue jusqu'à 30 tests PASS_TO_PASS après chaque `apply_patch` réussi et bloque `submit` une première fois en cas d'échec (`agent.py`).

**Bug trouvé par inspection de transcription (pas supposé)** : `matplotlib__matplotlib-22835` est passé de non-vide (v1) à vide (v2). Cause : un id de test paramétré du dataset lui-même est mal formé (`test_artist_inspector_get_valid_values[ACCEPTS:'` -- guillemet non échappé), pytest échoue à le *collecter* indépendamment de tout patch (`no match in any of [...]`). Mon `verify` traitait tout code de sortie pytest non-nul comme "régression", sans distinguer "erreur de collecte préexistante" de "vraie régression causée par le patch" -- l'agent, croyant à tort avoir cassé quelque chose, a tenté de "corriger" un problème fantôme jusqu'à corrompre ses propres tentatives de patch et finir à vide après 15 tours.

**Fix** : nouveau nœud `baseline` (point d'entrée du graphe, avant tout patch) qui capture le statut réel de chaque test PASS_TO_PASS *avant* modification. `verify` compare ensuite au statut de référence -- seul un test passé à la référence et cassé après coup compte comme régression ; un test déjà en échec/non collectable au départ est ignoré quel que soit son statut ensuite.

### v3 (Priorité 1 + 2 avec `verify` corrigé) -- 18,5 % résolues, 10 patchs vides

| Version | Résolues | Vides | Tokens moyens/instance | Itérations moyennes |
|---|---|---|---|---|
| v1 (baseline) | 6 (22,2 %) | 4 (14,8 %) | 155 835 | 11,6 |
| v2 (durci, bug verify) | 5 (18,5 %) | 11 (40,7 %) | -- | -- |
| v3 (durci, verify corrigé) | 5 (18,5 %) | 10 (37,0 %) | 114 532 | 10,7 |

Effets positifs confirmés par inspection directe (pas supposés) : 3 instances vides en v1 (`seaborn-3407`, `pylint-6506`, `sympy-15345`) sont redevenues non-vides en v3 -- cohérent avec la troncature qui règle le problème identifié à l'Étape 6. Coût moyen en tokens en baisse nette (155 835 → 114 532).

**Mais** : `sympy__sympy-20154`, résolue en v1, est vide en v3 -- investigation de sa transcription montre que `verify` n'est **jamais intervenu** sur ce cas : l'agent a généré 4 diffs mal formés d'affilée (`corrupt patch`), jamais appliqués avec succès, puis a soumis vide. Aucun rapport avec le durcissement -- c'est la même fragilité de génération de diff déjà vue ailleurs (non-déterminisme).

### Conclusion honnête -- pourquoi on s'arrête là plutôt que de sur-interpréter

Le nombre de résolues (6 → 5 → 5) varie de seulement 1 instance sur les trois versions -- **alors qu'on a démontré à plusieurs reprises dans ce projet que rejouer la même instance avec un code strictement identique donne parfois un résultat différent** (`pylint-6506`, `sympy-23262`, `astropy-7746` ont tous changé d'issue entre deux runs sans aucune modification de code). Avec un seul tirage par instance et par version (27 échantillons, pas de répétition), cet écart de 1 est **dans la marge de bruit qu'on a nous-mêmes mesurée** -- il serait malhonnête d'affirmer que le durcissement a clairement aidé ou clairement nui au taux de résolution brut sur cette seule base.

Ce qui est solidement établi, indépendamment du bruit sur le taux global :
1. Le bug de faux positif de v2 était réel, diagnostiqué par preuve directe (pas supposition), et corrigé.
2. La troncature de `read_file` a un effet positif mesurable et reproduit (3 cas concrets, coût moyen en tokens en baisse).
3. Les nœuds de garde-fou (`verify`/`submit_blocked`/`checkpoint`) fonctionnent mécaniquement comme conçu (vérifiés par transcription) mais leur effet net sur le taux de résolution n'est pas démontré sur un échantillon d'une seule mesure par instance.

**Pour trancher proprement** (non fait, cadrage identifié mais hors budget de ce projet) : il faudrait plusieurs tirages par instance et par version (ex. 3-5x), exactement l'idée de la Priorité 3 (best-of-N) envisagée mais non implémentée -- la vraie conclusion scientifique de cette itération est que **le pass@1 sur un tirage unique n'est pas une mesure fiable pour ce genre d'agent**, ce qui est en soi un résultat de recherche valide et un argument direct en faveur du best-of-N pour une itération future.

**Version retenue** : v3, pour la correction technique (le bug de v2 est un vrai bug, pas un choix de conception) et pour la baisse de coût observée -- pas parce que le taux de résolution serait prouvé meilleur.
