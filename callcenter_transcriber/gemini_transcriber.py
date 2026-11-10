"""Módulo Core do Transcritor de Call Center com Google Gemini.

Contém as regras de negócio, upload seguro via File API com MIME explícito,
guarda ativa com timeout de 30s e rechecagem final, fallback restrito e tratamento de metadados.
"""
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# Modelos recomendados para call center no catálogo oficial Gemini (Flash por padrão)
MODELOS_DISPONIVEIS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.8-flash",
]

NORMALIZER_VERSION = "v3_flac_vlc_3024"
normalizar_audio_ogg = None


def sincronizar_upload_session(uploaded_file, session_state: Dict[str, Any]) -> bool:
    """Verifica a identidade do upload ativo e invalida resultados e caches se o arquivo mudou.

    Retorna True se houve mudança no upload (invalidação realizada), False se o arquivo for o mesmo.
    Garante o cumprimento estrito do critério C3: apenas UMA cópia ativa na sessão e descarte
    imediato de resultado, avisos prévios e player ao trocar ou remover o upload.
    """
    if uploaded_file is None:
        if "current_file_token" in session_state:
            session_state.pop("current_file_token", None)
            session_state.pop("resultado", None)
            session_state.pop("audio_bytes", None)
            session_state.pop("mime_type", None)
            session_state.pop("ultimo_arquivo", None)
            session_state.pop("aviso_recuperacao", None)
            session_state.pop("meta_aviso", None)
            for k in list(session_state.keys()):
                if k.startswith("norm_"):
                    session_state.pop(k, None)
            return True
        return False

    token = f"{getattr(uploaded_file, 'file_id', '')}_{uploaded_file.name}_{uploaded_file.size}"
    if session_state.get("current_file_token") != token:
        session_state["current_file_token"] = token
        session_state.pop("resultado", None)
        session_state.pop("audio_bytes", None)
        session_state.pop("mime_type", None)
        session_state.pop("ultimo_arquivo", None)
        session_state.pop("aviso_recuperacao", None)
        session_state.pop("meta_aviso", None)
        for k in list(session_state.keys()):
            if k.startswith("norm_"):
                session_state.pop(k, None)
        return True

    return False


def _obter_normalizador():
    """Importa o normalizador de áudio de forma segura e desacoplada."""
    if normalizar_audio_ogg is not None:
        return normalizar_audio_ogg
    try:
        from callcenter_transcriber.audio_normalization import normalizar_audio_ogg as _norm
        return _norm
    except ImportError:
        try:
            from audio_normalization import normalizar_audio_ogg as _norm
            return _norm
        except ImportError:
            return None


def _obter_prefixo_valido(dados: bytes) -> Optional[float]:
    """Obtém a duração do prefixo íntegro pré-defeito via ogg_validation canônico."""
    try:
        from callcenter_transcriber.ogg_validation import obter_prefixo_valido_pre_defeito
        return obter_prefixo_valido_pre_defeito(dados)
    except ImportError:
        try:
            from ogg_validation import obter_prefixo_valido_pre_defeito
            return obter_prefixo_valido_pre_defeito(dados)
        except ImportError:
            try:
                from callcenter_transcriber.audio_normalization import obter_prefixo_valido_pre_defeito
                return float(obter_prefixo_valido_pre_defeito(dados))
            except ImportError:
                return None

MAPA_MIME_AUDIO = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}

LIMITE_TAMANHO_MB = 64.0
LIMITE_DURACAO_SEGUNDOS = 7200  # 2 horas (120 minutos)
FORMATOS_PERMITIDOS = [".mp3", ".ogg", ".m4a", ".aac"]


class FalaItem(BaseModel):
    timestamp: str = Field(description="Momento aproximado da fala no formato MM:SS (ex: '00:08')")
    locutor: str = Field(description="Nome e papel do interlocutor (ex: 'Atendente (Carlos)' ou 'Cliente (Juliana)')")
    papel: str = Field(description="Papel do participante: 'Atendente', 'Cliente' ou 'Outro'")
    texto: str = Field(description="Transcrição literal e fidedigna do que foi falado")


