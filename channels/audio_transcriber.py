"""Download de mídia da Cloud API da Meta + transcrição via Groq Whisper."""

import logging

import httpx

log = logging.getLogger(__name__)

_META_BASE = "https://graph.facebook.com/v25.0"
_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_DOWNLOAD_TIMEOUT = 30.0
_TRANSCRIPTION_TIMEOUT = 60.0

_EXT_MAP = {
    "audio/ogg": "ogg",
    "audio/ogg; codecs=opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/x-m4a": "m4a",
}


async def baixar_midia_meta(media_id: str, token: str) -> tuple[bytes, str]:
    """Retorna (bytes, mime_type) de qualquer mídia da Cloud API (áudio, imagem, documento).

    A Cloud API exige dois passos: (1) buscar a URL temporária do arquivo via
    GET /{media_id}, (2) baixar o conteúdo dessa URL — ambos com o mesmo Bearer token.
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
        info_resp = await client.get(f"{_META_BASE}/{media_id}", headers=headers)
        info_resp.raise_for_status()
        info = info_resp.json()

        url = info["url"]
        mime_type: str = info.get("mime_type", "application/octet-stream")

        midia_resp = await client.get(url, headers=headers)
        midia_resp.raise_for_status()
        return midia_resp.content, mime_type


# Alias mantido para não quebrar imports existentes
baixar_audio_meta = baixar_midia_meta


async def transcrever_audio(audio_bytes: bytes, mime_type: str, groq_api_key: str) -> str:
    """Transcreve áudio em português via Groq Whisper large-v3-turbo.

    Retorna o texto transcrito. Levanta httpx.HTTPStatusError em falha HTTP.
    """
    base_mime = mime_type.split(";")[0].strip()
    ext = _EXT_MAP.get(mime_type) or _EXT_MAP.get(base_mime, "ogg")

    async with httpx.AsyncClient(timeout=_TRANSCRIPTION_TIMEOUT) as client:
        resp = await client.post(
            _GROQ_URL,
            headers={"Authorization": f"Bearer {groq_api_key}"},
            files={"file": (f"audio.{ext}", audio_bytes, base_mime)},
            data={
                "model": "whisper-large-v3-turbo",
                "language": "pt",
                "response_format": "text",
            },
        )
        resp.raise_for_status()
        return resp.text.strip()
