#!/usr/bin/env python3
"""Varredura de segredos em repositórios git — árvore atual E histórico completo.

Substituto simples do gitleaks quando ele não está disponível (no CI, o
gitleaks oficial roda — ver Fase 1.4). NUNCA imprime o valor encontrado:
só repo, commit, arquivo, regra e uma impressão digital (sha256 truncado)
que permite deduplicar e acompanhar a rotação sem expor o segredo.
Para JWTs, decodifica localmente apenas o claim `role` (anon/service_role).

Uso:
    python3 scripts/ops/varrer_segredos.py <repo> [<repo> ...]
Saída: tabela em texto; código 1 se algo além de chaves "anon" foi achado.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REGRAS: list[tuple[str, re.Pattern[str]]] = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("groq-key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("google-oauth-secret", re.compile(r"GOCSPX-[A-Za-z0-9_-]{20,}")),
    ("private-key-pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("gcp-service-account", re.compile(r'"private_key_id"\s*:\s*"[0-9a-f]{20,}"')),
    ("meta-access-token", re.compile(r"\bEAA[A-Za-z0-9]{60,}")),
    ("supabase-secret-key", re.compile(r"sb_secret_[A-Za-z0-9_-]{20,}")),
    ("postgres-url-com-senha", re.compile(r"postgres(?:ql)?://[^:\s/]+:[^@\s]{6,}@[^\s'\"]+")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("expo-token", re.compile(r"\bEXPO_TOKEN\s*[=:]\s*['\"]?[A-Za-z0-9_-]{20,}")),
    (
        "atribuicao-generica",
        re.compile(
            r"(?i)\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|SENHA|PRIVATE_KEY)[A-Z0-9_]*"
            r"\s*[=:]\s*['\"]?(?!\$\{|process\.env|os\.environ|settings\.|<|troque|your|seu|xxx)"
            r"[A-Za-z0-9_\-+/=.]{20,}"
        ),
    ),
]

# Arquivos onde falsos positivos são esperados (hashes de integridade etc.).
IGNORAR_ARQUIVOS = re.compile(
    r"(package-lock\.json|uv\.lock|\.lock$|\.svg$|sbom.*\.json$|trivy.*\.json$)"
)


def _impressao(valor: str) -> str:
    return hashlib.sha256(valor.encode()).hexdigest()[:10]


def _papel_jwt(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        dados = json.loads(base64.urlsafe_b64decode(payload))
        return str(dados.get("role") or dados.get("iss") or "?")
    except Exception:
        return "?"


def _classificar(regra: str, valor: str) -> str:
    return f"jwt:{_papel_jwt(valor)}" if regra == "jwt" else regra


def _varrer_texto(linhas, origem_por_linha):
    """Gera (commit, arquivo, regra, impressao) para cada achado."""
    for commit, arquivo, linha in origem_por_linha(linhas):
        if IGNORAR_ARQUIVOS.search(arquivo):
            continue
        for regra, padrao in REGRAS:
            for m in padrao.finditer(linha):
                yield commit, arquivo, _classificar(regra, m.group(0)), _impressao(m.group(0))


def _historico(repo: Path):
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "--all",
            "-p",
            "--no-color",
            "--no-ext-diff",
            "--format=@@COMMIT@@%h %ad",
            "--date=short",
            "-U0",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )

    def origem(linhas):
        commit, arquivo = "?", "?"
        for linha in linhas:
            if linha.startswith("@@COMMIT@@"):
                commit = linha[len("@@COMMIT@@") :]
            elif linha.startswith("+++ b/"):
                arquivo = linha[6:]
            elif linha.startswith("+") and not linha.startswith("+++"):
                yield commit, arquivo, linha[1:]

    return _varrer_texto(proc.stdout.splitlines(), origem)


def _arvore_nao_ignorada(repo: Path):
    """Arquivos no disco que o git NÃO ignora (rastreados ou não) — o que iria
    num `git add .`. Arquivos ignorados (.env etc.) são reportados à parte."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    )
    for rel in proc.stdout.splitlines():
        caminho = repo / rel
        if not caminho.is_file() or caminho.stat().st_size > 2_000_000:
            continue
        try:
            conteudo = caminho.read_text(errors="replace")
        except Exception:
            continue
        for linha in conteudo.splitlines():
            yield "WORKTREE", rel, linha


def _ignorados_sensiveis(repo: Path) -> list[str]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    padrao = re.compile(
        r"(\.env|secret|adminsdk|service-account|credential|\.pem$|\.p12$|\.key$)", re.I
    )
    return [
        p
        for p in proc.stdout.splitlines()
        if padrao.search(p) and "node_modules" not in p and ".venv" not in p
    ]


def main(repos: list[str]) -> int:
    grave = 0
    for r in repos:
        repo = Path(r).resolve()
        achados: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for commit, arquivo, regra, imp in _historico(repo):
            achados[(regra, imp, arquivo)].append(commit)
        worktree = {
            (regra, imp, arquivo)
            for _, arquivo, regra, imp in _varrer_texto(None, lambda _: _arvore_nao_ignorada(repo))
        }

        print(f"\n=== {repo.name} ===")
        if not achados and not worktree:
            print("  histórico e árvore: nada encontrado")
        for (regra, imp, arquivo), commits in sorted(achados.items()):
            na_arvore = "SIM" if (regra, imp, arquivo) in worktree else "não"
            print(
                f"  [histórico] {regra:<24} fp={imp} arquivo={arquivo} "
                f"commits={len(commits)} (1º: {commits[-1]}) ainda_no_disco={na_arvore}"
            )
            grave += not regra.startswith("jwt:anon")
        for regra, imp, arquivo in sorted(worktree):
            if not any(k[0] == regra and k[1] == imp for k in achados):
                print(f"  [só no disco, não commitado] {regra:<24} fp={imp} arquivo={arquivo}")
                grave += not regra.startswith("jwt:anon")
        ignorados = _ignorados_sensiveis(repo)
        if ignorados:
            print("  arquivos sensíveis no disco, IGNORADOS pelo git (ok, só confira):")
            for p in ignorados:
                print(f"    - {p}")
    print(f"\nAchados que não são chave anon pública: {grave}")
    return 1 if grave else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