class MetadadosChamada(BaseModel):
    empresa_cliente_fantasia: str = Field(
        default="Não identificada",
        description="Nome fantasia ou marca da empresa do CLIENTE (ex: 'Supermercado Central', 'Farmácia Vida'). ATENÇÃO: NÃO colocar Infobrasil, pois Infobrasil é a nossa empresa que atende."
    )
    empresa_cliente_razao_social: Optional[str] = Field(
        default="Não citada",
        description="Razão social da empresa do CLIENTE se citada (ex: 'Comercial de Alimentos Silva Ltda'), senão 'Não citada'."
    )
    atendente: str = Field(
        default="Não identificado",
        description="Nome do atendente/técnico da Infobrasil que atendeu o chamado."
    )
    cliente: str = Field(
        default="Não identificado",
        description="Nome do cliente/usuário solicitante que conversou com o atendente."
    )
    protocolo_principal: Optional[str] = Field(
        default="Não citado",
        description="Número do protocolo principal gerado ou citado nesta chamada."
    )
    outros_protocolos: List[str] = Field(
        default_factory=list,
        description="Outros números de protocolos, tickets ou chamados anteriores citados na conversa (ex: ['88412', '91002'])."
    )
    status_resolucao: str = Field(
        default="Não especificado",
        description="Situação do chamado: 'Resolvido no atendimento', 'Pendente / Aguardando retorno', 'Em andamento' ou 'Encaminhado para outro setor/nível'."
    )
    prazo_informado: Optional[str] = Field(
        default="Não informado",
        description="Prazo ou previsão que o atendente acordou com o cliente (ex: 'Em até 2 horas', 'Até o fim da tarde', 'Próximo dia útil' ou 'Não informado')."
    )
    motivo_contato: str = Field(
        default="Suporte técnico",
        description="Motivo central do chamado (ex: 'Banco de dados travado', 'Erro na emissão de NF-e', 'Lentidão no PDV')."
    )
    resumo_atendimento: str = Field(
        default="",
        description="Resumo sucinto em 2 ou 3 frases sobre o atendimento e o desfecho."
    )
    sentimento_geral: str = Field(
        default="Neutro",
        description="Sentimento geral do cliente: 'Positivo / Satisfeito', 'Neutro' ou 'Insatisfeito / Reclamação'."
    )


def calcular_hash_audio(uploaded_file) -> str:
    """Calcula identificador determinístico baseado no file_id ou SHA-256 do conteúdo do áudio."""
    file_id = getattr(uploaded_file, "file_id", None)
    if file_id:
        return f"fid_{file_id}"

    pos_original = 0
    if hasattr(uploaded_file, "tell"):
        try:
            pos_original = uploaded_file.tell()
        except Exception:
            pos_original = 0

    uploaded_file.seek(0)
    conteudo = uploaded_file.read()
    uploaded_file.seek(pos_original)

    h = hashlib.sha256(conteudo).hexdigest()[:16]
    return f"sha_{h}_{len(conteudo)}"


def obter_mime_audio(caminho_ou_nome: str) -> str:
    """Mapeia extensão para o MIME type canônico suportado pelo Gemini File API.

    Rejeita extensões desconhecidas com ValueError para evitar envio com formato incorreto.
    """
    ext = Path(caminho_ou_nome).suffix.lower()
    if not ext or ext not in MAPA_MIME_AUDIO:
        ext_desc = ext if ext else "(sem extensão)"
        formatos = ", ".join(sorted(MAPA_MIME_AUDIO.keys()))
        raise ValueError(f"Extensão de áudio '{ext_desc}' não suportada. Formatos suportados: {formatos}")
    return MAPA_MIME_AUDIO[ext]


