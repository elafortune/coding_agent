"""Étape 5 -- fait tourner l'agent sur les 27 instances sélectionnées, écrit les
prédictions au format attendu par le harnais officiel `swebench eval`.

Robuste aux erreurs par instance (une instance qui plante n'arrête pas les
autres) et trace itérations/tokens/temps par instance pour le rapport final.
"""

import argparse
import json
import time
from pathlib import Path

from datasets import load_dataset

from agent import run_agent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"
MODEL_LABEL = "gemini-2.5-flash-agent"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iterations", type=int, default=15)
    parser.add_argument("--limit", type=int, default=None, help="limiter le nombre d'instances (debug)")
    args = parser.parse_args()

    subset = json.loads((DATA_DIR / "swebench_lite_subset.json").read_text(encoding="utf-8"))
    instance_ids = [r["instance_id"] for r in subset]
    if args.limit:
        instance_ids = instance_ids[: args.limit]

    print(f"Chargement de SWE-bench Lite pour {len(instance_ids)} instances...")
    ds = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
    by_id = {ex["instance_id"]: ex for ex in ds}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    preds_path = RESULTS_DIR / "preds_full.jsonl"
    runs_path = RESULTS_DIR / "agent_runs.jsonl"

    with open(preds_path, "w", encoding="utf-8") as preds_f, open(runs_path, "w", encoding="utf-8") as runs_f:
        for i, iid in enumerate(instance_ids, 1):
            print(f"[{i}/{len(instance_ids)}] {iid} ...", flush=True)
            instance = by_id[iid]
            t0 = time.time()
            error = None
            try:
                result = run_agent(instance, max_iterations=args.max_iterations)
            except Exception as e:  # noqa: BLE001 -- une instance en échec ne doit pas arrêter les autres
                result = {
                    "instance_id": iid,
                    "model_patch": "",
                    "iterations": 0,
                    "n_messages": 0,
                    "total_tokens": 0,
                }
                error = f"{type(e).__name__}: {e}"

            elapsed = time.time() - t0
            result["elapsed_seconds"] = round(elapsed, 1)
            result["error"] = error
            runs_f.write(json.dumps(result, ensure_ascii=False) + "\n")
            runs_f.flush()

            pred = {
                "instance_id": iid,
                "model_patch": result["model_patch"],
                "model_name_or_path": MODEL_LABEL,
            }
            preds_f.write(json.dumps(pred, ensure_ascii=False) + "\n")
            preds_f.flush()

            status = "OK" if not error else f"ERREUR: {error}"
            print(
                f"  -> {result['iterations']} itérations, {result['total_tokens']} tokens, "
                f"{elapsed:.0f}s, {status}",
                flush=True,
            )

    print(f"\nPrédictions écrites dans {preds_path}")
    print(f"Détails par run dans {runs_path}")
    print(
        "\nPour noter :\n"
        f"  swebench eval SWE-bench/SWE-bench_Lite -p {preds_path} "
        f"--run-id full_eval --report-dir {RESULTS_DIR} -j 4"
    )


if __name__ == "__main__":
    main()
