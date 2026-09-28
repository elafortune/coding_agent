"""Outils exposés à l'agent (façon MCP allégé) : recherche de code, lecture de
fichier, application de patch, exécution de commande -- tous délégués au
`Sandbox` (conteneur Docker isolé), jamais exécutés sur l'hôte.
"""

import shlex

from sandbox import Sandbox


def search_code(sandbox: Sandbox, pattern: str, path: str = ".", max_results: int = 50) -> str:
    """Recherche `pattern` (regex étendue) dans les fichiers .py sous `path`."""
    cmd = (
        f"grep -rn --include='*.py' -E {shlex.quote(pattern)} {shlex.quote(path)} "
        f"| head -n {max_results}"
    )
    _, output = sandbox.exec(cmd)
    return output or "(aucun résultat)"


MAX_LINES_DEFAULT = 200


def read_file(sandbox: Sandbox, path: str, start: int | None = None, end: int | None = None) -> str:
    """Lit `path`, avec numéros de ligne. `start`/`end` pour se limiter à une plage.

    Sans plage explicite, tronque au-delà de `MAX_LINES_DEFAULT` lignes plutôt que
    de renvoyer le fichier entier -- un seul fichier volumineux dumpé intégralement
    (observé : 60 937 caractères en un appel) gonfle l'historique complet de la
    conversation à chaque tour suivant (Étape 6, cause principale des runs à
    500K+ tokens).
    """
    quoted = shlex.quote(path)

    if start is not None and end is not None:
        cmd = f"sed -n '{start},{end}p' {quoted} | nl -ba -v{start}"
        exit_code, output = sandbox.exec(cmd)
        if exit_code != 0:
            return f"ERREUR (code {exit_code}) : {output}"
        return output

    count_code, count_output = sandbox.exec(f"wc -l < {quoted}")
    if count_code != 0:
        return f"ERREUR (code {count_code}) : {count_output}"
    try:
        total_lines = int(count_output.strip())
    except ValueError:
        total_lines = None

    if total_lines is not None and total_lines > MAX_LINES_DEFAULT:
        exit_code, output = sandbox.exec(f"head -n {MAX_LINES_DEFAULT} {quoted} | cat -n")
        if exit_code != 0:
            return f"ERREUR (code {exit_code}) : {output}"
        return (
            f"{output}\n"
            f"[TRONQUÉ -- fichier de {total_lines} lignes au total, seules les {MAX_LINES_DEFAULT} "
            f"premières sont affichées. Précise start/end pour lire une autre plage.]"
        )

    exit_code, output = sandbox.exec(f"cat -n {quoted}")
    if exit_code != 0:
        return f"ERREUR (code {exit_code}) : {output}"
    return output


def apply_patch(sandbox: Sandbox, diff_text: str) -> tuple[bool, str]:
    """Applique un diff unifié (format `git diff`) dans le dépôt du conteneur."""
    sandbox.write_file("/tmp/agent.patch", diff_text)
    check_code, check_output = sandbox.exec("git apply --check -v /tmp/agent.patch")
    if check_code != 0:
        return False, f"Patch invalide, non appliqué :\n{check_output}"
    exit_code, output = sandbox.exec("git apply -v /tmp/agent.patch")
    return exit_code == 0, output


def run_tests(sandbox: Sandbox, test_ids: list[str]) -> tuple[int, str]:
    """Lance pytest sur une liste d'identifiants de test (node ids)."""
    test_args = " ".join(shlex.quote(t) for t in test_ids)
    return sandbox.exec(f"pytest -rA --tb=short {test_args}")


def run_command(sandbox: Sandbox, cmd: str) -> tuple[int, str]:
    """Exécute une commande arbitraire dans le conteneur (bash/pytest/etc.)."""
    return sandbox.exec(cmd)


def get_diff(sandbox: Sandbox) -> str:
    """Récupère le diff courant du dépôt -- c'est le patch final soumis à l'évaluation."""
    _, output = sandbox.exec("git -c core.fileMode=false diff")
    return output
