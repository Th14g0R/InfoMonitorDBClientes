"""Módulo de validação estrutural e conformidade de contêineres OGG (RFC 3533).

Validador puro e determinístico de integridade de áudio OGG para call center.
Verifica:
- Assinatura mágica b'OggS' e versão 0 (RFC 3533).
- Integridade de checksum CRC-32 (polinômio 0x04C11DB7) com oráculo Mutagen.
- Encadeamento contíguo de páginas por lacing (sem gaps, overlaps ou lixo residual).
- Suporte a streams encadeados em série (Chaining: Stream A com BOS..EOS seguido de Stream B com BOS..EOS).
- Rejeição de multiplexação concorrente (páginas intercaladas de múltiplos seriais em um mesmo stream).
- Contiguidade e ordenação de números de sequência (seq) por serial.
- Flags obrigatórias de início (BOS) e encerramento (EOS).
- Mensagens de erro claras e orientativas para a interface (UI), com detalhes técnicos em 'detalhes'.
- Extração de cabeçalhos e metadados de codecs suportados (Vorbis e Opus):
  canais, sample rate e duração total exata calculada a partir dos granules por stream.
"""
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Union

# Oráculo de bitswap e conversão rápida de CRC compatível com Mutagen
try:
    from mutagen._util import cdata
    _BITSWAP = cdata.bitswap
    _USE_MUTAGEN_FAST_CRC = True
except ImportError:
    _USE_MUTAGEN_FAST_CRC = False
    _BITSWAP = None

