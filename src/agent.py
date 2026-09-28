"""Graphe LangGraph pilotant l'agent de résolution de bugs.

Architecture : une boucle "agent (LLM + outils) <-> exécution d'outil" avec
conditions de sortie explicites (appel de `submit`, ou budget d'itérations
épuisé) -- pas une boucle ReAct libre : le graphe impose le contrôle de flux
(LangGraph gère les transitions), le jeu d'outils est fermé et défini à
l'avance, et l'agent n'a jamais accès aux tests cachés (FAIL_TO_PASS/
test_patch) qui serviront à le noter -- exactement les conditions d'une
vraie revue de code.
"""

import os
from typing import TypedDict

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from langgraph.graph import END, StateGraph
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

import tools as tool_impl
from sandbox import Sandbox

RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, errors.APIError) and getattr(exc, "code", None) in RETRYABLE_HTTP_CODES

MODEL_NAME = "gemini-2.5-flash"

SYSTEM_PROMPT = """Tu es un agent de résolution de bugs logiciels. On te donne un rapport de bug \
sur le dépôt {repo}. Ton dépôt de travail est dans un conteneur, à /testbed, à l'état du commit \
juste avant le vrai correctif -- ton travail est de comprendre et corriger le bug.

Outils disponibles :
- search_code(pattern, path=".") : recherche une regex étendue dans les fichiers .py du dépôt
- read_file(path, start=None, end=None) : lit un fichier, avec numéros de ligne. Sans plage, \
tronqué au-delà de 200 lignes -- précise start/end pour lire la suite d'un gros fichier plutôt \
que de le redemander en entier
- apply_patch(diff_text) : applique un diff unifié (format `git diff`) au dépôt ; renvoie \
l'erreur git si le patch ne s'applique pas, pour que tu puisses corriger et réessayer
- run_command(cmd) : exécute une commande bash arbitraire (ex: pytest sur un fichier de test \
existant du dépôt)
- submit() : termine ta tentative -- à appeler uniquement quand tu es confiant dans ton correctif

Règles :
- Explore le code avant d'éditer, ne devine pas à l'aveugle.
- Après chaque apply_patch réussi, une vérification automatique et déterministe s'exécute toute \
seule (pas besoin de la demander) : elle relance les tests déjà existants du dépôt qui doivent \
rester valides, et t'indique si ton changement en a cassé un. Si c'est le cas, submit() sera \
bloqué une première fois pour te laisser l'occasion de corriger -- tu peux forcer en rappelant \
submit() une seconde fois si tu juges que c'est acceptable.
- Tu ne connais PAS les tests exacts qui valideront ta correction (ils sont cachés, comme dans \
une vraie revue de code) -- la vérification automatique porte sur des tests déjà existants, pas \
sur le test qui prouvera ta correction.
- Budget limité : {max_iterations} tours maximum. Un point de contrôle s'affichera automatiquement \
aux 70% du budget si tu n'as pas encore soumis. Appelle submit() dès que tu es confiant, plutôt \
que d'itérer sans but.
- Réponds toujours en appelant un outil -- jamais de texte libre.

Rapport de bug :
{problem_statement}
"""

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_code",
        description="Recherche une regex étendue (grep -E) dans les fichiers .py du dépôt.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex étendue à chercher"},
                "path": {"type": "string", "description": "Répertoire ou fichier où chercher (défaut : tout le dépôt)"},
            },
            "required": ["pattern"],
        },
    ),
    types.FunctionDeclaration(
        name="read_file",
        description="Lit un fichier du dépôt, avec numéros de ligne.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Chemin du fichier, relatif à la racine du dépôt"},
                "start": {"type": "integer", "description": "Première ligne à lire (optionnel)"},
                "end": {"type": "integer", "description": "Dernière ligne à lire (optionnel)"},
            },
            "required": ["path"],
        },
    ),
    types.FunctionDeclaration(
        name="apply_patch",
        description="Applique un diff unifié (format `git diff`) au dépôt.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "diff_text": {"type": "string", "description": "Contenu du diff, format unifié git"},
            },
            "required": ["diff_text"],
        },
    ),
    types.FunctionDeclaration(
        name="run_command",
        description="Exécute une commande bash arbitraire dans le dépôt (ex: pytest sur un fichier de test).",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Commande shell à exécuter"},
            },
            "required": ["cmd"],
        },
    ),
    types.FunctionDeclaration(
        name="submit",
        description="Termine la tentative -- à appeler uniquement quand le correctif est prêt.",
        parameters_json_schema={"type": "object", "properties": {}},
    ),
]

