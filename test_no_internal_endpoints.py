"""Guard de segurança do repositório: nenhum endpoint interno pode ser versionado.

O repositório é público. Este teste falha se qualquer arquivo rastreado pelo git
contiver host de cluster, IP privado literal ou o nome do cluster.

Os padrões são montados por concatenação para que este arquivo, ele mesmo, não
contenha os literais proibidos.

Execute com:

    python3 -m unittest test_no_internal_endpoints -v
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Montados por partes: a varredura roda sobre o repositório inteiro, inclusive este arquivo.
CLUSTER_SUFFIX = "svc" + "." + "cluster" + "." + "local"
CLUSTER_NAMES = ("ha" + "des", "aias" + "sist")
REGISTRY_HOSTS = ("gh" + "cr" + ".io",)

PRIVATE_IP = re.compile(
    r"\b(?:10|127)\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b"
    r"|\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b"
    r"|\b172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b"
)

ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini", ".sh"}
ALLOWED_NAMES = {"LICENSE", ".gitignore"}


def tracked_files():
    """Arquivos versionados, segundo o próprio git."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


class NoInternalEndpointsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tracked_files()

    def test_git_sees_files(self):
        self.assertTrue(self.files, "git ls-files não retornou nada")

    def test_no_cluster_endpoints_in_tracked_files(self):
        offenders = []
        for rel in self.files:
            path = REPO_ROOT / rel
            if path.suffix not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            lowered = text.lower()
            if CLUSTER_SUFFIX in lowered or any(name in lowered for name in CLUSTER_NAMES):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"endpoint/host interno versionado em: {offenders}")

    def test_no_private_ip_literals_in_tracked_files(self):
        offenders = []
        for rel in self.files:
            path = REPO_ROOT / rel
            if path.suffix not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if PRIVATE_IP.search(text):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"IP privado literais em: {offenders}")

    def test_no_registry_hosts_in_tracked_files(self):
        offenders = []
        for rel in self.files:
            path = REPO_ROOT / rel
            if path.suffix not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if any(host in text for host in REGISTRY_HOSTS):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"host de registry interno em: {offenders}")

    def test_secret_files_are_not_tracked(self):
        tracked = set(self.files)
        forbidden = {"token" + ".txt", "install_tool.py", ".env"}
        present = sorted(forbidden & tracked)
        self.assertEqual(present, [], f"arquivos sensíveis versionados: {present}")


if __name__ == "__main__":
    unittest.main()