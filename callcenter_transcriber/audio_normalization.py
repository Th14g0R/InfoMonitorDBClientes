"""Módulo de normalização de áudio OGG com falhas estruturais via FFmpeg e fallback VLC homologados.

Arquitetura:
1. Decodificador primário: FFmpeg homologado bundled (imageio-ffmpeg) sobre bytes originais.
2. Fallback secundário: VLC local homologado (TRANSCRITOR_VLC_EXE ou C:/Program Files/VideoLAN/VLC/vlc.exe),
   acionado estritamente quando o decodificador primário falhar ou não ultrapassar o trecho íntegro
   pré-defeito (típico de fluxos multiplexados sem BOS de centrais Asterisk PBX).
3. Codificador e auditor de prova: FFmpeg converte para FLAC nativo (-sample_fmt s16) e executa
   decodificação de prova integral (round-trip PCM s16le), conferindo contagem de amostras e STREAMINFO MD5.
4. Segurança estrita: sem shell, sem consulta ao PATH/cwd/env, instâncias isoladas, bounds -t 7201 e -fs 64MB/240MB,
   cleanup garantido em bloco finally e caminho_audio=None.
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

try:
    from callcenter_transcriber.vlc_decoder import decodificar_com_vlc, obter_caminho_vlc
except ImportError:
    from vlc_decoder import decodificar_com_vlc, obter_caminho_vlc

# Tentativa de importação do helper oficial exportado por ogg_validation
try:
    from callcenter_transcriber.ogg_validation import (
        obter_prefixo_valido_pre_defeito as _prefixo_externo,
    )
except ImportError:
    try:
        from ogg_validation import (
            obter_prefixo_valido_pre_defeito as _prefixo_externo,
        )
    except ImportError:
        _prefixo_externo = None

# Limites estritos de segurança e integridade
LIMIT_DURATION_SECONDS: float = 7200.0  # Máximo de 2 horas exatas (7200s, excesso recusado)
LIMIT_FILE_SIZE_BYTES: int = 64 * 1024 * 1024  # 64 MiB (tamanho máximo do FLAC)
LIMIT_PCM_SIZE_BYTES: int = 240 * 1024 * 1024  # 240 MiB (cap de segurança para PCM temporário em disco)
FFMPEG_TIMEOUT_BOUND_SECONDS: int = 7201  # Limite rígido de decodificação no demuxer (-t)
FS_MARGIN_BYTES: int = 16 * 1024  # Margem de segurança para detectar truncamento pelo limite -fs

# Controle de concorrência: 1 processo de conversão por vez
_CONVERSION_LOCK = threading.Semaphore(1)

# Tabela estática de CRC-32 (RFC 3533 - polinômio 0x04C11DB7) para validação
_OGG_CRC_TABLE: list[int] = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        if _r & 0x80000000:
            _r = ((_r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            _r = (_r << 1) & 0xFFFFFFFF
    _OGG_CRC_TABLE.append(_r)


def _calc_page_crc(page_bytes: bytes | bytearray) -> int:
    """Calcula o checksum CRC-32 RFC 3533 sobre os bytes de uma página OGG."""
    reg = 0
    for b in page_bytes:
        reg = ((reg << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((reg >> 24) & 0xFF) ^ b]
    return reg


def obter_caminho_ffmpeg() -> str:
    """Retorna o caminho absoluto do executável FFmpeg bundled homologado.

    Resolve diretamente na pasta 'binaries/' do pacote imageio_ffmpeg instalado,
    validando estritamente que o caminho resolvido é filho do diretório do pacote.
    NUNCA consulta PATH, NUNCA consulta cwd e NUNCA executa binários de fontes externas.

    Raises:
        RuntimeError: Se o binário bundled não for localizado ou não for executável.
    """
    try:
        import imageio_ffmpeg
        pkg_dir = Path(imageio_ffmpeg.__file__).resolve().parent
        bin_dir = pkg_dir / "binaries"

        if not bin_dir.is_dir():
            raise RuntimeError(f"Diretório de binários bundled ausente em {pkg_dir}.")

        candidatos = [
            f for f in bin_dir.iterdir()
            if f.is_file() and f.name.startswith("ffmpeg-") and not f.name.endswith(".py")
        ]
        if not candidatos:
            raise RuntimeError("Nenhum executável FFmpeg bundled encontrado em imageio_ffmpeg/binaries.")

        bin_path = candidatos[0].resolve()

        if not bin_path.is_relative_to(pkg_dir):
            raise RuntimeError("Caminho do binário resolvido escapa do diretório do pacote homologado.")

        if not bin_path.is_file():
            raise RuntimeError("Arquivo do binário bundled não é válido.")

        return str(bin_path)
    except Exception as e:
        raise RuntimeError("Binário homologado do FFmpeg (bundled) não está disponível.") from e


def pre_validar_oggs_bos(dados: bytes) -> Tuple[bool, Optional[int], Optional[str]]:
    """Verifica se os dados iniciam com uma página OGG válida com cabeçalho BOS.

    Garante que:
    1. A assinatura 'OggS' está presente no offset 0.
    2. A versão de estrutura do contêiner é exatamente 0 (dados[4] == 0).
    3. A flag BOS (Beginning of Stream, bit 0x02) está ativa na página 0.
    4. O CRC-32 da página 0 é íntegro conforme a RFC 3533.
    5. O payload da página 0 contém Vorbis (\\x01vorbis) ou Opus (OpusHead).

    Retorna:
        (valido: bool, serial_p0: Optional[int], mensagem_erro: Optional[str])
    """
    if not dados:
        return False, None, "Arquivo vazio: nenhum dado recebido."

    if len(dados) < 4 or dados[:4] != b"OggS":
        return False, None, "Assinatura OggS ausente no início do arquivo (falso OGG ou formato inválido)."

    if len(dados) < 28:
        return False, None, "Tamanho insuficiente para cabeçalho OGG (mínimo 28 bytes)."

    if dados[4] != 0:
        return False, None, f"Versão de contêiner OGG não suportada ({dados[4]}); esperado 0."

    header_type = dados[5]
    if not (header_type & 0x02):
        return False, None, "A primeira página do contêiner não indica início de fluxo (flag BOS ausente)."

    serial_p0 = struct.unpack("<I", dados[14:18])[0]
    num_segments = dados[26]

    tamanho_cabecalho = 27 + num_segments
    if len(dados) < tamanho_cabecalho:
        return False, None, "Cabeçalho da primeira página truncado."

    segment_table = dados[27:tamanho_cabecalho]
    body_len = sum(segment_table)
    tamanho_total_p0 = tamanho_cabecalho + body_len

    if len(dados) < tamanho_total_p0:
        return False, None, "Dados da primeira página OGG truncados."

    pagina_p0 = bytearray(dados[:tamanho_total_p0])
    crc_registrado = struct.unpack("<I", pagina_p0[22:26])[0]

    struct.pack_into("<I", pagina_p0, 22, 0)
    crc_calculado = _calc_page_crc(pagina_p0)

    if crc_calculado != crc_registrado:
        return False, None, "CRC-32 inválido no cabeçalho inicial (página 0 corrompida)."

    corpo_p0 = dados[tamanho_cabecalho:tamanho_total_p0]
    if not (corpo_p0.startswith(b"\x01vorbis") or corpo_p0.startswith(b"OpusHead")):
        return False, None, "Codec não suportado: a página inicial não contém identificador Vorbis ou Opus."

    return True, serial_p0, None


def _calcular_prefixo_valido_local(dados: bytes) -> Optional[float]:
    """Calcula a duração do último trecho íntegro antes de qualquer defeito estrutural.

    Retorna:
        - None: quando não é possível determinar o prefixo (arquivo inválido, não-OGG, versão!=0,
          página 0 corrompida, truncada ou codec desconhecido).
        - 0.0: quando a página 0 é um cabeçalho legítimo e válido, mas o defeito ocorre antes
          de qualquer frame sonoro decodificável (granule 0).
        - float > 0.0: duração em segundos da última página com CRC, versão, sequência e lacing válidos.
    """
    if not dados or len(dados) < 27 or dados[:4] != b"OggS":
        return None

    # Validação da página 0
    ver, htype, gran, ser, seq, stored_crc, n_seg = struct.unpack("<BBQIIIB", dados[4:27])
    if ver != 0 or not (htype & 0x02) or seq != 0:
        return None
    if 27 + n_seg > len(dados):
        return None

    seg_table = dados[27 : 27 + n_seg]
    body_len = sum(seg_table)
    p0_len = 27 + n_seg + body_len
    if p0_len > len(dados):
        return None

    p0_copy = bytearray(dados[:p0_len])
    struct.pack_into("<I", p0_copy, 22, 0)
    if _calc_page_crc(p0_copy) != stored_crc:
        return None

    corpo_p0 = dados[27 + n_seg : p0_len]
    codec = None
    sample_rate = 0
    opus_preskip = 0

    if len(corpo_p0) >= 16 and corpo_p0.startswith(b"\x01vorbis"):
        codec = "Vorbis"
        vorbis_ver = struct.unpack("<I", corpo_p0[7:11])[0]
        if vorbis_ver != 0:
            return None
        sample_rate = struct.unpack("<I", corpo_p0[12:16])[0]
        if sample_rate <= 0:
            return None
    elif len(corpo_p0) >= 19 and corpo_p0.startswith(b"OpusHead"):
        codec = "Opus"
        opus_preskip = struct.unpack("<H", corpo_p0[10:12])[0]
        sample_rate = 48000
    else:
        return None

    last_valid_duration = 0.0
    p0_serial = ser
    prev_seq = 0
    offset = p0_len
    limite = len(dados)

    while offset < limite:
        if offset + 27 > limite:
            break
        if dados[offset : offset + 4] != b"OggS":
            break

        ver, htype, gran, ser, seq, stored_crc, n_seg = struct.unpack("<BBQIIIB", dados[offset+4 : offset+27])
        if ver != 0 or ser != p0_serial or seq != prev_seq + 1:
            break
        if offset + 27 + n_seg > limite:
            break

        seg_table = dados[offset + 27 : offset + 27 + n_seg]
        p_len = 27 + n_seg + sum(seg_table)
        if offset + p_len > limite:
            break

        p_copy = bytearray(dados[offset : offset + p_len])
        struct.pack_into("<I", p_copy, 22, 0)
        if _calc_page_crc(p_copy) != stored_crc:
            break

        if gran != 0xFFFFFFFFFFFFFFFF and gran != -1:
            if codec == "Vorbis":
                last_valid_duration = max(0.0, gran / float(sample_rate))
            elif codec == "Opus":
                amostras = max(0, gran - opus_preskip)
                last_valid_duration = max(0.0, amostras / 48000.0)

        prev_seq = seq
        offset += p_len

    return round(last_valid_duration, 3)


def obter_prefixo_valido_pre_defeito(dados: bytes) -> Optional[float]:
    """Calcula a duração do último trecho íntegro antes de qualquer defeito estrutural.

    Consome preferencialmente o helper público homologado em ogg_validation; caso ausente,
    utiliza a implementação canônica local.

    Retorna None para entradas inválidas/não mensuráveis (fail closed), 0.0 para início sem amostras
    e float > 0.0 para áudios com prefixo parcialmente decodificável.
    """
    if callable(_prefixo_externo):
        try:
            return _prefixo_externo(dados)
        except Exception:
            pass
    return _calcular_prefixo_valido_local(dados)


def parse_flac_streaminfo(flac_bytes: bytes) -> Dict[str, Any]:
    """Decodifica os metadados do bloco obrigatório STREAMINFO de um fluxo FLAC em memória.

    Retorna dicionário contendo sample_rate, channels, bits_per_sample, total_samples, duration e md5_raw.
    """
    if len(flac_bytes) < 42 or flac_bytes[:4] != b"fLaC":
        raise ValueError("Não é um fluxo FLAC válido (assinatura fLaC ausente).")

    block_type = flac_bytes[4] & 0x7F
    if block_type != 0:
        raise ValueError("O primeiro bloco de metadados FLAC não é STREAMINFO.")

    block_len = int.from_bytes(flac_bytes[5:8], "big")
    if block_len < 34:
        raise ValueError("Bloco STREAMINFO possui tamanho inválido (< 34 bytes).")

    streaminfo = flac_bytes[8:42]
    meta_num = int.from_bytes(streaminfo[10:18], "big")

    sample_rate = meta_num >> 44
    channels = ((meta_num >> 41) & 0x07) + 1
    bits_per_sample = ((meta_num >> 36) & 0x1F) + 1
    total_samples = meta_num & 0xFFFFFFFFF

    duration = (total_samples / sample_rate) if sample_rate > 0 else 0.0
    md5_bytes = streaminfo[18:34]

    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": bits_per_sample,
        "total_samples": total_samples,
        "duration": duration,
        "md5_raw": md5_bytes,
    }


def _verificar_flac_saida_integral(
    ffmpeg_bin: str,
    caminho_flac: Path,
    total_samples_esperado: int,
    sample_rate: int,
    md5_esperado: bytes,
    timeout_segundos: float = 60.0,
) -> Tuple[bool, Optional[str]]:
    """Executa a decodificação de prova integral da saída FLAC em PCM s16le (Checklist B2).

    Garante que:
    1. O arquivo FLAC gerado decodifica sem erros (-xerror).
    2. O total de amostras decodificadas é estritamente igual a STREAMINFO total_samples > 0.
    3. O áudio contém amostras sonoras não-nulas (não é 100% silêncio zerado).
    4. O hash MD5 do PCM decodificado confere com a assinatura STREAMINFO quando presente.
    5. O processo está limitado por -t 7201 e -fs 240MB no nível do decodificador.
    """
    pcm_temp = caminho_flac.parent / "verify.raw"
    try:
        cmd = [
            ffmpeg_bin,
            "-nostdin",
            "-protocol_whitelist", "file",
            "-xerror",
            "-f", "flac",
            "-t", str(FFMPEG_TIMEOUT_BOUND_SECONDS),
            "-i", str(caminho_flac),
            "-f", "s16le",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-v", "error",
            "-y",
            "-fs", str(LIMIT_PCM_SIZE_BYTES),
            str(pcm_temp),
        ]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout_segundos,
            creationflags=creationflags,
        )

        if proc.returncode != 0:
            return False, "Decodificação de prova do áudio FLAC falhou: fluxo contém frames corrompidos."

        if not pcm_temp.is_file():
            return False, "Falha na prova de saída: arquivo PCM de verificação não foi produzido."

        tamanho_pcm = pcm_temp.stat().st_size
        if tamanho_pcm > LIMIT_PCM_SIZE_BYTES:
            return False, "Tamanho PCM decodificado excede o limite máximo permitido (240 MB)."

        total_bytes = 0
        tem_amostra_nao_zero = False
        h_md5 = hashlib.md5()

        with open(pcm_temp, "rb") as f_pcm:
            while True:
                bloco = f_pcm.read(65536)
                if not bloco:
                    break
                total_bytes += len(bloco)
                h_md5.update(bloco)
                if not tem_amostra_nao_zero and any(b != 0 for b in bloco):
                    tem_amostra_nao_zero = True

        frames_decodificados = total_bytes // 2

        if frames_decodificados == 0:
            return False, "Falha na prova de integridade: nenhum frame de áudio foi decodificado."

        if frames_decodificados != total_samples_esperado:
            return False, (
                f"Contagem de amostras PCM decodificadas ({frames_decodificados}) "
                f"diverge do cabeçalho STREAMINFO ({total_samples_esperado})."
            )

        if not tem_amostra_nao_zero:
            return False, "Áudio recuperado consiste exclusivamente em silêncio zerado (ausência de voz)."

        if md5_esperado and md5_esperado != (b"\x00" * 16):
            if h_md5.digest() != md5_esperado:
                return False, "Checksum MD5 do PCM decodificado diverge da assinatura STREAMINFO do FLAC."

        return True, None

    except subprocess.TimeoutExpired:
        return False, f"Tempo limite excedido na prova de decodificação do FLAC ({timeout_segundos:.0f}s)."
    except Exception as e:
        return False, f"Erro inesperado na prova de decodificação: {type(e).__name__}."
    finally:
        if pcm_temp.exists():
            try:
                pcm_temp.unlink()
            except OSError:
                pass


def normalizar_audio_ogg(
    fonte: Union[bytes, str, Path],
    timeout_segundos: float = 120.0,
    sample_rate: int = 16000,
) -> Dict[str, Any]:
    """Normaliza áudio OGG com falhas de contêiner para FLAC nativo (audio/flac) sem alterar bytes.

    Pipeline de recuperação homologado:
    1. Avalia o prefixo íntegro pré-defeito com fail closed estrito se indeterminado (None).
    2. Tenta decodificação nativa com FFmpeg sobre os bytes originais.
    3. Se o FFmpeg falhar ou a saída for <= prefixo íntegro pré-defeito (típico de PBX Asterisk),
       aciona o fallback tolerante do VLC local isolado (Checklist V2-V8).
    4. Converte a saída para FLAC nativo (-sample_fmt s16) e audita com round-trip PCM integral (Checklist B2).
    5. Se ambos falharem ou o VLC não estiver instalado, falha fechado (sem upload) orientando reexportação.

    Argumentos:
        fonte: Bytes do arquivo ou caminho (str/Path) do áudio original.
        timeout_segundos: Prazo máximo de execução do subprocesso (padrão 120s).
        sample_rate: Taxa de amostragem de saída em Hz (padrão 16000).

    Retorna:
        Dicionário com o contrato homologado:
        {
            'sucesso': bool,
            'audio_bytes': bytes | None,
            'caminho_audio': None,
            'mime_type': 'audio/flac',
            'duracao': float,
            'canais': int,
            'sample_rate': int,
            'tamanho_bytes': int,
            'decodificador': str,              # 'ffmpeg' ou 'vlc'
            'recuperacao_parcial': bool,
            'avisos': list[str],
            'erro': str | None
        }
    """
    resultado_padrao: Dict[str, Any] = {
        "sucesso": False,
        "audio_bytes": None,
        "caminho_audio": None,
        "mime_type": "audio/flac",
        "duracao": 0.0,
        "canais": 1,
        "sample_rate": sample_rate,
        "tamanho_bytes": 0,
        "decodificador": "nenhum",
        "recuperacao_parcial": False,
        "avisos": [],
        "erro": None,
    }

    try:
        if isinstance(fonte, (str, Path)):
            caminho_input = Path(fonte)
            if not caminho_input.is_file():
                resultado_padrao["erro"] = "Arquivo de áudio não encontrado no caminho informado."
                return resultado_padrao
            dados_brutos = caminho_input.read_bytes()
        elif isinstance(fonte, (bytes, bytearray)):
            dados_brutos = bytes(fonte)
        else:
            resultado_padrao["erro"] = "Tipo de entrada inválido: esperado bytes, str ou Path."
            return resultado_padrao
    except Exception as e:
        resultado_padrao["erro"] = f"Não foi possível ler os dados de entrada: {type(e).__name__}."
        return resultado_padrao

    if len(dados_brutos) > LIMIT_FILE_SIZE_BYTES:
        resultado_padrao["erro"] = "Arquivo excede o limite máximo de tamanho permitido (64 MB)."
        return resultado_padrao

    valido, serial_p0, msg_erro = pre_validar_oggs_bos(dados_brutos)
    if not valido or serial_p0 is None:
        resultado_padrao["erro"] = msg_erro or "Arquivo não é um contêiner OGG válido com áudio suportado."
        return resultado_padrao

    # Critério T2-1: Determina o prefixo íntegro pré-defeito. Se for None, fail closed obrigatório.
    prefixo_valido = obter_prefixo_valido_pre_defeito(dados_brutos)
    if prefixo_valido is None:
        resultado_padrao["erro"] = (
            "Não foi possível determinar o trecho íntegro do contêiner OGG "
            "(estrutura ou metadados de fluxo indeterminados). Normalização recusada por segurança."
        )
        return resultado_padrao

    try:
        ffmpeg_bin = obter_caminho_ffmpeg()
    except RuntimeError as re:
        resultado_padrao["erro"] = str(re)
        return resultado_padrao

    vlc_bin = obter_caminho_vlc()

    with _CONVERSION_LOCK:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            in_temp = tmp_path / "input.ogg"
            out_temp = tmp_path / "output.flac"
            vlc_wav_temp = tmp_path / "vlc_decoded.wav"

            try:
                # 1. Alimenta o processo estritamente com os bytes ORIGINAIS (sem reescrita de cabeçalho)
                in_temp.write_bytes(dados_brutos)

                tolerancia_1_frame = 1.0 / sample_rate
                decodificador_ativo = "ffmpeg"
                duracao_final = 0.0
                total_samples_final = 0
                md5_flac_final = b""

                # Tentativa primária: Decodificador nativo FFmpeg
                cmd_ffmpeg = [
                    ffmpeg_bin,
                    "-nostdin",
                    "-protocol_whitelist", "file",
                    "-f", "ogg",
                    "-err_detect", "ignore_err",
                    "-v", "error",
                    "-y",
                    "-t", str(FFMPEG_TIMEOUT_BOUND_SECONDS),
                    "-i", str(in_temp),
                    "-map", "0:a:0",
                    "-vn", "-sn", "-dn",
                    "-c:a", "flac",
                    "-sample_fmt", "s16",
                    "-ar", str(sample_rate),
                    "-ac", "1",
                    "-threads", "1",
                    "-map_metadata", "-1",
                    "-fs", str(LIMIT_FILE_SIZE_BYTES),
                    str(out_temp),
                ]

                creationflags = 0
                if sys.platform == "win32":
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

                proc_ffmpeg = subprocess.run(
                    cmd_ffmpeg,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=min(timeout_segundos, 60.0),
                    creationflags=creationflags,
                )

                ffmpeg_avancou = False
                if proc_ffmpeg.returncode == 0 and out_temp.is_file() and out_temp.stat().st_size > 0:
                    try:
                        meta_primaria = parse_flac_streaminfo(out_temp.read_bytes())
                        dur_primaria = meta_primaria["duration"]

                        # Rejeição imediata se exceder o limite de 2 horas
                        if dur_primaria > LIMIT_DURATION_SECONDS:
                            resultado_padrao["erro"] = f"Duração recuperada ({dur_primaria:.1f}s) excede o limite de 2 horas."
                            return resultado_padrao

                        # Critério B1: O decodificador primário precisa ultrapassar o trecho íntegro pré-defeito
                        if dur_primaria > (prefixo_valido + tolerancia_1_frame):
                            ffmpeg_avancou = True
                            duracao_final = dur_primaria
                            total_samples_final = meta_primaria["total_samples"]
                            md5_flac_final = meta_primaria.get("md5_raw", b"")
                    except Exception:
                        ffmpeg_avancou = False

                # Fallback secundário: VLC (se FFmpeg falhou ou parou no prefixo íntegro pré-defeito)
                if not ffmpeg_avancou:
                    if not vlc_bin:
                        resultado_padrao["erro"] = (
                            f"Recuperação insuficiente: decodificador primário não ultrapassou "
                            f"o trecho íntegro ({prefixo_valido:.2f}s) e decodificador tolerante VLC não está disponível "
                            "no servidor para áudio de PBX. Reexportação necessária."
                        )
                        return resultado_padrao

                    sucesso_vlc, dur_vlc, err_vlc = decodificar_com_vlc(
                        vlc_bin=vlc_bin,
                        caminho_input=in_temp,
                        caminho_wav_out=vlc_wav_temp,
                        sample_rate=sample_rate,
                        timeout_segundos=min(timeout_segundos, 60.0),
                    )

                    if not sucesso_vlc:
                        resultado_padrao["erro"] = err_vlc or "Falha no decodificador tolerante VLC."
                        return resultado_padrao

                    # Valida se VLC ultrapassou o prefixo íntegro (Critério B1 / 1 frame fixed)
                    if dur_vlc <= (prefixo_valido + tolerancia_1_frame):
                        resultado_padrao["erro"] = (
                            f"Recuperação insuficiente: decodificador tolerante ({dur_vlc:.2f}s) "
                            f"não ultrapassou o trecho íntegro ({prefixo_valido:.2f}s). Reexportação necessária."
                        )
                        return resultado_padrao

                    if dur_vlc > LIMIT_DURATION_SECONDS:
                        resultado_padrao["erro"] = f"Duração recuperada ({dur_vlc:.1f}s) excede o limite de 2 horas."
                        return resultado_padrao

                    # Codifica o WAV gerado pelo VLC para FLAC nativo via FFmpeg (Checklist V8)
                    cmd_flac_vlc = [
                        ffmpeg_bin,
                        "-nostdin",
                        "-protocol_whitelist", "file",
                        "-f", "wav",
                        "-v", "error",
                        "-y",
                        "-t", str(FFMPEG_TIMEOUT_BOUND_SECONDS),
                        "-i", str(vlc_wav_temp),
                        "-map", "0:a:0",
                        "-vn", "-sn", "-dn",
                        "-c:a", "flac",
                        "-sample_fmt", "s16",
                        "-ar", str(sample_rate),
                        "-ac", "1",
                        "-threads", "1",
                        "-map_metadata", "-1",
                        "-fs", str(LIMIT_FILE_SIZE_BYTES),
                        str(out_temp),
                    ]

                    proc_flac = subprocess.run(
                        cmd_flac_vlc,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        shell=False,
                        timeout=min(timeout_segundos, 60.0),
                        creationflags=creationflags,
                    )

                    if proc_flac.returncode != 0 or not out_temp.is_file():
                        resultado_padrao["erro"] = "Falha ao codificar saída do VLC para FLAC."
                        return resultado_padrao

                    meta_flac = parse_flac_streaminfo(out_temp.read_bytes())
                    decodificador_ativo = "vlc"
                    duracao_final = meta_flac["duration"]
                    total_samples_final = meta_flac["total_samples"]
                    md5_flac_final = meta_flac.get("md5_raw", b"")

                # Checklist B3: Detecta truncamento pelo cap -fs de 64 MB
                tamanho_gerado = out_temp.stat().st_size
                if tamanho_gerado >= (LIMIT_FILE_SIZE_BYTES - FS_MARGIN_BYTES):
                    resultado_padrao["erro"] = (
                        "Áudio recuperado atingiu o limite máximo de 64 MB (saída truncada pelo limitador de tamanho)."
                    )
                    return resultado_padrao

                audio_bytes = out_temp.read_bytes()

                # Checklist B2: Prova técnica de decodificação integral da saída FLAC (round-trip PCM + conferência MD5)
                sucesso_prova, erro_prova = _verificar_flac_saida_integral(
                    ffmpeg_bin=ffmpeg_bin,
                    caminho_flac=out_temp,
                    total_samples_esperado=total_samples_final,
                    sample_rate=sample_rate,
                    md5_esperado=md5_flac_final,
                    timeout_segundos=min(timeout_segundos, 60.0),
                )
                if not sucesso_prova:
                    resultado_padrao["erro"] = erro_prova or "Falha na prova de integridade do arquivo FLAC decodificado."
                    return resultado_padrao

                avisos = [
                    f"Áudio normalizado via decodificador tolerante local ({duracao_final:.2f}s recuperados). "
                    "Inconsistências estruturais de fluxo da central telefônica PBX foram contornadas; "
                    "verifique o diálogo transcrito para possíveis cortes."
                ]

                return {
                    "sucesso": True,
                    "audio_bytes": audio_bytes,
                    "caminho_audio": None,
                    "mime_type": "audio/flac",
                    "duracao": round(duracao_final, 3),
                    "canais": 1,
                    "sample_rate": sample_rate,
                    "tamanho_bytes": len(audio_bytes),
                    "decodificador": decodificador_ativo,
                    "recuperacao_parcial": True,
                    "avisos": avisos,
                    "erro": None,
                }

            except subprocess.TimeoutExpired:
                resultado_padrao["erro"] = f"Tempo limite excedido durante a normalização do áudio ({timeout_segundos:.0f}s)."
                return resultado_padrao
            except Exception as e:
                resultado_padrao["erro"] = f"Erro inesperado durante a normalização: {type(e).__name__}."
                return resultado_padrao
