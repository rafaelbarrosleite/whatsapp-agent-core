"""Testes do scripts/ops/varrer_segredos.py em um repo git temporário.

Garante que o scanner encontra segredos no HISTÓRICO (mesmo já removidos da
árvore) e que nunca imprime o valor — só regra, arquivo e impressão digital.
"""

import importlib.util
import pathlib
import subprocess
import sys

_CAMINHO = pathlib.Path(__file__).parent.parent / "scripts" / "ops" / "varrer_segredos.py"
_spec = importlib.util.spec_from_file_location("varrer_segredos", _CAMINHO)
vs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vs
_spec.loader.exec_module(vs)

# Valores FALSOS, montados em partes para este arquivo não disparar scanners.
CHAVE_FALSA = "sk-ant-" + "api03-" + "A" * 12 + "b" * 12 + "C" * 12
PEM_FALSO = "-----BEGIN " + "PRIVATE KEY-----"


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def _repo_com_segredo_removido(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "config.py").write_text(f'ANTHROPIC = "{CHAVE_FALSA}"\n')
    (repo / "k.txt").write_text(PEM_FALSO + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "oops")
    (repo / "config.py").write_text('ANTHROPIC = os.environ["ANTHROPIC_API_KEY"]\n')
    (repo / "k.txt").unlink()
    _git(repo, "commit", "-qam", "remove")
    return repo


def test_encontra_no_historico_sem_imprimir_valor(tmp_path, capsys) -> None:
    repo = _repo_com_segredo_removido(tmp_path)

    codigo = vs.main([str(repo)])
    saida = capsys.readouterr().out

    assert codigo == 1
    assert "anthropic-key" in saida and "config.py" in saida
    assert "private-key-pem" in saida
    assert "ainda_no_disco=não" in saida  # removido da árvore, mas vive no histórico
    assert CHAVE_FALSA not in saida
    assert "AAAAAAAAAAAA" not in saida


def test_jwt_anon_nao_conta_como_grave(tmp_path, capsys) -> None:
    import base64
    import json

    def b64(o: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    cabecalho = b64({"alg": "HS256", "typ": "JWT"})
    anon = f"{cabecalho}.{b64({'role': 'anon', 'x': 'y' * 10})}.{'s' * 20}"
    repo = tmp_path / "r2"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pub.ts").write_text(f'const k = "{anon}"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "anon")

    codigo = vs.main([str(repo)])
    saida = capsys.readouterr().out

    assert "jwt:anon" in saida
    assert codigo == 0
    assert anon not in saida