# Tabela oficial RFC 3533 pré-calculada (fallback independente)
def _gerar_tabela_crc() -> List[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            if r & 0x80000000:
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                r = (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table

_CRC_TABLE = _gerar_tabela_crc()


def calcular_ogg_crc(pagina_bytes: Union[bytes, bytearray, memoryview]) -> int:
    """Calcula o checksum CRC-32 oficial da RFC 3533 com os bytes 22..25 zerados."""
    if _USE_MUTAGEN_FAST_CRC and _BITSWAP is not None:
        arr = bytearray(pagina_bytes)
        arr[22:26] = b"\x00\x00\x00\x00"
        crc_raw = (~zlib.crc32(arr.translate(_BITSWAP), -1)) & 0xFFFFFFFF
        crc_be = struct.pack(">I", crc_raw).translate(_BITSWAP)
        return struct.unpack("<I", crc_be)[0]
    else:
        crc = 0
        arr = bytearray(pagina_bytes)
        arr[22:26] = b"\x00\x00\x00\x00"
        for b in arr:
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ b]
        return crc


def _extrair_info_vorbis(primeiro_pacote: bytes) -> tuple[Optional[int], Optional[int]]:
    """Extrai canais e taxa de amostragem do cabeçalho de identificação Vorbis."""
    if len(primeiro_pacote) >= 16 and primeiro_pacote.startswith(b"\x01vorbis"):
        versao = struct.unpack("<I", primeiro_pacote[7:11])[0]
        if versao == 0:
            canais = primeiro_pacote[11]
            sample_rate = struct.unpack("<I", primeiro_pacote[12:16])[0]
            return canais, sample_rate
    return None, None


def _extrair_info_opus(primeiro_pacote: bytes) -> tuple[Optional[int], Optional[int], int]:
    """Extrai canais, taxa nominal e pre-skip do cabeçalho OpusHead."""
    if len(primeiro_pacote) >= 16 and primeiro_pacote.startswith(b"OpusHead"):
        canais = primeiro_pacote[9]
        pre_skip = struct.unpack("<H", primeiro_pacote[10:12])[0]
        sample_rate = struct.unpack("<I", primeiro_pacote[12:16])[0] or 48000
        return canais, sample_rate, pre_skip
    return None, None, 0


def obter_prefixo_valido_pre_defeito(dados: Union[bytes, bytearray]) -> Optional[float]:
    """Calcula a duração (em segundos) da última página com conformidade RFC 3533 válida antes do defeito.

    Contrato Canônico Unificado:
    - Retorna None se os dados forem desconhecidos, truncados antes do cabeçalho ou não iniciarem com
      página BOS Vorbis/Opus válida com seq=0 e CRC-32 íntegro (sinalizando FALHA FECHADA para contêiner inválido).
    - Retorna 0.0 se a página 0 (BOS) for legítima, mas o defeito ocorrer antes de qualquer áudio decodificado (granule=0).
    - Retorna float (> 0.0) correspondente à duração do último granule válido contíguo antes da primeira avaria
      (ex: falha de CRC, salto de seq, quebra de serial, truncamento ou gap).
    - Trata corretamente fluxos Vorbis (sample_rate do cabeçalho de id) e Opus (48kHz, descontando pre_skip).
    - Preserva o granule anterior quando uma página intermediária contiver a sentinela 0xFFFFFFFFFFFFFFFF (-1).
    """
    if not isinstance(dados, (bytes, bytearray)):
        return None

    limite = len(dados)
    if limite < 28 or dados[:4] != b"OggS":
        return None

    # Versão do contêiner deve ser estritamente 0 (RFC 3533)
    if dados[4] != 0:
        return None

    # Página 0 (offset 0): flag BOS obrigatória (bit 0x02)
    h_type_p0 = dados[5]
    if not (h_type_p0 & 0x02):
        return None

    # Validação do cabeçalho da página 0
    num_seg_p0 = dados[26]
    tamanho_cab_p0 = 27 + num_seg_p0
    if limite < tamanho_cab_p0:
        return None

    body_len_p0 = sum(dados[27:tamanho_cab_p0])
    page0_len = tamanho_cab_p0 + body_len_p0
    if limite < page0_len:
        return None

    # seq da página 0 DEVE ser 0 conforme RFC 3533
    p0_seq = struct.unpack("<I", dados[18:22])[0]
    if p0_seq != 0:
        return None

    # CRC-32 da página 0
    p0_crc_gravado = struct.unpack("<I", dados[22:26])[0]
    if calcular_ogg_crc(dados[:page0_len]) != p0_crc_gravado:
        return None

    # Extração de codec na página 0
    p0_serial = struct.unpack("<I", dados[14:18])[0]
    body_p0 = dados[tamanho_cab_p0:page0_len]

    sample_rate = 8000
    opus_preskip = 0
    codec = None

    if body_p0.startswith(b"\x01vorbis"):
        codec = "Vorbis"
        canais, sr = _extrair_info_vorbis(body_p0)
        if sr and sr > 0:
            sample_rate = sr
        else:
            return None
    elif body_p0.startswith(b"OpusHead"):
        codec = "Opus"
        canais, sr, pre_skip = _extrair_info_opus(body_p0)
        sample_rate = 48000
        opus_preskip = pre_skip
    else:
        return None

    # Página 0 íntegra confirmada! Itera pelas páginas sequenciais subsequentes
    last_valid_granule = 0
    prev_seq = 0
    offset = page0_len

    while offset < limite:
        if offset + 27 > limite or dados[offset:offset+4] != b"OggS":
            break

        ver, ht, granule, serial, seq, stored_crc, n_seg = struct.unpack(
            "<BBQIIIB", dados[offset+4:offset+27]
        )

        if ver != 0:
            break

        # Continuidade de serial e sequência contígua no fluxo
        if serial != p0_serial or seq != prev_seq + 1:
            break

        if offset + 27 + n_seg > limite:
            break

        b_len = sum(dados[offset+27 : offset+27+n_seg])
        p_len = 27 + n_seg + b_len
        if offset + p_len > limite:
            break

        # Verificação estrita de CRC-32 RFC 3533
        if calcular_ogg_crc(dados[offset : offset+p_len]) != stored_crc:
            break

        # Página válida confirmada: atualiza granule ignorando a sentinela -1
        if granule != 0xFFFFFFFFFFFFFFFF:
            last_valid_granule = granule
        prev_seq = seq
        offset += p_len

        # Encerramento formal do stream
        if ht & 0x04:
            break

    # Cálculo final da duração pré-defeito
    if codec == "Vorbis":
        dur = max(0.0, last_valid_granule / float(sample_rate))
    elif codec == "Opus":
        amostras = max(0, last_valid_granule - opus_preskip)
        dur = max(0.0, amostras / 48000.0)
    else:
        dur = 0.0

    return round(dur, 3)


def validar_ogg(fonte: Union[bytes, bytearray, str, Path]) -> Dict:
    """Valida a conformidade RFC 3533 de um arquivo ou buffer OGG.

    Retorna um dicionário padronizado:
    - valido (bool): True se o contêiner for íntegro e decodificável.
    - erro (str|None): Mensagem clara e amigável em caso de erro, ideal para UI.
    - avisos (list[str]): Alertas informativos não-bloqueantes.
    - codec (str|None): 'Vorbis' ou 'Opus'.
    - canais (int|None): 1 para mono, 2 para estéreo.
    - sample_rate (int|None): Taxa de amostragem em Hz.
    - duracao (float|None): Duração total em segundos somada através dos fluxos.
    - detalhes (dict): Informações técnicas forenses detalhadas.
    """
    if isinstance(fonte, (str, Path)):
        p = Path(fonte)
        if not p.is_file():
            return {
                "valido": False,
                "erro": f"Arquivo não encontrado: {p.name}",
                "avisos": [],
                "codec": None,
                "canais": None,
                "sample_rate": None,
                "duracao": None,
                "detalhes": {"falha": "ARQUIVO_INEXISTENTE", "caminho": str(p)}
            }
        with open(p, "rb") as f:
            dados = f.read()
    elif isinstance(fonte, (bytes, bytearray, memoryview)):
        dados = bytes(fonte)
    else:
        return {
            "valido": False,
            "erro": "Tipo de entrada inválido para validação de áudio.",
            "avisos": [],
            "codec": None,
            "canais": None,
            "sample_rate": None,
            "duracao": None,
            "detalhes": {"falha": "TIPO_INVALIDO", "tipo": str(type(fonte))}
        }

    total_bytes = len(dados)
    avisos: List[str] = []

    if total_bytes < 27:
        return {
            "valido": False,
            "erro": "Arquivo OGG corrompido: gravação incompleta ou truncada prematuramente.",
            "avisos": [],
            "codec": None,
            "canais": None,
            "sample_rate": None,
            "duracao": None,
            "detalhes": {"falha": "TAMANHO_INSUFICIENTE", "tamanho_bytes": total_bytes}
        }

    pos = 0
    page_count = 0
    streams_concluidos: List[dict] = []

    # Estado do stream atual
    current_serial: Optional[int] = None
    current_stream_ended: bool = True
    current_seq: int = -1
    current_granule: int = 0
    current_codec: Optional[str] = None
    current_channels: Optional[int] = None
    current_rate: Optional[int] = None
    current_opus_preskip: int = 0

    codec_global: Optional[str] = None
    canais_global: Optional[int] = None
    sample_rate_global: Optional[int] = None

    def _calcular_duracao_prefixo_valido() -> float:
        if current_codec == "Vorbis" and current_rate and current_rate > 0:
            return round(max(0.0, current_granule / float(current_rate)), 3)
        elif current_codec == "Opus":
            amostras = max(0, current_granule - current_opus_preskip)
            return round(max(0.0, amostras / 48000.0), 3)
        return 0.0

    while pos < total_bytes:
        # 1. Validação de tamanho mínimo restante para o cabeçalho OGG (27 bytes)
        if pos + 27 > total_bytes:
            if current_stream_ended and page_count > 0:
                bytes_extras = total_bytes - pos
                return {
                    "valido": False,
                    "erro": "Arquivo OGG contém dados residuais ou corrompidos após o encerramento do áudio.",
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "LIXO_RESIDUAL",
                        "offset": pos,
                        "bytes_extras": bytes_extras,
                        "total_paginas": page_count
                    }
                }
            return {
                "valido": False,
                "erro": "Arquivo OGG corrompido: gravação incompleta ou truncada prematuramente.",
                "avisos": avisos,
                "codec": codec_global,
                "canais": canais_global,
                "sample_rate": sample_rate_global,
                "duracao": None,
                "detalhes": {
                    "falha": "TRUNCAMENTO_CABECALHO",
                    "offset": pos,
                    "tamanho_bytes": total_bytes,
                    "total_paginas": page_count
                }
            }

        # 2. Verificação da assinatura mágica b'OggS'
        magic = dados[pos:pos+4]
        if magic != b"OggS":
            if current_stream_ended and page_count > 0:
                bytes_extras = total_bytes - pos
                return {
                    "valido": False,
                    "erro": "Arquivo OGG contém dados residuais ou corrompidos após o encerramento do áudio.",
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "LIXO_RESIDUAL",
                        "offset": pos,
                        "bytes_extras": bytes_extras,
                        "total_paginas": page_count
                    }
                }
            prox_oggs = dados.find(b"OggS", pos)
            gap_bytes = (prox_oggs - pos) if prox_oggs != -1 else (total_bytes - pos)
            return {
                "valido": False,
                "erro": "Gravação OGG com falha de encadeamento ou dados corrompidos entre blocos de áudio.",
                "avisos": avisos,
                "codec": codec_global,
                "canais": canais_global,
                "sample_rate": sample_rate_global,
                "duracao": None,
                "detalhes": {
                    "falha": "QUEBRA_ENCADEAMENTO",
                    "offset": pos,
                    "gap_bytes": gap_bytes,
                    "proximo_oggs": prox_oggs,
                    "total_paginas": page_count,
                    "duracao_prefixo_valido": _calcular_duracao_prefixo_valido(),
                }
            }

        # 3. Leitura dos campos do cabeçalho
        version, header_type, granule, serial, seq, stored_crc, num_segments = struct.unpack(
            "<BBQIIIB", dados[pos+4:pos+27]
        )

        if version != 0:
            return {
                "valido": False,
                "erro": "Versão de contêiner OGG não suportada (apenas a versão 0 da RFC 3533 é válida).",
                "avisos": avisos,
                "codec": codec_global,
                "canais": canais_global,
                "sample_rate": sample_rate_global,
                "duracao": None,
                "detalhes": {"falha": "VERSAO_INVALIDA", "offset": pos, "versao": version}
            }

        # 4. Cálculo do lacing e comprimento total da página
        if pos + 27 + num_segments > total_bytes:
            return {
                "valido": False,
                "erro": "Arquivo OGG corrompido: tabela de lacing corrompida ou truncada.",
                "avisos": avisos,
                "codec": codec_global,
                "canais": canais_global,
                "sample_rate": sample_rate_global,
                "duracao": None,
                "detalhes": {
                    "falha": "TRUNCAMENTO_LACING",
                    "offset": pos,
                    "num_segments": num_segments,
                    "duracao_prefixo_valido": _calcular_duracao_prefixo_valido(),
                }
            }

        seg_table = dados[pos+27 : pos+27+num_segments]
        body_len = sum(seg_table)
        page_len = 27 + num_segments + body_len

        if pos + page_len > total_bytes:
            return {
                "valido": False,
                "erro": "Arquivo OGG corrompido: pacote de áudio incompleto ou interrompido.",
                "avisos": avisos,
                "codec": codec_global,
                "canais": canais_global,
                "sample_rate": sample_rate_global,
                "duracao": None,
                "detalhes": {
                    "falha": "TRUNCAMENTO_CORPO",
                    "offset": pos,
                    "pagina_tamanho": page_len,
                    "tamanho_bytes": total_bytes,
                    "duracao_prefixo_valido": _calcular_duracao_prefixo_valido(),
                }
            }

        # 5. Validação do Checksum CRC-32 (RFC 3533)
        page_data = dados[pos : pos+page_len]
        calc_crc = calcular_ogg_crc(page_data)
        if calc_crc != stored_crc:
            duracao_prefixo = 0.0
            if current_codec == "Vorbis" and current_rate and current_rate > 0:
                duracao_prefixo = round(max(0.0, current_granule / float(current_rate)), 3)
            elif current_codec == "Opus":
                amostras = max(0, current_granule - current_opus_preskip)
                duracao_prefixo = round(max(0.0, amostras / 48000.0), 3)

            return {
                "valido": False,
                "erro": "Gravação OGG corrompida: checksum de integridade inválido. O arquivo sofreu perda de pacotes ou interrupção durante a gravação.",
                "avisos": avisos,
                "codec": codec_global or current_codec,
                "canais": canais_global or current_channels,
                "sample_rate": sample_rate_global or current_rate,
                "duracao": None,
                "detalhes": {
                    "falha": "CRC_INVALIDO",
                    "pagina": page_count,
                    "offset": pos,
                    "stored_crc": hex(stored_crc),
                    "calc_crc": hex(calc_crc),
                    "duracao_prefixo_valido": duracao_prefixo,
                }
            }

        is_bos = bool(header_type & 0x02)
        is_eos = bool(header_type & 0x04)

        # 6. Gerenciamento de Streams: Início de novo stream (BOS) vs Stream em andamento
        if current_stream_ended:
            # Deve ser o início de um novo stream (primeiro stream ou stream encadeado imediatamente após EOS)
            if not is_bos:
                return {
                    "valido": False,
                    "erro": (
                        "Arquivo OGG inválido: fluxo de áudio sem o cabeçalho obrigatório de início (BOS) "
                        "ou com dados intercalados após encerramento."
                    ),
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {"falha": "BOS_AUSENTE", "offset": pos, "pagina": page_count, "serial": serial}
                }

            # RFC 3533: toda primeira página (BOS) de um fluxo lógico DEVE iniciar com seq = 0
            if seq != 0:
                return {
                    "valido": False,
                    "erro": "Gravação OGG inválida: número de sequência da primeira página do fluxo (BOS) deve ser zero.",
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "BOS_SEQ_NAO_ZERO",
                        "offset": pos,
                        "pagina": page_count,
                        "serial": serial,
                        "seq_encontrada": seq
                    }
                }

            # Em fluxos encadeados (chaining), o novo fluxo deve ter um número serial distinto
            if streams_concluidos and any(s["serial"] == serial for s in streams_concluidos):
                return {
                    "valido": False,
                    "erro": (
                        "Gravação OGG encadeada inválida: o novo fluxo de áudio reutiliza o número serial de um fluxo anterior. "
                        "Por favor, reexporte o arquivo com identificadores únicos de stream."
                    ),
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "SERIAL_DUPLICADO_CHAIN",
                        "offset": pos,
                        "pagina": page_count,
                        "serial": serial
                    }
                }

            current_serial = serial
            current_stream_ended = False
            current_seq = 0
            current_granule = granule if granule != 0xFFFFFFFFFFFFFFFF else 0
            current_codec = None
            current_channels = None
            current_rate = None
            current_opus_preskip = 0

            # Inspeção do cabeçalho de identificação (Vorbis ou Opus)
            if num_segments > 0 and body_len > 0:
                primeiro_pacote = dados[pos+27+num_segments : pos+27+num_segments+min(body_len, 64)]
                if primeiro_pacote.startswith(b"\x01vorbis"):
                    current_codec = "Vorbis"
                    current_channels, current_rate = _extrair_info_vorbis(primeiro_pacote)
                elif primeiro_pacote.startswith(b"OpusHead"):
                    current_codec = "Opus"
                    current_channels, current_rate, current_opus_preskip = _extrair_info_opus(primeiro_pacote)
                else:
                    return {
                        "valido": False,
                        "erro": "Codec de áudio OGG não suportado pelo Gemini. Utilize gravações em formato Vorbis ou Opus.",
                        "avisos": avisos,
                        "codec": "Desconhecido",
                        "canais": None,
                        "sample_rate": None,
                        "duracao": None,
                        "detalhes": {"falha": "CODEC_NAO_SUPORTADO", "offset": pos}
                    }

            if codec_global is None:
                codec_global = current_codec
                canais_global = current_channels
                sample_rate_global = current_rate
        else:
            # Página pertencente ao stream em andamento
            if is_bos:
                # BOS no meio de um stream ativo sem que o anterior tenha finalizado com EOS
                return {
                    "valido": False,
                    "erro": (
                        "Áudio OGG com fluxos sobrepostos ou múltiplos canais concorrentes não é suportado pelo Gemini. "
                        "Por favor, exporte a gravação em canal único (mono/estéreo simples)."
                    ),
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "MULTIPLEXACAO_BOS_INTERNO",
                        "offset": pos,
                        "pagina": page_count,
                        "serial_ativo": current_serial,
                        "serial_novo": serial
                    }
                }

            if serial != current_serial:
                # Multiplexação concorrente: intercalação de seriais simultâneos antes do término do anterior
                return {
                    "valido": False,
                    "erro": (
                        "Áudio OGG multiplexado com múltiplos canais concorrentes não é suportado pelo Gemini. "
                        "Por favor, exporte a gravação em canal único (mono/estéreo simples)."
                    ),
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "MULTIPLEXACAO_CONCORRENTE",
                        "offset": pos,
                        "pagina": page_count,
                        "serial_esperado": current_serial,
                        "serial_encontrado": serial,
                        "duracao_prefixo_valido": _calcular_duracao_prefixo_valido(),
                    }
                }

            # Verificação de contiguidade de sequência de páginas no mesmo stream
            if seq != current_seq + 1:
                return {
                    "valido": False,
                    "erro": "Gravação OGG com perda de pacotes ou interrupção na sequência de áudio.",
                    "avisos": avisos,
                    "codec": codec_global,
                    "canais": canais_global,
                    "sample_rate": sample_rate_global,
                    "duracao": None,
                    "detalhes": {
                        "falha": "QUEBRA_SEQUENCIA",
                        "offset": pos,
                        "pagina": page_count,
                        "seq_esperada": current_seq + 1,
                        "seq_encontrada": seq,
                        "duracao_prefixo_valido": _calcular_duracao_prefixo_valido(),
                    }
                }
            current_seq = seq
            if granule != 0xFFFFFFFFFFFFFFFF:
                current_granule = granule

        # Verificação de encerramento do stream atual (EOS)
        if is_eos:
            current_stream_ended = True
            duracao_stream = 0.0
            if current_codec == "Vorbis" and current_rate and current_rate > 0:
                duracao_stream = max(0.0, current_granule / float(current_rate))
            elif current_codec == "Opus":
                amostras = max(0, current_granule - current_opus_preskip)
                duracao_stream = amostras / 48000.0

            streams_concluidos.append({
                "serial": current_serial,
                "codec": current_codec,
                "canais": current_channels,
                "sample_rate": current_rate,
                "granule_final": current_granule,
                "duracao": duracao_stream
            })

        pos += page_len
        page_count += 1

    # 7. Verificação de lixo ou bytes extras após a última página
    if pos < total_bytes:
        bytes_extras = total_bytes - pos
        return {
            "valido": False,
            "erro": "Arquivo OGG contém dados residuais ou corrompidos após o encerramento do áudio.",
            "avisos": avisos,
            "codec": codec_global,
            "canais": canais_global,
            "sample_rate": sample_rate_global,
            "duracao": None,
            "detalhes": {
                "falha": "LIXO_RESIDUAL",
                "offset": pos,
                "bytes_extras": bytes_extras,
                "duracao_prefixo_valido": _calcular_duracao_prefixo_valido(),
            }
        }

    # 8. Validação caso o último stream não tenha finalizado formalmente com EOS
    if not current_stream_ended:
        avisos.append("Fluxo OGG finalizou sem a flag EOS (End of Stream) na última página.")
        duracao_stream = 0.0
        if current_codec == "Vorbis" and current_rate and current_rate > 0:
            duracao_stream = max(0.0, current_granule / float(current_rate))
        elif current_codec == "Opus":
            amostras = max(0, current_granule - current_opus_preskip)
            duracao_stream = amostras / 48000.0

        streams_concluidos.append({
            "serial": current_serial,
            "codec": current_codec,
            "canais": current_channels,
            "sample_rate": current_rate,
            "granule_final": current_granule,
            "duracao": duracao_stream
        })

    # 9. Cálculo de duração total somando os fluxos legítimos encadeados
    duracao_total: Optional[float] = None
    if streams_concluidos:
        duracao_total = round(sum(s["duracao"] for s in streams_concluidos), 2)

    return {
        "valido": True,
        "erro": None,
        "avisos": avisos,
        "codec": codec_global,
        "canais": canais_global,
        "sample_rate": sample_rate_global,
        "duracao": duracao_total,
        "detalhes": {
            "tamanho_bytes": total_bytes,
            "total_paginas": page_count,
            "total_streams": len(streams_concluidos),
            "seriais": [s["serial"] for s in streams_concluidos]
        }
    }