def inspecionar_audio(uploaded_file, session_cache: Optional[Dict[str, Any]] = None) -> dict:
    """Valida formato (sem .wav), tamanho (máx 64MB), integridade OGG (RFC 3533) e duração (máx 2h)."""
    nome = getattr(uploaded_file, "name", "audio.mp3")
    ext = Path(nome).suffix.lower()
    tamanho = getattr(uploaded_file, "size", None)
    if tamanho is None:
        try:
            uploaded_file.seek(0, os.SEEK_END)
            tamanho = uploaded_file.tell()
            uploaded_file.seek(0)
        except Exception:
            tamanho = 0

    tam_mb = tamanho / (1024 * 1024)

    info = {
        "valido": True,
        "erros": [],
        "avisos": [],
        "nome": nome,
        "ext": ext,
        "tamanho_mb": tam_mb,
        "duracao_segundos": 0.0,
        "duracao_formatada": "Não calculada",
        "detalhes_tecnicos": None,
    }

    # 1. Bloqueio de formatos sem compactação (.wav)
    if ext == ".wav":
        info["valido"] = False
        info["erros"].append(
            "O formato **.wav** é um formato sem compactação e não é permitido. "
            "Por favor, converta ou exporte sua gravação em formato compactado: **.ogg**, **.mp3** ou **.m4a**."
        )
        return info
    elif ext not in FORMATOS_PERMITIDOS:
        info["valido"] = False
        info["erros"].append(
            f"Extensão **{ext}** não permitida. Utilize apenas formatos compactados: **.ogg**, **.mp3**, **.m4a** ou **.aac**."
        )
        return info

    # 2. Bloqueio de tamanho (máximo 64 MB)
    if tam_mb > LIMITE_TAMANHO_MB:
        info["valido"] = False
        info["erros"].append(
            f"O arquivo possui **{tam_mb:.1f} MB**, ultrapassando o limite máximo de **{LIMITE_TAMANHO_MB:.0f} MB**. "
            "Gravações de telefonia compactadas em .ogg/.mp3 raramente passam de 25 MB. Compacte ou reduza o arquivo."
        )
        return info

    # 3. Validação estrutural do contêiner OGG (RFC 3533) com cache determinístico por hash/file_id
    if ext == ".ogg":
        pos_cur = 0
        if hasattr(uploaded_file, "tell"):
            try:
                pos_cur = uploaded_file.tell()
            except Exception:
                pos_cur = 0

        uploaded_file.seek(0)
        bytes_ogg = uploaded_file.read()
        uploaded_file.seek(pos_cur)

        cache_key = f"val_ogg_{calcular_hash_audio(uploaded_file)}"
        res_ogg = None
        if session_cache is not None and cache_key in session_cache:
            res_ogg = session_cache[cache_key]
        else:
            try:
                from callcenter_transcriber.ogg_validation import validar_ogg
            except ImportError:
                from ogg_validation import validar_ogg

            res_ogg = validar_ogg(bytes_ogg)
            if session_cache is not None:
                session_cache[cache_key] = res_ogg

        if not res_ogg.get("valido", False):
            info["detalhes_tecnicos"] = res_ogg.get("erro")

            falha_codigo = res_ogg.get("detalhes", {}).get("falha", "")
            pagina_falha = res_ogg.get("detalhes", {}).get("pagina", 0)
            codec_id = res_ogg.get("codec")

            # Verifica se é um contêiner OGG estruturado que possui início de stream válido (Vorbis ou Opus)
            # Falso OGG (m3u8, HTML, texto puro ou lixo sem b'OggS') NÃO deve tentar normalizar
            eh_recuperavel = (
                bytes_ogg.startswith(b"OggS")
                and (codec_id in ("Vorbis", "Opus") or pagina_falha > 0)
                and falha_codigo not in ("TAMANHO_INSUFICIENTE", "TIPO_INVALIDO", "VERSAO_INVALIDA")
            )

            if eh_recuperavel:
                info["valido"] = True
                info["requer_recuperacao"] = True
                dur_task1 = _obter_prefixo_valido(bytes_ogg)
                duracao_prefixo = dur_task1 if dur_task1 is not None else res_ogg.get("detalhes", {}).get("duracao_prefixo_valido")
                info["duracao_prefixo_valido"] = duracao_prefixo
                info["avisos"].append(
                    "Inconsistências estruturais detectadas no contêiner OGG. "
                    "Ao iniciar a transcrição, o sistema normalizará uma cópia do áudio automaticamente."
                )
                info["duracao_formatada"] = "Aferida na cópia normalizada"
            else:
                info["valido"] = False
                info["requer_recuperacao"] = False
                info["erros"].append(
                    "Arquivo de gravação OGG corrompido ou formato inválido. "
                    "Solicite uma nova exportação da chamada na central telefônica/PBX."
                )
                return info

        if res_ogg.get("avisos"):
            info["avisos"].extend(res_ogg["avisos"])

        if res_ogg.get("duracao") and res_ogg["duracao"] > 0:
            dur_seg = float(res_ogg["duracao"])
            info["duracao_segundos"] = dur_seg
            m, s = divmod(int(dur_seg), 60)
            h, m = divmod(m, 60)
            info["duracao_formatada"] = f"{h}h {m:02d}m {s:02d}s" if h > 0 else f"{m:02d}m {s:02d}s"

    # 4. Inspeção de duração da gravação (máximo 2 horas = 7200s) para demais formatos ou caso não calculada
    if info["duracao_segundos"] == 0.0:
        try:
            from mutagen import File as MutagenFile
            pos_cur = 0
            if hasattr(uploaded_file, "tell"):
                try:
                    pos_cur = uploaded_file.tell()
                except Exception:
                    pos_cur = 0

            uploaded_file.seek(0)
            bytes_audio = uploaded_file.read()
            uploaded_file.seek(pos_cur)

            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(bytes_audio)
                tmp_path = tmp.name

            try:
                m_audio = MutagenFile(tmp_path)
                if m_audio and m_audio.info and hasattr(m_audio.info, "length"):
                    dur_seg = float(m_audio.info.length)
                    info["duracao_segundos"] = dur_seg
                    m, s = divmod(int(dur_seg), 60)
                    h, m = divmod(m, 60)
                    info["duracao_formatada"] = f"{h}h {m:02d}m {s:02d}s" if h > 0 else f"{m:02d}m {s:02d}s"
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
        except Exception:
            pass

    if info["duracao_segundos"] > LIMITE_DURACAO_SEGUNDOS:
        info["valido"] = False
        info["erros"].append(
            f"A gravação possui **{info['duracao_formatada']}**, ultrapassando o limite de **2 horas de áudio** (120 min). "
            "Divida a ligação em partes menores para realizar a transcrição."
        )

    return info


