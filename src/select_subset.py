"""Sélectionne un sous-ensemble stratifié de SWE-bench Lite (par dépôt, seed fixe)
pour l'évaluation de l'agent. Reproductible : `python src/select_subset.py`.
"""

import json
import random
from collections import defaultdict
from pathlib import Path

from datasets import load_dataset

TARGET_SIZE = 24
SEED = 42
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def patch_stats(patch: str) -> tuple[int, int, int]:
    files = set()
    added = removed = 0
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            files.add(line.split()[2])
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return len(files), added, removed


def main() -> None:
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    rows = []
    for ex in ds:
        n_files, added, removed = patch_stats(ex["patch"])
        fail_to_pass = ex["FAIL_TO_PASS"]
        n_fail_to_pass = len(eval(fail_to_pass)) if isinstance(fail_to_pass, str) else len(fail_to_pass)
        rows.append(
            {
                "instance_id": ex["instance_id"],
                "repo": ex["repo"],
                "files_changed": n_files,
                "lines_added": added,
                "lines_removed": removed,
                "n_fail_to_pass": n_fail_to_pass,
            }
        )

    by_repo = defaultdict(list)
    for r in rows:
        by_repo[r["repo"]].append(r)

    total = sum(len(v) for v in by_repo.values())
    random.seed(SEED)
    selected_ids = []
    alloc = {}
    for repo, items in by_repo.items():
        n = max(1, round(len(items) / total * TARGET_SIZE))
        alloc[repo] = n
        sample = random.sample(items, min(n, len(items)))
        selected_ids.extend(r["instance_id"] for r in sample)

    print("allocation par dépôt:", alloc, "total =", sum(alloc.values()))

    by_id = {ex["instance_id"]: ex for ex in ds}
    stats_by_id = {r["instance_id"]: r for r in rows}

    subset = []
    flags = []
    for iid in sorted(selected_ids):
        ex = by_id[iid]
        s = stats_by_id[iid]
        ps = (ex["problem_statement"] or "").strip()
        lines_changed = s["lines_added"] + s["lines_removed"]
        issues = []
        if len(ps) < 80:
            issues.append("problem_statement très court")
        if s["n_fail_to_pass"] == 0:
            issues.append("FAIL_TO_PASS vide")
        if lines_changed > 60:
            issues.append(f"patch volumineux ({lines_changed} lignes)")
        if issues:
            flags.append((iid, issues))
        subset.append(
            {
                "instance_id": iid,
                "repo": s["repo"],
                "lines_changed": lines_changed,
                "n_fail_to_pass": s["n_fail_to_pass"],
                "problem_statement_preview": ps[:150].replace("\n", " "),
            }
        )

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "swebench_lite_subset.json").write_text(
        json.dumps(subset, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{len(subset)} instances sélectionnées -> data/swebench_lite_subset.json")
    if flags:
        print("\nInstances avec un signal à vérifier manuellement :")
        for iid, issues in flags:
            print(" -", iid, issues)
    else:
        print("Aucun cas dégénéré détecté (problem_statement vide, FAIL_TO_PASS vide, patch anormalement gros).")


if __name__ == "__main__":
    main()
