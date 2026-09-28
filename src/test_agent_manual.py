"""Test du graphe LangGraph sur 1-2 instances, avant de lancer l'éval complète
(Étape 4 du cadrage) -- affiche la transcription complète pour vérifier le
câblage (appels d'outils, réponses, condition de sortie).
"""

import json
import sys

from datasets import load_dataset

from agent import run_agent

INSTANCE_ID = sys.argv[1] if len(sys.argv) > 1 else "astropy__astropy-7746"
MAX_ITERATIONS = int(sys.argv[2]) if len(sys.argv) > 2 else 10


def main() -> None:
    ds = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
    instance = next(ex for ex in ds if ex["instance_id"] == INSTANCE_ID)

    print(f"=== Instance {INSTANCE_ID} (budget {MAX_ITERATIONS} tours) ===\n")
    result = run_agent(instance, max_iterations=MAX_ITERATIONS)

    print("--- Résultat ---")
    print("iterations:", result["iterations"])
    print("n_messages:", result["n_messages"])
    print("\n--- Patch final produit ---")
    print(result["model_patch"] or "(patch vide)")

    with open(f"data/agent_run_{INSTANCE_ID}.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nSauvegardé dans data/agent_run_{INSTANCE_ID}.json")


if __name__ == "__main__":
    main()