def sanitizar_erro(erro: Exception, chave_api: Optional[str] = None) -> str:
    """Remove credenciais, chaves de API explícitas ou de ambiente, caminhos locais e tags HTML."""
    texto = str(erro)

    # 1. Redige chave explicitamente fornecida
    if chave_api and len(chave_api.strip()) >= 6:
        texto = texto.replace(chave_api.strip(), "***")

    # 2. Redige chave do ambiente se configurada
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    if env_key and len(env_key) >= 6:
        texto = texto.replace(env_key, "***")

    # 3. Padrões de chaves Google/Gemini
    texto = re.sub(r"AIza[0-9A-Za-z_-]{30,}", "AIzaSy***", texto)
    texto = re.sub(r"(?i)(api[_-]?key[:=]\s*)[A-Za-z0-9_\-\.]{8,}", r"\1***", texto)
    texto = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{15,}", r"\1***", texto)

    # 4. Remove caminhos absolutos do sistema de arquivos (Windows e Unix)
    texto = re.sub(r"[A-Za-z]:\\[\w\.\-\\\s]+", "[caminho_local]", texto)
    texto = re.sub(r"/(?:home|Users|var|tmp|etc)/[\w\.\-/\s]+", "[caminho_local]", texto)

    # 5. Remove tags HTML e comprime espaços em branco
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:250]


def deve_fazer_fallback_modelo(erro: Exception) -> bool:
    """Retorna True apenas para indisponibilidade (503/UNAVAILABLE) ou modelo inexistente/não suportado (404/400 modelo).

    JAMAIS faz fallback para erros genéricos de payload/áudio/argumento 400, mesmo que contenham substrings numéricas.
    """
    code = getattr(erro, "code", None)
    status = getattr(erro, "status", None)
    message = getattr(erro, "message", None) or str(erro)
    msg_lower = message.lower()

    if code is None:
        m_code = re.search(r"\b(400|401|403|404|429|500|502|503|504)\b", str(erro))
        if m_code:
            code = int(m_code.group(1))

    # 1. 503 / UNAVAILABLE / Sobrecarga temporária
    if code == 503 or status == "UNAVAILABLE":
        return True
    if re.search(r"\b(overloaded|high demand|resource_exhausted)\b", msg_lower) and code not in (400, 401, 403):
        return True

    # 2. 404 / NOT_FOUND (apenas se for relativo a modelo inexistente)
    if code == 404 or status == "NOT_FOUND":
        termos_modelo = ["model", "models/", "gemini-"]
        if any(t in msg_lower for t in termos_modelo):
            return True

    # 3. 400 / INVALID_ARGUMENT (WHITELIST ESTRITA DE MODELO)
    # Se o erro for 400, NUNCA faz fallback exceto se for explicitamente sobre o modelo não ser suportado/conhecido
    if code == 400 or status == "INVALID_ARGUMENT" or re.search(r"\b(400|invalid_argument)\b", msg_lower):
        termos_whitelist_modelo = [
            "model not found",
            "is not supported for this",
            "unknown model",
            "unsupported model",
            "invalid model",
            "deprecated model",
            "model does not exist",
        ]
        if any(t in msg_lower for t in termos_whitelist_modelo):
            return True
        # Erro 400 genérico, container de áudio inválido, chave inválida -> NÃO faz fallback
        return False

    return False


def eh_erro_503_indisponivel(erro: Exception) -> bool:
    """Verifica de forma estrita e estruturada se o erro é 503 / UNAVAILABLE.

    Regras estritas de classificação:
    1. Se houver código numérico explícito (code, status_code, response.status_code):
       - Erros 4xx (ex: 400, 401, 403, 404, 429) NUNCA realizam retry, mesmo que status seja 'UNAVAILABLE'.
       - Código 503 retorna True.
       - Qualquer outro código numérico (ex: 500, 502, 504) retorna False.
    2. Se o código numérico estiver ausente (None):
       - status estruturado == 'UNAVAILABLE' (ou enum gRPC UNAVAILABLE) retorna True.
    3. Erros genéricos sem atributos estruturados (ex: RuntimeError("503 ...") ou texto puro) NUNCA realizam retry.
    """
    # 1. Extração estruturada de código numérico
    code = getattr(erro, "code", None)
    if callable(code):
        try:
            code = code()
        except Exception:
            code = None

    status_code = getattr(erro, "status_code", None)
    if callable(status_code):
        try:
            status_code = status_code()
        except Exception:
            status_code = None

    resp = getattr(erro, "response", None)
    resp_code = getattr(resp, "status_code", None) if resp is not None else None

    # Prioridade 1: se qualquer código numérico estiver presente
    for c in (code, status_code, resp_code):
        if isinstance(c, int):
            if 400 <= c < 500:
                return False
            if c == 503:
                return True
            return False

    # Prioridade 2: código ausente -> checa status estruturado
    status = getattr(erro, "status", None)
    if isinstance(status, str) and status.strip().upper() == "UNAVAILABLE":
        return True
    if hasattr(status, "name") and str(status.name).upper() == "UNAVAILABLE":
        return True

    grpc_code = getattr(erro, "grpc_status_code", None)
    if grpc_code is not None:
        str_grpc = str(grpc_code).upper()
        if "UNAVAILABLE" in str_grpc or str_grpc == "14":
            return True

    # Prioridade 3: Erro sem atributos estruturados (mesmo que tenha "503" no texto) -> NUNCA retry!
    return False