NUDGE_TEXT = (
    "Réponds en appelant un outil (search_code, read_file, apply_patch, run_command) "
    "ou submit() si tu as terminé -- pas de texte libre."
)

CHECKPOINT_FRACTION = 0.7
MAX_REGRESSION_CHECK = 30  # borne le nombre de tests PASS_TO_PASS rejoués par `verify` (temps, pas tokens)


class AgentState(TypedDict):
    contents: list
    iterations: int
    max_iterations: int
    total_tokens: int
    just_patched: bool  # un apply_patch a réussi ce tour-ci -- consommé immédiatement par le routage
    verified: bool  # le dernier patch appliqué a passé la vérification de non-régression sans échec
    submit_warned: bool  # l'agent a déjà été averti une fois d'une soumission non vérifiée
    checkpoint_fired: bool  # le point de contrôle budget (70%) s'est déjà déclenché une fois
    baseline_status: dict  # statut PASS_TO_PASS avant tout patch (test_id -> "PASSED"/"FAILED"/"ERROR")


def _parse_pytest_summary(output: str) -> dict:
    """Parse la section \"short test summary info\" de `pytest -rA` en {test_id: statut}.
    Un test absent du résultat (erreur de collecte globale, id introuvable) est simplement
    absent du dict -- ni PASSED ni FAILED, on ne peut rien affirmer dessus."""
    statuses = {}
    for line in output.splitlines():
        line = line.strip()
        for status in ("PASSED", "FAILED", "ERROR"):
            if line.startswith(status + " "):
                rest = line[len(status) + 1 :]
                test_id = rest.split(" - ", 1)[0].strip()
                statuses[test_id] = status
                break
    return statuses


def _call_tool(sandbox: Sandbox, name: str, args: dict) -> tuple[str, bool]:
    """Renvoie (texte pour l'agent, patch_applied) -- patch_applied déclenche `verify`."""
    args = args or {}
    if name == "search_code":
        return tool_impl.search_code(sandbox, **args), False
    if name == "read_file":
        return tool_impl.read_file(sandbox, **args), False
    if name == "apply_patch":
        ok, msg = tool_impl.apply_patch(sandbox, **args)
        return f"succès: {ok}\n{msg}", ok
    if name == "run_command":
        code, output = tool_impl.run_command(sandbox, **args)
        return f"exit_code: {code}\n{output}", False
    return f"ERREUR: outil inconnu '{name}'", False


def make_agent_node(client: genai.Client, model_name: str, tool: types.Tool, system_prompt: str):
    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _generate(contents):
        return client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[tool],
            ),
        )

    def agent_node(state: AgentState) -> dict:
        response = _generate(state["contents"])
        candidate = response.candidates[0] if response.candidates else None
        candidate_content = candidate.content if candidate else None
        if candidate_content is None or not candidate_content.parts:
            # Réponse vide/bloquée (ex: finish_reason SAFETY ou MAX_TOKENS sans contenu) --
            # on ne plante pas, on le traite comme "pas d'appel d'outil" pour relancer via nudge.
            reason = candidate.finish_reason if candidate else "no_candidate"
            candidate_content = types.Content(
                role="model", parts=[types.Part(text=f"(réponse vide, finish_reason={reason})")]
            )
        tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
        return {
            "contents": state["contents"] + [candidate_content],
            "iterations": state["iterations"] + 1,
            "total_tokens": state["total_tokens"] + (tokens or 0),
        }

    return agent_node


def make_tool_node(sandbox: Sandbox):
    def tool_node(state: AgentState) -> dict:
        last = state["contents"][-1]
        response_parts = []
        patch_applied = False
        for part in last.parts:
            if not part.function_call:
                continue
            fc = part.function_call
            try:
                result_text, applied = _call_tool(sandbox, fc.name, fc.args)
                patch_applied = patch_applied or applied
            except Exception as e:  # noqa: BLE001 -- l'agent doit voir l'erreur, pas planter
                result_text = f"ERREUR outil: {e}"
            response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=fc.id, name=fc.name, response={"result": result_text}
                    )
                )
            )
        new_content = types.Content(role="user", parts=response_parts)
        update = {"contents": state["contents"] + [new_content], "just_patched": patch_applied}
        if patch_applied:
            # nouveau patch -- l'avertissement précédent ne concernait pas celui-ci
            update["verified"] = False
            update["submit_warned"] = False
        return update

    return tool_node


