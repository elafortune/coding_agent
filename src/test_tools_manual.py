"""Vérification manuelle de chaque outil, hors graphe, sur une vraie instance
SWE-bench avant intégration dans LangGraph (Étape 3 du cadrage).
"""

from datasets import load_dataset

from sandbox import Sandbox
from tools import apply_patch, get_diff, read_file, run_tests, search_code

INSTANCE_ID = "astropy__astropy-7746"


def main() -> None:
    ds = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
    instance = next(ex for ex in ds if ex["instance_id"] == INSTANCE_ID)

    print(f"=== Instance {INSTANCE_ID} ===")
    print("Démarrage du conteneur...")
    with Sandbox(instance) as sb:
        print("\n--- search_code: 'all_pix2world' ---")
        print(search_code(sb, "all_pix2world", path="astropy/wcs"))

        print("\n--- read_file: astropy/wcs/tests/test_wcs.py (lignes 1090-1096) ---")
        print(read_file(sb, "astropy/wcs/tests/test_wcs.py", start=1090, end=1096))

        print("\n--- apply_patch (test_patch -- ajoute les tests FAIL_TO_PASS, fait partie du harnais, jamais montré à l'agent) ---")
        ok, msg = apply_patch(sb, instance["test_patch"])
        print("succès:", ok)
        print(msg)

        print("\n--- run_tests AVANT patch de code (doit échouer, comportement attendu) ---")
        fail_to_pass = eval(instance["FAIL_TO_PASS"]) if isinstance(instance["FAIL_TO_PASS"], str) else instance["FAIL_TO_PASS"]
        print("Tests ciblés :", fail_to_pass)
        exit_code, output = run_tests(sb, fail_to_pass)
        print("exit_code:", exit_code)
        print(output[-1500:])

        print("\n--- apply_patch (patch de référence) ---")
        ok, msg = apply_patch(sb, instance["patch"])
        print("succès:", ok)
        print(msg)

        print("\n--- get_diff (doit refléter le patch appliqué) ---")
        print(get_diff(sb)[:500])

        print("\n--- run_tests APRÈS patch (doit réussir) ---")
        exit_code, output = run_tests(sb, fail_to_pass)
        print("exit_code:", exit_code)
        print(output[-1500:])


if __name__ == "__main__":
    main()