def extrair_texto_transcricao(resp) -> Tuple[str, Optional[str]]:
    """Extrai texto e aviso de corte/incompletude tratando candidates, parts, finish_reason e block_reason."""
    if resp is None:
        return "", None

    # Verifica possíveis bloqueios no nível da resposta
    prompt_feedback = getattr(resp, "prompt_feedback", None)
    block_reason = getattr(prompt_feedback, "block_reason", None) if prompt_feedback else None
    if block_reason and str(block_reason).upper() not in ("NONE", ""):
        raise RuntimeError(f"O áudio/conteúdo foi bloqueado pela política de segurança da IA: {block_reason}")

    partes: List[str] = []
    aviso_incompleto: Optional[str] = None

    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        finish_reason = getattr(cand, "finish_reason", None)
        fr_str = getattr(finish_reason, "name", str(finish_reason)) if finish_reason else "STOP"
        if fr_str and fr_str.upper() not in ("STOP", "NONE", ""):
            aviso_incompleto = f"A transcrição pode estar incompleta (motivo de finalização: {fr_str})."

        content = getattr(cand, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None) or []
        for part in parts:
            at = getattr(part, "audio_transcription", None)
            if at and getattr(at, "text", None):
                partes.append(at.text.strip())
            elif getattr(part, "text", None):
                partes.append(part.text.strip())

    # Fallback para resp.text se nenhuma part gerou texto
    if not partes and getattr(resp, "text", None):
        partes.append(resp.text.strip())

    texto_final = "\n".join(partes).strip()
    return texto_final, aviso_incompleto


