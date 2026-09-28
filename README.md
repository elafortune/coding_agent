# Agent de résolution de bugs autonome — SWE-bench

Agent de code qui explore un dépôt, comprend un rapport de bug, écrit et vérifie un correctif dans un environnement sandboxé, sans jamais voir les tests qui serviront à le noter — évalué avec le harnais officiel [SWE-bench](https://www.swebench.com/) sur un sous-ensemble de 27 instances de `SWE-bench Lite`.

**Résultat central** : 5/27 instances résolues (18,5 %) avec `gemini-2.5-flash` (API gratuite, aucun fine-tuning), un budget de 15 tours et un jeu d'outils fermé. Mais le vrai résultat de ce projet n'est pas ce chiffre — c'est une leçon méthodologique trouvée en essayant de l'améliorer : **un seul tirage par instance n'est pas une mesure fiable** pour juger un changement d'architecture d'agent, un LLM à température non nulle donnant parfois une issue différente sur la même instance avec un code strictement identique. Le détail complet, y compris un bug de faux positif trouvé et corrigé en cours de route, est documenté dans [`step_by_step.md`](./step_by_step.md).

## Architecture

- **Sandbox** (`src/sandbox.py`) : un conteneur Docker par instance, construit à partir des images officielles du harnais SWE-bench (dépôt déjà cloné au bon commit, dépendances installées) — l'agent ne touche jamais le système de fichiers réel.
- **Outils** (`src/tools.py`) : `search_code` (grep), `read_file` (tronqué à 200 lignes sans plage explicite), `apply_patch` (diff unifié), `run_command` (bash arbitraire), `get_diff`.
- **Graphe LangGraph** (`src/agent.py`) : boucle contrôlée `agent ↔ tools`, avec trois nœuds déterministes de garde-fou plutôt qu'un séquencement rigide de phases —
  - `baseline` / `verify` : capture avant tout patch quels tests `PASS_TO_PASS` passent réellement, puis compare après chaque `apply_patch` réussi (jamais `FAIL_TO_PASS`/`test_patch`, jamais exposés à l'agent) ;
  - `submit_blocked` : bloque une soumission une première fois si une régression est détectée, laisse l'agent forcer une seconde fois ;
  - `checkpoint` : rappel explicite à 70 % du budget d'itérations si aucune soumission n'a encore eu lieu.
- **Évaluation** (`src/run_eval.py`) : génère les prédictions pour un ensemble d'instances, notées ensuite par le harnais officiel `swebench eval` (le même que celui du leaderboard public).

## Résultat détaillé

| Version | Résolues | Patchs vides | Tokens moyens/instance |
|---|---|---|---|
| v1 — sans garde-fous | 6/27 (22,2 %) | 4/27 (14,8 %) | 155 835 |
| v2 — garde-fous, bug de faux positif | 5/27 (18,5 %) | 11/27 (40,7 %) | — |
| v3 — garde-fous, bug corrigé | 5/27 (18,5 %) | 10/27 (37,0 %) | 114 532 |

Le bug de v2 : un identifiant de test paramétré du dataset SWE-bench lui-même est mal formé, pytest échoue à le collecter indépendamment de tout patch — le garde-fou de non-régression accusait l'agent d'avoir cassé quelque chose qu'il n'avait pas touché, le poussant à corrompre ses propres tentatives de correction. Diagnostiqué par inspection directe de la transcription de conversation, pas supposé. Voir [`step_by_step.md`](./step_by_step.md#itération-damélioration----3-versions-un-résultat-plus-nuancé-quespéré) pour l'enquête complète et la conclusion méthodologique.

## Lancer le projet

```bash
python -m venv .venv && source .venv/bin/activate  # nécessite un système Linux (WSL2 sous Windows) -- voir step_by_step.md
pip install -r requirements.txt
cp .env.example .env  # ajouter une clé API Gemini gratuite (aistudio.google.com/apikey)

python src/select_subset.py          # sélectionne le sous-ensemble d'instances
python src/run_eval.py               # fait tourner l'agent, écrit les prédictions
swebench eval SWE-bench/SWE-bench_Lite -p evals/results/preds_full.jsonl \
  --run-id my_run --report-dir evals/results -j 1
```

Mode d'emploi complet, commandes exactes et blocages rencontrés (dont une migration Windows → WSL2 nécessaire pour un bug de fin de ligne CRLF dans le harnais officiel) : [`step_by_step.md`](./step_by_step.md).

## Structure du dépôt

```
src/            sandbox, outils, graphe LangGraph, sélection du dataset, éval
data/           sous-ensemble d'instances sélectionné (versionné, reproductible)
evals/results/  prédictions, rapports de notation, statistiques par instance, transcriptions
step_by_step.md mode d'emploi technique complet (commandes, diagnostics, décisions)
```
