"""security/token_compartilhado.py — comparação de segredos compartilhados.

Usado onde a autenticação é um token estático combinado entre as partes
(ex.: chamadas internas entre CRM e motor). Regras:
  - comparação em tempo constante (hmac.compare_digest) — `==`/`!=` vazam,
    pelo tempo de resposta, quantos caracteres iniciais estão corretos;
  - falha fechado: sem segredo configurado, NADA é aceito.
"""

import hmac


def token_valido(recebido: str | None, esperado: str | None) -> bool:
    """True só se `esperado` está configurado e `recebido` é idêntico a ele."""
    if not esperado or not recebido:
        return False
    return hmac.compare_digest(recebido.encode("utf-8"), esperado.encode("utf-8"))
