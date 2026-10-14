"""Fonte única de verdade para normalização de telefone.

Todo ponto que LÊ ou ESCREVE `contatos.telefone` — busca por contato, criação
de contato, resolução de identidade — deve passar o número por
`normalizar_telefone` ANTES de qualquer `.eq()`/`.insert()`. Isso garante que o
mesmo número de WhatsApp resolva sempre para a MESMA string canônica,
independentemente do formato que a Meta (Cloud API) ou o LLM
entregou. Sem isso, um número vira dois contatos e duas conversas (ver migration
20260707000000_contatos_normalizar_telefone.sql).
"""

import re

__all__ = ["normalizar_telefone"]


def normalizar_telefone(raw: str) -> str:
    """Normaliza um número WhatsApp para E.164 canônico.

    Aceita JID do WhatsApp ("5567...@s.whatsapp.net"), E.164 ("+5567..."),
    só dígitos, ou o formato "from" da Cloud API. Para números brasileiros
    (DDI 55) com DDD + 8 dígitos, o provider (Meta) omitiu
    o 9º dígito do celular — a função o insere após o DDD. Números que não
    começam com 55 são tratados como não-brasileiros e só recebem o prefixo '+'.

    É IDEMPOTENTE: normalizar um número já canônico devolve o mesmo valor, então
    é seguro chamá-la em qualquer camada sem risco de dupla transformação.

    Exemplos:
        "+5567999991234"            -> "+5567999991234"
        "556799991234"             -> "+5567999991234"  (insere 9 omitido)
        "5567999991234"            -> "+5567999991234"
        "67999991234"              -> "+5567999991234"  (celular BR sem DDI 55)
        "556799991234@s.whatsapp.net" -> "+5567999991234"
        "15551234567"              -> "+15551234567"    (não-BR, sem inserção)
    """
    numero = raw.split("@")[0]  # remove sufixo JID se presente
    digits = re.sub(r"\D", "", numero)

    if not digits.startswith("55"):
        # Celular brasileiro digitado SEM o DDI 55 (ex.: "67999991234")? O
        # nacional tem 11 dígitos: DDD (2) + 9XXXXXXXX (o 9 do celular). Detecção
        # conservadora pelo 9 na 3ª posição, para NÃO confundir com números
        # internacionais de 11 dígitos (ex.: US "1..."). Prepende 55 e segue no
        # fluxo BR abaixo — assim "67999991234" == "+5567999991234". Fixo/8 díg
        # sem DDI é raro no WhatsApp e segue tratado como internacional.
        if len(digits) == 11 and digits[2] == "9":
            digits = "55" + digits
        else:
            return f"+{digits}"

    resto = digits[2:]
    if len(resto) == 10:
        resto = resto[:2] + "9" + resto[2:]
    return f"+55{resto}"
