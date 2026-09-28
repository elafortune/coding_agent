"""Encapsule un conteneur Docker par instance SWE-bench.

Réutilise directement les images pré-construites du harnais officiel `swebench`
(`instance["image"]`) : dépôt déjà cloné au bon commit, dépendances déjà
installées dans un environnement conda nommé `testbed`, prêt à l'emploi.
Pas de volume monté depuis l'hôte -- l'agent ne touche jamais le système de
fichiers réel, uniquement celui, jetable, du conteneur.
"""

import io
import tarfile
import uuid

import docker

CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate && conda activate testbed"
WORKDIR = "/testbed"


class Sandbox:
    def __init__(self, instance: dict):
        self.instance = instance
        self.image = instance["image"]
        self.instance_id = instance["instance_id"]
        self.client = docker.from_env()
        self.container = None

    def start(self) -> "Sandbox":
        name = f"swe-agent-{self.instance_id}-{uuid.uuid4().hex[:8]}"
        self.container = self.client.containers.run(
            self.image,
            name=name,
            command="sleep infinity",
            detach=True,
        )
        return self

    def stop(self) -> None:
        if self.container is not None:
            self.container.remove(force=True)
            self.container = None

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def exec(self, cmd: str, workdir: str = WORKDIR) -> tuple[int, str]:
        """Exécute `cmd` dans le conteneur, environnement conda `testbed` activé."""
        full_cmd = f"{CONDA_ACTIVATE} && cd {workdir} && {cmd}"
        result = self.container.exec_run(["bash", "-lc", full_cmd])
        output = result.output.decode("utf-8", errors="replace") if result.output else ""
        return result.exit_code, output

    def write_file(self, container_path: str, content: str) -> None:
        """Écrit un fichier dans le conteneur (équivalent `docker cp`, via une
        archive tar en mémoire -- l'API Docker n'expose pas d'écriture directe)."""
        data = content.encode("utf-8")
        directory, _, filename = container_path.rpartition("/")
        directory = directory or "/"

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_stream.seek(0)

        self.container.put_archive(directory, tar_stream)
