"""security/guardrail_entrada.py — barreira determinística antes do LLM.

Adaptado do Tech Challenge FIAP Fase 3 (safety/guardrails.py → check_input).
Lá, a lição foi: a garantia de segurança tem que estar no CÓDIGO, não no
modelo — regra determinística é auditável, testável e não muda com o prompt.

Aqui o público é o PACIENTE no WhatsApp, não o médico. As duas situações em
que o agente não pode depender do LLM:

  - urgência: sinais de alarme (dor no peito, falta de ar, AVC...). A resposta
    certa é sempre a mesma — SAMU 192 / pronto-socorro — e não pode esperar
    o LLM decidir, nem correr o risco de ele "agendar uma consulta".
  - crise: ideação suicida ou autolesão → CVV 188.

Mensagem legítima não pode disparar ("meu pai teve AVC ano passado, quero
marcar consulta"): nomes de doença (AVC, infarto) só contam com contexto de
agora ("acho que estou tendo"), e sintoma precedido de passado ou negação
("tive dor no peito", "sem falta de ar") não conta.
A bateria de casos fica em tests/test_guardrail_entrada.py.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class Categoria(StrEnum):
    urgencia = "urgencia"
    crise = "crise"


@dataclass(frozen=True)
class Classificacao:
    categoria: Categoria
    # Nome curto da regra que disparou — vai para log/auditoria no lugar do
    # texto do paciente (que é dado de saúde e não pode ir ao log).
    regra: str


def _normalizar(texto: str) -> str:
    """minúsculas, sem acento e com espaços colapsados."""
    sem_acento = unicodedata.normalize("NFKD", texto or "")
    sem_acento = sem_acento.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", sem_acento).strip()


# "estou tendo", "to tendo", "acho que e", "acho que estou tendo um"...
_AGORA = (
    r"(?:estou|to|ta|esta|estamos|acho que (?:e|eh|estou|ta|esta|to)|parece)"
    r"(?: tendo| com)?(?: um| uma)?"
)

# Ordem importa só para o nome da regra no log: crise antes de urgência,
# porque "tomei uma caixa de remédio pra morrer" é crise (CVV) e urgência.
_REGRAS: list[tuple[Categoria, str, re.Pattern[str]]] = [
    (
        Categoria.crise,
        "ideacao_suicida",
        re.compile(
            r"\b(?:quero|vou|penso em|pensando em|vontade de|desejo de) "
            r"(?:me )?(?:morrer|me matar|matar)\b"
            r"|\bme matar\b"
            r"|\bsuicid"
            r"|\btirar (?:a )?(?:minha )?(?:propria )?vida\b"
            r"|\bacabar com (?:a )?minha vida\b"
            r"|\bnao (?:quero|aguento) mais viver\b"
            r"|\bmelhor (?:eu )?(?:estar|estivesse) mort[oa]\b"
        ),
    ),
    (
        Categoria.crise,
        "autolesao",
        re.compile(r"\b(?:me cortei|me cortando|me cortar|automutila|me machucar de proposito)"),
    ),
    (
        Categoria.urgencia,
        "dor_toracica",
        re.compile(
            r"\b(?:dor|aperto|pressao|queimacao) (?:forte |muito forte |intensa )?"
            r"(?:no|na) (?:peito|coracao)\b"
            r"|\bpeito (?:apertado|doendo muito)\b"
            rf"|\b{_AGORA} (?:infartando|infarto|enfarto|enfartando|ataque cardiaco)\b"
            r"|\binfartando\b"
        ),
    ),
    (
        Categoria.urgencia,
        "falta_de_ar",
        re.compile(
            r"\bfalta de ar\b"
            r"|\bnao (?:consigo|consegue|to conseguindo|estou conseguindo|esta conseguindo) "
            r"respirar\b"
            r"|\bdificuldade (?:para|pra|de|em) respirar\b"
            r"|\bsufocando\b"
            r"|\bficando roxo\b|\blabios roxos\b"
        ),
    ),
    (
        Categoria.urgencia,
        "sinais_avc",
        re.compile(
            r"\bboca (?:torta|entortou|ficou torta)\b"
            r"|\b(?:um )?lado do (?:corpo|rosto) (?:paralisado|dormente|caido|parou)\b"
            r"|\bfala (?:enrolada|arrastada)\b|\bfalando enrolado\b"
            r"|\bnao (?:consigo|consegue) (?:mexer|levantar|sentir) "
            r"(?:o|a|um|uma) (?:braco|perna|lado)\b"
            rf"|\b{_AGORA} (?:avc|derrame)\b"
            r"|\btendo (?:um )?(?:avc|derrame)\b"
        ),
    ),
    (
        Categoria.urgencia,
        "perda_consciencia",
        re.compile(
            r"\bdesmai(?:ou|ei|ando|ado|ada)\b"
            r"|\bdesacordad[oa]\b"
            r"|\bnao (?:acorda|responde mais|esta respondendo)\b"
            r"|\bperdeu (?:a )?(?:consciencia|os sentidos)\b"
            r"|\bconvuls(?:ao|ionando|ionou|oes)\b"
            r"|\bataque epileptico\b"
        ),
    ),
    (
        Categoria.urgencia,
        "sangramento",
        re.compile(
            r"\bsangr(?:amento|ando) (?:muito|forte|intenso|sem parar|que nao para)\b"
            r"|\bsangue que nao para\b"
            rf"|\b{_AGORA} hemorragia\b"
            r"|\b(?:vomitando|vomitei|tossindo|tossi) sangue\b"
        ),
    ),
    (
        Categoria.urgencia,
        "alergia_grave",
        re.compile(
            r"\bgarganta (?:esta |ta )?(?:fechando|fechou|inchada|inchando)\b"
            r"|\b(?:lingua|labios?|rosto) (?:inchad[oa]s?|inchando) "
            r".{0,40}\b(?:respirar|ar|garganta)\b"
            r"|\b(?:choque )?anafila"
        ),
    ),
    (
        Categoria.urgencia,
        "trauma_grave",
        re.compile(
            r"\bfratura exposta\b"
            r"|\bosso (?:esta |ta |ficou )?(?:pra|para) fora\b"
            r"|\b(?:foi )?atropelad[oa]\b"
            r"|\bbateu (?:forte )?a cabeca e (?:desmaiou|esta vomitando|ta vomitando|nao acorda)\b"
        ),
    ),
    (
        Categoria.urgencia,
        "intoxicacao",
        re.compile(
            r"\b(?:tomei|tomou|engoliu|engoli) "
            r"(?:uma caixa|a cartela|varios|varias|muitos|muitas|todos os|todas as) "
            r"(?:de )?(?:comprimidos|remedios|pilulas|capsulas)\b"
            r"|\boverdose\b"
            r"|\b(?:bebeu|tomou|engoliu) (?:agua sanitaria|veneno|produto de limpeza|soda)\b"
        ),
    ),
]


# Contexto logo ANTES do sintoma que indica passado ou negação: "tive dor no
# peito ano passado", "sem falta de ar". Vale só para urgência — em crise o
# custo de não responder é alto demais ("não quero mais viver" tem "nao").
_JANELA_CONTEXTO = 25
_PASSADO_OU_NEGACAO = re.compile(
    r"\b(?:tive|teve|tivemos|tiveram|tinha|ja tive|historico de|nao tenho|nao tem|"
    r"nao estou com|nao to com|nao sinto|nao senti|sem)\b"
)


def _vale(categoria: Categoria, texto: str, inicio: int) -> bool:
    if categoria is Categoria.crise:
        return True
    antes = texto[max(0, inicio - _JANELA_CONTEXTO) : inicio]
    return not _PASSADO_OU_NEGACAO.search(antes)


def classificar_entrada(texto: str) -> Classificacao | None:
    """Devolve a categoria de risco da mensagem do paciente, ou None se nenhuma
    regra disparar. Puro e determinístico — não acessa banco nem LLM."""
    normalizado = _normalizar(texto)
    if not normalizado:
        return None
    for categoria, regra, padrao in _REGRAS:
        for achado in padrao.finditer(normalizado):
            if _vale(categoria, normalizado, achado.start()):
                return Classificacao(categoria=categoria, regra=regra)
    return None


MENSAGEM_URGENCIA = (
    "⚠️ Pelo que você descreveu, isso pode ser uma emergência.\n\n"
    "Ligue agora para o SAMU (192) ou vá ao pronto-socorro mais próximo. "
    "Não espere pela consulta nem por uma resposta por aqui."
)

MENSAGEM_CRISE = (
    "Sinto muito que você esteja passando por isso. Você merece ajuda agora.\n\n"
    "Ligue para o CVV no 188 (gratuito, 24 horas) ou converse pelo site cvv.org.br. "
    "Se houver risco imediato, ligue para o SAMU (192)."
)


def mensagem_para(categoria: Categoria) -> str:
    """Texto fixo enviado ao paciente. Nunca gerado pelo LLM.

    Não promete "avisei a equipe": o aviso depende de haver alguém disponível
    no handoff, e uma promessa falsa numa emergência é pior que nenhuma."""
    return MENSAGEM_CRISE if categoria is Categoria.crise else MENSAGEM_URGENCIA
