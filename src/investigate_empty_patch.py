"""Étape 6 -- rejoue une instance à patch vide avec capture de la transcription
complète, pour diagnostiquer pourquoi l'agent n'a jamais réussi à patcher.
"""

import sys

from datasets import load_dataset

from agent import run_agent

INSTANCE_ID = sys.argv[1] if len(sys.argv) > 1 else "pylint-dev__pylint-6506"


def main() -> None:
    ds = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
    instance = next(ex for ex in ds if ex["instance_id"] == INSTANCE_ID)

    transcript_path = f"evals/results/transcript_{INSTANCE_ID}.json"
    result = run_agent(instance, max_iterations=15, transcript_path=transcript_path)

    print("iterations:", result["iterations"])
    print("total_tokens:", result["total_tokens"])
    print("model_patch vide:", not result["model_patch"].strip())
    print("transcript ->", transcript_path)


if __name__ == "__main__":
    main()
