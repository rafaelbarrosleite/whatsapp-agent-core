"""security/anonimizacao.py — remove dado pessoal de texto livre.

Portado do Tech Challenge FIAP Fase 3 (data/anonymize.py). Determinístico e
auditável: cada ocorrência vira um marcador ([CPF], [NOME]...) e a função
devolve a contagem por tipo.

Dois usos, com rigor diferente:

  - anonimizar(): texto que SAI para terceiro ou vira dataset — traces do
    Langfuse, conversas exportadas para a bateria de avaliação. Pega tudo que
    conseguir, inclusive datas e nomes rotulados ("Paciente: Maria Souza").
  - mascarar_para_log(): rede de proteção dos logs. Só padrões sem ambiguidade
    (e-mail e CPF/CNS formatados). Datas e números longos ficam: o log precisa
    de data de agendamento e de ids da Meta (15–16 dígitos) para depuração.
    Telefone já é tratado em core/observability.mascarar_telefones.

Diferença para o original da FIAP: sem o dicionário de prenomes do Faker.
Ele pegava nome solto em nota clínica, mas exigiria o Faker em produção e dá
falso positivo em conversa de WhatsApp ("Bom dia Maria Clara"). Nome só é
removido quando vem rotulado ou com tratamento (Dr., Sra.).
"""

import re
from collections import Counter
from dataclasses import dataclass, field

_PALAVRA_NOME = r"[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]+"
_SEP = r"[ \t]+"
_TRATAMENTO = r"(?:Sra?|Srta|Dra?)\.[ \t]+"
_SEQ_NOME = rf"{_PALAVRA_NOME}(?:{_SEP}(?:d[aeo]s?{_SEP}|e{_SEP})?{_PALAVRA_NOME})*"

# Ordem importa: padrões mais específicos primeiro.
_PADROES: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # E.164 como o WhatsApp entrega (+5567999998888): o padrão de telefone
    # abaixo exige fronteira de palavra depois do +55 e não pega este formato.
    ("TELEFONE", re.compile(r"(?<!\w)\+?55\d{10,11}\b")),
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("CNS", re.compile(r"\b[1-2789]\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\b")),
    ("RG", re.compile(r"\b\d{1,2}\.\d{3}\.\d{3}-[\dXx]\b")),
    ("TELEFONE", re.compile(r"(?:\+55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b")),
    ("CEP", re.compile(r"\b\d{5}-\d{3}\b")),
    ("DATA", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
]

_CAMPOS_ROTULADOS: list[tuple[str, re.Pattern[str]]] = [
    # O valor não pode começar com "[": já anonimizado (reaplicar é idempotente).
    ("ENDERECO", re.compile(r"(?i)(endere[çc]o\s*:[ \t]*)((?![ \t\[])[^\n]+)")),
    (
        "NOME",
        re.compile(
            r"(?i:(?:nome|paciente|m[ãa]e|pai|acompanhante|respons[áa]vel)\s*:\s*)"
            rf"((?:{_TRATAMENTO})?{_SEQ_NOME})"
        ),
    ),
    ("NOME", re.compile(rf"\b(?:Sra?|Srta)\.\s+({_SEQ_NOME})")),
    ("MEDICO", re.compile(rf"\b(?:Dra?\.|Doutora?|Enf\.)\s+({_SEQ_NOME})")),
]

# Para log: só o que não tem ambiguidade com dado operacional.
_PADROES_LOG: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("CPF", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    ("CNS", re.compile(r"\b[1-2789]\d{2} \d{4} \d{4} \d{4}\b")),
]


@dataclass
class ResultadoAnonimizacao:
    texto: str
    contagem: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.contagem.values())


def anonimizar(texto: str) -> ResultadoAnonimizacao:
    """Troca dado pessoal por marcadores. Use antes de mandar texto de paciente
    para fora (Langfuse, dataset de avaliação, exportação)."""
    contagem: Counter = Counter()
    texto = texto or ""

    for rotulo, padrao in _CAMPOS_ROTULADOS:

        def _trocar_grupo(achado: re.Match[str], rotulo: str = rotulo) -> str:
            contagem[rotulo] += 1
            inicio, fim = achado.span(achado.lastindex)
            rel_inicio, rel_fim = inicio - achado.start(), fim - achado.start()
            inteiro = achado.group(0)
            return inteiro[:rel_inicio] + f"[{rotulo}]" + inteiro[rel_fim:]

        texto = padrao.sub(_trocar_grupo, texto)

    for rotulo, padrao in _PADROES:
        texto, n = padrao.subn(f"[{rotulo}]", texto)
        contagem[rotulo] += n

    return ResultadoAnonimizacao(texto=texto, contagem=+contagem)


def encontrar_dados_pessoais(texto: str) -> list[str]:
    """Tipos de dado pessoal ainda detectáveis (lista vazia = limpo). Serve de
    checagem final antes de gravar um dataset."""
    return sorted(anonimizar(texto).contagem)


def mascarar_para_log(texto: str) -> str:
    """Remove e-mail e CPF/CNS formatados de uma linha de log."""
    for rotulo, padrao in _PADROES_LOG:
        texto = padrao.sub(f"[{rotulo}]", texto)
    return texto