def processar_audio(
    arquivo,
    chave_api: str,
    modelo_desejado: str,
    status_placeholder=None,
    requer_recuperacao: bool = False,
    duracao_prefixo_valido: Optional[float] = None,
    session_cache: Optional[Dict[str, Any]] = None
) -> Tuple[dict, str, Optional[str]]:
    """Processa áudio usando Gemini File API com MIME explícito, timeout de 30s com rechecagem e fallback restrito."""
    client = genai.Client(api_key=chave_api)

    ext = Path(arquivo.name).suffix.lower() or ".mp3"
    mime_audio = obter_mime_audio(arquivo.name)

    arquivo.seek(0)
    audio_bytes = arquivo.read()

    aviso_recuperacao = None
    audio_recuperado = False
    duracao_recuperada = None
    audio_bytes_envio = audio_bytes

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
        tmp_file.write(audio_bytes)
        tmp_path = tmp_file.name

    gemini_file = None
    try:
        # Se requer recuperação estrutural de contêiner OGG
        if ext == ".ogg" and requer_recuperacao:
            if status_placeholder:
                status_placeholder.info("🛠️ Normalizando cópia do contêiner OGG para áudio FLAC lossless...")

            if duracao_prefixo_valido is None:
                duracao_prefixo_valido = _obter_prefixo_valido(audio_bytes)

            normalizador = _obter_normalizador()
            if normalizador is None:
                raise RuntimeError(
                    "[Etapa: Recuperação de Áudio] Módulo de normalização de áudio não disponível. "
                    "Solicite uma nova exportação da chamada na central telefônica/PBX."
                )

            cache_key_norm = f"norm_{calcular_hash_audio(arquivo)}_{NORMALIZER_VERSION}"
            res_norm = None
            if session_cache is not None and cache_key_norm in session_cache:
                res_norm = session_cache[cache_key_norm]
            else:
                res_norm = normalizador(tmp_path)
                if session_cache is not None:
                    session_cache[cache_key_norm] = res_norm

            if not res_norm.get("sucesso", False):
                err_msg = res_norm.get("erro") or "Falha desconhecida na normalização"
                raise RuntimeError(
                    f"[Etapa: Recuperação de Áudio] Falha ao normalizar áudio avariado: {err_msg}. "
                    "Solicite uma nova exportação da chamada na central telefônica/PBX."
                )

            duracao_recuperada = float(res_norm.get("duracao", 0.0) or 0.0)

            # Guarda estrita (T2-1): a normalização DEVE avançar além do prefixo íntegro pré-defeito.
            # Tolerância de ~0.05s destina-se apenas a compensar arredondamento de amostras/frames.
            # Distinção estrita: None (desconhecido) -> FALHA FECHADA.
            # limiar_prefixo == 0 (defeito no início) -> exige duracao_recuperada > 0.05s.
            if duracao_prefixo_valido is None:
                raise RuntimeError(
                    "[Etapa: Recuperação de Áudio] Não foi possível determinar o prefixo íntegro do contêiner danificado. "
                    "Por segurança, o envio foi interrompido. Solicite uma nova exportação da chamada na central telefônica/PBX."
                )

            limiar_prefixo = float(duracao_prefixo_valido)
            if duracao_recuperada <= (limiar_prefixo + 0.05):
                raise RuntimeError(
                    f"[Etapa: Recuperação de Áudio] Não foi possível recuperar a gravação além do trecho inicial ({duracao_recuperada:.2f}s). "
                    "O contêiner de áudio possui corrupção irrecuperável. "
                    "Solicite uma nova exportação da chamada na central telefônica/PBX."
                )

            audio_bytes_envio = res_norm["audio_bytes"]
            mime_audio = res_norm.get("mime_type", "audio/flac")
            dur_txt = f" ({duracao_recuperada:.1f}s decodificados)" if duracao_recuperada else ""
            aviso_recuperacao = (
                (res_norm.get("avisos") and res_norm["avisos"][0])
                or f"Aviso de Recuperação Parcial: gravação recuperada de contêiner danificado{dur_txt}. Timestamps e reprodução referem-se à cópia normalizada; trechos corrompidos podem ter sido cortados."
            )
            audio_recuperado = True

            # Sobrescreve o arquivo temporário com o áudio FLAC normalizado
            with open(tmp_path, "wb") as f_norm:
                f_norm.write(audio_bytes_envio)
        # -----------------------------------------------------------------
        # Etapa 1: Upload com MIME type explícito
        # -----------------------------------------------------------------
        if status_placeholder:
            status_placeholder.info(f"📤 Carregando áudio no serviço seguro do Google File API ({mime_audio})...")

        try:
            gemini_file = client.files.upload(
                file=tmp_path,
                config=types.UploadFileConfig(mime_type=mime_audio)
            )
        except Exception as e_up:
            raise RuntimeError(f"[Etapa: Upload do Arquivo] Falha ao enviar áudio (MIME {mime_audio}): {sanitizar_erro(e_up, chave_api)}") from e_up

        # -----------------------------------------------------------------
        # Etapa 2: Aguarda arquivo ficar ACTIVE (timeout 30s com rechecagem final)
        # -----------------------------------------------------------------
        arquivo_ativo = False
        estado_final = "DESCONHECIDO"
        for _ in range(30):
            if hasattr(gemini_file, "state"):
                estado_final = getattr(gemini_file.state, "name", str(gemini_file.state))
                if estado_final == "ACTIVE":
                    arquivo_ativo = True
                    break
                elif estado_final in ("FAILED", "ERROR"):
                    raise RuntimeError(f"[Etapa: Processamento do Arquivo] O áudio falhou no processamento do Gemini (estado: {estado_final}).")
            time.sleep(1)
            try:
                gemini_file = client.files.get(name=gemini_file.name)
            except Exception as e_get:
                raise RuntimeError(f"[Etapa: Processamento do Arquivo] Falha ao consultar estado do arquivo: {sanitizar_erro(e_get, chave_api)}") from e_get

        # Rechecagem final para cobrir se virou ACTIVE na 30ª consulta
        if not arquivo_ativo and hasattr(gemini_file, "state"):
            estado_final = getattr(gemini_file.state, "name", str(gemini_file.state))
            if estado_final == "ACTIVE":
                arquivo_ativo = True

        if not arquivo_ativo:
            raise TimeoutError(f"[Etapa: Processamento do Arquivo] Tempo limite esgotado (30s) aguardando ativação do áudio (estado: {estado_final}).")

        # -----------------------------------------------------------------
        # Etapa 3: Transcrição com lista de candidatos e fallback restrito
        # -----------------------------------------------------------------
        prompt_transcricao = (
            "Você é um auditor sênior de qualidade e transcrição de call center de TI e telecomunicações.\n"
            "Esta gravação é uma chamada de suporte técnico da empresa 'Infobrasil Sistemas' (ou apenas 'Infobrasil').\n\n"
            "REGRAS CRÍTICAS DE IDENTIFICAÇÃO DOS PARTICIPANTES:\n"
            "1. A INFOBRASIL SOMOS NÓS (a empresa que presta o atendimento de suporte). O 'Atendente' é SEMPRE quem atende pela Infobrasil.\n"
            "2. O 'Cliente' é a pessoa externa que liga ou recebe o contato solicitando suporte para a sua respectiva empresa (loja, farmácia, supermercado, etc.).\n"
            "3. DIFERENCIAÇÃO CLARA DE VOZES: Use a voz e o contexto de quem com certeza é o cliente (quem relata o problema/dúvida) para diferenciar com precisão a voz do atendente (quem atende com saudação institucional e busca resolver o chamado).\n"
            "4. PADRÃO ESTRITO DE ROTULAÇÃO (NÃO MISTURE NOMES):\n"
            "   - Se souber o nome do atendente: 'Atendente (Nome)'\n"
            "   - Se souber o nome do cliente: 'Cliente (Nome)'\n"
            "   - NUNCA coloque dois nomes juntos ou entre parênteses como '(NomeAtendente / NomeCliente)'.\n"
            "   - Se houver qualquer dúvida sobre o nome, rotule estritamente como apenas 'Atendente' ou 'Cliente', sem inventar parênteses.\n"
            "5. FORMATO OBRIGATÓRIO POR LINHA:\n"
            "   Cada fala DEVE começar em uma linha separada no formato:\n"
            "   [MM:SS] Atendente (Nome): Fala dita\n"
            "   [MM:SS] Cliente (Nome): Fala dita\n"
            "   NUNCA junte falas de interlocutores diferentes no mesmo parágrafo. Se o interlocutor mudar, crie uma nova linha com o novo timestamp.\n"
            "6. Transcreva com fidelidade absoluta todo o diálogo até o final da ligação."
        )

        candidatos = [modelo_desejado]
        for alt in ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.8-flash"]:
            if alt not in candidatos:
                candidatos.append(alt)

        texto_transcrito = ""
        aviso_corte = None
        modelo_utilizado = modelo_desejado

        for idx, mod in enumerate(candidatos):
            try:
                if status_placeholder:
                    status_placeholder.info(f"🎧 Transcrevendo áudio com **{mod}**...")

                resp_trans = client.models.generate_content(
                    model=mod,
                    contents=[gemini_file, prompt_transcricao]
                )
                texto_transcrito, aviso_corte = extrair_texto_transcricao(resp_trans)
                if not texto_transcrito:
                    raise RuntimeError(f"O modelo {mod} retornou conteúdo vazio.")
                modelo_utilizado = mod
                break
            except Exception as e:
                if deve_fazer_fallback_modelo(e) and idx < len(candidatos) - 1:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"[Etapa: Transcrição - Modelo: {mod}] {sanitizar_erro(e, chave_api)}") from e

        if not texto_transcrito:
            raise RuntimeError(f"[Etapa: Transcrição - Modelo: {modelo_utilizado}] Não foi possível gerar a transcrição do áudio.")

        # -----------------------------------------------------------------
        # Etapa 4: Parsing do diálogo
        # -----------------------------------------------------------------
        padrao = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:]+):\s*(.*)")
        dialogo = []
        for linha in texto_transcrito.split("\n"):
            linha_limpa = linha.strip()
            if not linha_limpa:
                continue
            m = padrao.match(linha_limpa)
            if m:
                tempo, locutor, fala = m.groups()
                loc_clean = locutor.strip()
                papel = "Atendente" if "atendente" in loc_clean.lower() else ("Cliente" if "cliente" in loc_clean.lower() else "Outro")
                dialogo.append({
                    "timestamp": tempo,
                    "locutor": loc_clean,
                    "papel": papel,
                    "texto": fala.strip()
                })
            else:
                if dialogo:
                    dialogo[-1]["texto"] += " " + linha_limpa
                else:
                    dialogo.append({
                        "timestamp": "00:00",
                        "locutor": "Áudio",
                        "papel": "Outro",
                        "texto": linha_limpa
                    })

        # -----------------------------------------------------------------
        # Etapa 5: Extração de Metadados via JSON estruturado
        # -----------------------------------------------------------------
        if status_placeholder:
            status_placeholder.info("⚡ Identificando dados do cliente, empresa, protocolos, prazos e desfecho...")

        prompt_meta = (
            "Você é um analista de qualidade da INFOBRASIL SISTEMAS.\n"
            "Analise a transcrição deste atendimento e extraia com precisão os seguintes dados em JSON:\n\n"
            "1. empresa_cliente_fantasia: Nome fantasia ou marca da empresa do CLIENTE (atenção: NÃO coloque Infobrasil, pois Infobrasil é a nossa empresa de suporte). Ex: 'Supermercado Central', 'Drogaria Silva', 'Padaria Central'. Se não citada, coloque 'Não identificada'.\n"
            "2. empresa_cliente_razao_social: Razão social da empresa do CLIENTE se mencionada (ex: 'Comercial de Alimentos Ltda'). Se não citada, coloque 'Não citada'.\n"
            "3. atendente: Nome do atendente/técnico da Infobrasil que prestou o suporte.\n"
            "4. cliente: Nome da pessoa (cliente) que solicitou o atendimento.\n"
            "5. protocolo_principal: Número de protocolo gerado ou principal citado neste atendimento (ou 'Não citado').\n"
            "6. outros_protocolos: Lista com quaisquer outros números de chamados, tickets ou protocolos anteriores citados durante a conversa (ex: ['88412']). Se nenhum outro for citado, lista vazia.\n"
            "7. status_resolucao: 'Resolvido no atendimento', 'Pendente / Aguardando retorno', 'Em andamento' ou 'Encaminhado para outro setor/nível'.\n"
            "8. prazo_informado: Prazo ou previsão que o atendente prometeu/acordou com o cliente (ex: 'Em até 2 horas', 'Até o fim da tarde', 'Próximo dia útil' ou 'Não informado').\n"
            "9. motivo_contato: Descrição concisa do problema/dúvida central (ex: 'Banco de dados travado', 'Erro na emissão de NF-e', 'Lentidão no PDV').\n"
            "10. resumo_atendimento: Resumo executivo em 2 ou 3 frases sobre o atendimento e o desfecho.\n"
            "11. sentimento_geral: Classificação do sentimento do cliente: 'Positivo / Satisfeito', 'Neutro' ou 'Insatisfeito / Reclamação'.\n\n"
            f"Transcrição da chamada:\n{texto_transcrito[:16000]}"
        )

        modelo_meta = "gemini-3.5-flash-lite" if "transcribe" in modelo_utilizado else modelo_utilizado
        meta_aviso = None
        default_meta_dict = {
            "empresa_cliente_fantasia": "Não identificada",
            "empresa_cliente_razao_social": "Não citada",
            "atendente": "Identificado no diálogo",
            "cliente": "Identificado no diálogo",
            "protocolo_principal": "Não citado",
            "outros_protocolos": [],
            "status_resolucao": "Não especificado",
            "prazo_informado": "Não informado",
            "motivo_contato": "Atendimento telefônico",
            "resumo_atendimento": "Transcrição realizada com sucesso.",
            "sentimento_geral": "Neutro"
        }
        meta_dict = None
        max_tentativas_meta = 3
        backoff_meta = [2.0, 4.0]

        for tentativa_meta in range(1, max_tentativas_meta + 1):
            try:
                resp_meta = client.models.generate_content(
                    model=modelo_meta,
                    contents=[prompt_meta],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=MetadadosChamada,
                        temperature=0.1,
                        http_options=types.HttpOptions(
                            retry_options=types.HttpRetryOptions(attempts=1)
                        )
                    )
                )
                dados_json = json.loads(resp_meta.text)
                if not isinstance(dados_json, dict):
                    # Fail-safe: json.loads retornou null (None), list ([]), string, int, etc.
                    meta_aviso = "Transcrição concluída; não foi possível extrair dados complementares da chamada."
                    meta_dict = dict(default_meta_dict)
                    break
                meta_dict = dados_json
                break
            except Exception as e_meta:
                if eh_erro_503_indisponivel(e_meta):
                    if tentativa_meta < max_tentativas_meta:
                        tempo_espera = backoff_meta[tentativa_meta - 1]
                        if status_placeholder:
                            status_placeholder.warning(
                                f"Serviço de metadados temporariamente indisponível (503). Nova tentativa em {int(tempo_espera)}s (tentativa {tentativa_meta}/{max_tentativas_meta})..."
                            )
                        time.sleep(tempo_espera)
                        continue
                    else:
                        meta_aviso = "Transcrição concluída; dados complementares temporariamente indisponíveis."
                        meta_dict = dict(default_meta_dict)
                        break
                else:
                    meta_aviso = "Transcrição concluída; não foi possível extrair dados complementares da chamada."
                    meta_dict = dict(default_meta_dict)
                    break

        if meta_dict is None:
            meta_dict = dict(default_meta_dict)

        # Concatena aviso de corte de tokens se ocorrido
        if aviso_corte:
            if meta_aviso:
                meta_aviso = f"{aviso_corte} | {meta_aviso}"
            else:
                meta_aviso = aviso_corte

        resultado_final = {
            "empresa_cliente_fantasia": meta_dict.get("empresa_cliente_fantasia") or "Não identificada",
            "empresa_cliente_razao_social": meta_dict.get("empresa_cliente_razao_social") or "Não citada",
            "atendente": meta_dict.get("atendente") or "Não identificado",
            "cliente": meta_dict.get("cliente") or "Não identificado",
            "protocolo_principal": meta_dict.get("protocolo_principal") or "Não citado",
            "outros_protocolos": meta_dict.get("outros_protocolos") or [],
            "status_resolucao": meta_dict.get("status_resolucao") or "Não especificado",
            "prazo_informado": meta_dict.get("prazo_informado") or "Não informado",
            "motivo_contato": meta_dict.get("motivo_contato") or "Não especificado",
            "resumo_atendimento": meta_dict.get("resumo_atendimento") or "Sem resumo disponível.",
            "sentimento_geral": meta_dict.get("sentimento_geral") or "Neutro",
            "dialogo": dialogo,
            "texto_bruto": texto_transcrito,
            "audio_bytes": audio_bytes_envio,
            "mime_type": mime_audio,
            "audio_recuperado": audio_recuperado,
            "recuperacao_parcial": audio_recuperado,
            "duracao_recuperada": duracao_recuperada,
            "aviso_recuperacao": aviso_recuperacao
        }

        return resultado_final, modelo_utilizado, meta_aviso

    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        if gemini_file and hasattr(gemini_file, "name"):
            try:
                client.files.delete(name=gemini_file.name)
            except Exception:
                pass