def make_baseline_node(sandbox: Sandbox, pass_to_pass: list[str]):
    """Nœud d'entrée, exécuté une seule fois avant tout patch : capture quels tests
    PASS_TO_PASS passent réellement *avant* toute modification. Nécessaire car certains
    ids paramétrés du dataset sont eux-mêmes mal formés (ex. observé :
    `test_artist_inspector_get_valid_values[ACCEPTS:'`, guillemet non échappé) et ne se
    collectent pas correctement même sans aucun patch -- sans cette référence, `verify`
    accuserait le patch de l'agent d'une casse préexistante et non liée à son travail."""
    checked = pass_to_pass[:MAX_REGRESSION_CHECK]

    def baseline_node(state: AgentState) -> dict:
        if not checked:
            return {"baseline_status": {}}
        _, output = tool_impl.run_tests(sandbox, checked)
        return {"baseline_status": _parse_pytest_summary(output)}

    return baseline_node


def make_verify_node(sandbox: Sandbox, pass_to_pass: list[str]):
    """Nœud déterministe (aucun appel LLM) : après un `apply_patch` réussi, rejoue les
    mêmes tests PASS_TO_PASS et compare au statut de référence (`baseline_status`) --
    seul un test passé à la référence et cassé maintenant compte comme régression. Un
    test déjà en échec/non collectable à la référence est ignoré, quel que soit son
    statut après coup : ce n'est pas quelque chose que l'agent a pu causer. Ne révèle
    jamais FAIL_TO_PASS/test_patch, seulement des tests déjà présents dans le dépôt
    (l'équivalent de "ne pas casser la CI existante", pas de fuite du critère caché)."""
    checked = pass_to_pass[:MAX_REGRESSION_CHECK]

    def verify_node(state: AgentState) -> dict:
        if not checked:
            msg = "Vérification automatique : aucun test de non-régression connu pour ce dépôt."
            verified = True
        else:
            baseline = state.get("baseline_status", {})
            _, output = tool_impl.run_tests(sandbox, checked)
            current = _parse_pytest_summary(output)
            regressions = [
                test_id
                for test_id, before in baseline.items()
                if before == "PASSED" and current.get(test_id) in ("FAILED", "ERROR")
            ]
            if not regressions:
                msg = (
                    f"Vérification automatique (non-régression, {len(checked)} tests pré-existants, "
                    "comparé à l'état avant patch) : aucune régression détectée."
                )
                verified = True
            else:
                preview = "\n".join(regressions[:10])
                msg = (
                    f"Vérification automatique (non-régression, {len(checked)} tests pré-existants, "
                    f"comparé à l'état avant patch) : ÉCHEC -- ces tests passaient avant ton patch "
                    f"et échouent maintenant :\n{preview}"
                )
                verified = False
        return {
            "contents": state["contents"] + [types.Content(role="user", parts=[types.Part(text=msg)])],
            "verified": verified,
            "just_patched": False,
        }

    return verify_node


def submit_blocked_node(state: AgentState) -> dict:
    msg = (
        "submit() bloqué une première fois : ton dernier correctif a fait échouer des tests "
        "de non-régression (voir le rapport ci-dessus). Corrige-le, ou rappelle submit() "
        "pour confirmer que tu soumets quand même en connaissance de cause."
    )
    return {
        "contents": state["contents"] + [types.Content(role="user", parts=[types.Part(text=msg)])],
        "submit_warned": True,
    }


def checkpoint_node(state: AgentState) -> dict:
    remaining = state["max_iterations"] - state["iterations"]
    msg = (
        f"Point de contrôle budget : il te reste {remaining} tour(s) sur {state['max_iterations']}. "
        "Si tu n'as pas encore de correctif appliqué, ou si tu répètes la même approche sans "
        "progrès depuis plusieurs tours, envisage une stratégie différente plutôt que d'épuiser "
        "le budget sans résultat."
    )
    return {
        "contents": state["contents"] + [types.Content(role="user", parts=[types.Part(text=msg)])],
        "checkpoint_fired": True,
    }


def nudge_node(state: AgentState) -> dict:
    nudge = types.Content(role="user", parts=[types.Part(text=NUDGE_TEXT)])
    return {"contents": state["contents"] + [nudge]}


def route_after_tools(state: AgentState) -> str:
    return "verify" if state.get("just_patched") else "agent"


def route_after_agent(state: AgentState) -> str:
    last = state["contents"][-1]
    fn_calls = [p.function_call for p in last.parts if p.function_call]
    budget_exhausted = state["iterations"] >= state["max_iterations"]
    if any(fc.name == "submit" for fc in fn_calls):
        # Budget épuisé : on termine avec ce qui existe, jamais de tour de plus pour
        # une confirmation -- sinon `submit_blocked` peut faire dépasser le budget d'un tour.
        if budget_exhausted or state["verified"] or state["submit_warned"]:
            return "finish"
        return "submit_blocked"
    if budget_exhausted:
        return "finish"
    if not state["checkpoint_fired"] and state["iterations"] >= CHECKPOINT_FRACTION * state["max_iterations"]:
        return "checkpoint"
    if not fn_calls:
        return "nudge"
    return "tools"


def build_graph(
    client: genai.Client,
    model_name: str,
    sandbox: Sandbox,
    system_prompt: str,
    pass_to_pass: list[str],
):
    tool = types.Tool(function_declarations=TOOL_DECLARATIONS)
    agent_node = make_agent_node(client, model_name, tool, system_prompt)
    tool_node = make_tool_node(sandbox)
    baseline_node = make_baseline_node(sandbox, pass_to_pass)
    verify_node = make_verify_node(sandbox, pass_to_pass)

    graph = StateGraph(AgentState)
    graph.add_node("baseline", baseline_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("verify", verify_node)
    graph.add_node("submit_blocked", submit_blocked_node)
    graph.add_node("checkpoint", checkpoint_node)
    graph.add_node("nudge", nudge_node)
    graph.set_entry_point("baseline")
    graph.add_edge("baseline", "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "nudge": "nudge",
            "finish": END,
            "submit_blocked": "submit_blocked",
            "checkpoint": "checkpoint",
        },
    )
    graph.add_conditional_edges("tools", route_after_tools, {"verify": "verify", "agent": "agent"})
    graph.add_edge("verify", "agent")
    graph.add_edge("submit_blocked", "agent")
    graph.add_edge("checkpoint", "agent")
    graph.add_edge("nudge", "agent")
    return graph.compile()


def run_agent(
    instance: dict,
    max_iterations: int = 15,
    model_name: str = MODEL_NAME,
    transcript_path: str | None = None,
) -> dict:
    load_dotenv()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    system_prompt = SYSTEM_PROMPT.format(
        repo=instance["repo"],
        problem_statement=instance["problem_statement"],
        max_iterations=max_iterations,
    )
    pass_to_pass = instance["PASS_TO_PASS"]
    if isinstance(pass_to_pass, str):
        pass_to_pass = eval(pass_to_pass)

    with Sandbox(instance) as sandbox:
        graph = build_graph(client, model_name, sandbox, system_prompt, pass_to_pass)
        init_state: AgentState = {
            "contents": [types.Content(role="user", parts=[types.Part(text="Commence.")])],
            "iterations": 0,
            "max_iterations": max_iterations,
            "total_tokens": 0,
            "just_patched": False,
            "verified": True,
            "submit_warned": False,
            "checkpoint_fired": False,
            "baseline_status": {},
        }
        # Plus de nœuds par tour qu'avant (agent -> tools -> verify -> agent, + checkpoint/submit_blocked
        # possibles) -- marge généreuse plutôt que reprendre le calcul exact à chaque changement de graphe.
        final_state = graph.invoke(init_state, config={"recursion_limit": max_iterations * 6 + 20})
        final_patch = tool_impl.get_diff(sandbox)

    result = {
        "instance_id": instance["instance_id"],
        "model_patch": final_patch,
        "iterations": final_state["iterations"],
        "n_messages": len(final_state["contents"]),
        "total_tokens": final_state["total_tokens"],
    }
    if transcript_path:
        _dump_transcript(final_state["contents"], transcript_path)
    return result


def _dump_transcript(contents: list, path: str) -> None:
    """Sérialise la conversation en JSON lisible (texte, appels d'outils, réponses)
    pour analyse post-hoc (Étape 6) -- pas conservé par défaut, coûteux en espace."""
    import json

    turns = []
    for content in contents:
        parts_out = []
        for part in content.parts:
            if part.text:
                parts_out.append({"text": part.text})
            elif part.function_call:
                parts_out.append({"function_call": {"name": part.function_call.name, "args": part.function_call.args}})
            elif part.function_response:
                parts_out.append({"function_response": {"name": part.function_response.name, "response": part.function_response.response}})
        turns.append({"role": content.role, "parts": parts_out})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(turns, f, indent=2, ensure_ascii=False)
