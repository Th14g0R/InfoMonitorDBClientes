"""Módulo helper para decodificação tolerante de contêineres OGG via VLC isolado (RFC 3533 / Asterisk PBX).

Implementa todas as diretrizes de segurança da checklist homologada (V2-V8):
- V2: Flags de isolamento estrito sem interface, sem rede, sem metadados e sem persistência (--ignore-config, --no-media-library, etc.).
- V3: Ambiente sanitizado mínimo (strip de variáveis VLC_* e credenciais/segredos; APPDATA/USERPROFILE redirecionados para temp; SystemRoot preservado).
- V4: MRL de arquivo de entrada via tempinput.ogg.as_uri(), access=file e demux=ogg.
- V5: Transcode sout para PCM16 mono 16000 com limite rígido stop-time 7201 e arquivo fixo.
- V6: Validação estrita do arquivo WAV (RIFF/WAVE, PCM format 1, 1 canal, 16000 Hz, 16-bit, sem overflow 0xFFFFFFFF, amostras não-nulas, <= 7200s).
- V7: Gerenciamento de ciclo de vida com subprocess.Popen, kill e wait em caso de timeout antes do cleanup, CREATE_NO_WINDOW e controle de concorrência por semáforo.
- V8: Integração fechada: ausência de binário falha fechado orientando reexportação, sem tentativas de download/rede.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Tuple

# Constantes de segurança e limites de áudio
LIMIT_DURATION_SECONDS: float = 7200.0  # Máximo de 2 horas (7200s)
LIMIT_PCM_SIZE_BYTES: int = 240 * 1024 * 1024  # 240 MiB (cap de segurança para WAV/PCM gerado)
VLC_STOP_TIME_SECONDS: int = 7201  # Limite máximo de transcode no VLC

# Concorrência: 1 processo VLC por vez
_VLC_LOCK = threading.Semaphore(1)

# Variáveis de ambiente sensíveis a remover do processo VLC (segredos, chaves, tokens)
_ENV_SENSITIVE_PATTERNS = {
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
    "GEMINI", "OPENAI", "AWS", "AZURE", "GCP", "AUTH",
}


def obter_caminho_vlc() -> Optional[str]:
    """Retorna o caminho absoluto do executável VLC local homologado, ou None.

    Ordem de busca estrita e determinística:
    1. Variável de configuração TRANSCRITOR_VLC_EXE (se informada e apontar para arquivo executável absoluto).
    2. Caminhos padrão de instalação no Windows:
       - C:\\Program Files\\VideoLAN\\VLC\\vlc.exe
       - C:\\Program Files (x86)\\VideoLAN\\VLC\\vlc.exe
    NUNCA consulta a variável de ambiente PATH nem diretório de trabalho relativo (cwd).
    """
    env_vlc = os.environ.get("TRANSCRITOR_VLC_EXE")
    if env_vlc:
        p_env = Path(env_vlc)
        if not p_env.is_absolute():
            return None
        p_env = p_env.resolve()
        if p_env.is_file():
            return str(p_env)
        return None

    caminhos_padrao = [
        Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
        Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
    ]

    for p in caminhos_padrao:
        if p.is_file():
            return str(p.resolve())

    return None


def validar_wav_pcm(
    caminho_wav: Path,
    sample_rate_esperado: int = 16000,
    canais_esperados: int = 1,
) -> Tuple[bool, float, Optional[str]]:
    """Valida estritamente a conformidade técnica do contêiner RIFF/WAVE PCM gerado.

    Checklist V6:
    1. Assinatura mágica 'RIFF....WAVE'.
    2. Chunk 'fmt ': áudio formato 1 (PCM), 1 canal (mono), sample_rate exato, 16 bits por amostra.
    3. Chunk 'data': tamanho != 0xFFFFFFFF (sem overflow), data_size > 0 e <= tamanho real do arquivo.
    4. Amostras sonoras não-nulas (não pode ser 100% silêncio zerado).
    5. Duração calculada <= LIMIT_DURATION_SECONDS (7200s).

    Retorna:
        (valido: bool, duracao: float, erro: Optional[str])
    """
    if not caminho_wav.is_file():
        return False, 0.0, "Arquivo WAV não foi criado pelo decodificador."

    tamanho_total = caminho_wav.stat().st_size
    if tamanho_total < 44:
        return False, 0.0, f"Arquivo WAV truncado ou incompleto ({tamanho_total} bytes; mínimo 44)."

    if tamanho_total > LIMIT_PCM_SIZE_BYTES:
        return False, 0.0, f"Tamanho do arquivo WAV excede o limite de segurança ({tamanho_total} > {LIMIT_PCM_SIZE_BYTES} bytes)."

    try:
        with open(caminho_wav, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return False, 0.0, "Arquivo gerado não possui assinatura válida de contêiner RIFF/WAVE."
            if struct.unpack("<I", header[4:8])[0] != tamanho_total - 8:
                return False, 0.0, "Tamanho declarado do contêiner WAV diverge do arquivo gerado."

            fmt_encontrado = False
            data_encontrado = False
            canais = 0
            sample_rate = 0
            bits_per_sample = 0
            data_size = 0
            data_offset = 12

            offset = 12
            while offset + 8 <= tamanho_total:
                f.seek(offset)
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id = chunk_header[:4]
                chunk_len = struct.unpack("<I", chunk_header[4:8])[0]
                offset += 8
                if chunk_id == b"data" and chunk_len == 0xFFFFFFFF:
                    return False, 0.0, "Tamanho de dados inválido no WAV (overflow flag 0xFFFFFFFF detectada)."
                if offset + chunk_len > tamanho_total:
                    return False, 0.0, "Arquivo WAV contém um bloco incompleto ou com tamanho inválido."

                if chunk_id == b"fmt ":
                    fmt_encontrado = True
                    fmt_payload = f.read(min(chunk_len, 40))
                    if len(fmt_payload) < 16:
                        return False, 0.0, "Chunk 'fmt ' do WAV truncado ou incompleto."
                    audio_fmt, canais, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack(
                        "<HHIIHH", fmt_payload[:16]
                    )
                    if audio_fmt != 1:
                        return False, 0.0, f"Formato de compressão WAV não suportado ({audio_fmt}); esperado PCM puro (1)."
                    if canais != canais_esperados:
                        return False, 0.0, f"Número de canais no WAV ({canais}) diverge do esperado ({canais_esperados})."
                    if sample_rate != sample_rate_esperado:
                        return False, 0.0, f"Taxa de amostragem no WAV ({sample_rate} Hz) diverge de {sample_rate_esperado} Hz."
                    if bits_per_sample != 16:
                        return False, 0.0, f"Resolução de amostragem no WAV ({bits_per_sample} bits) diverge de 16 bits."
                    if block_align != canais * 2 or byte_rate != sample_rate * block_align:
                        return False, 0.0, "Alinhamento das amostras PCM do WAV inválido."

                elif chunk_id == b"data":
                    data_encontrado = True
                    data_size = chunk_len
                    data_offset = offset

                offset += chunk_len
                # Alinhamento de palavra de 16 bits na especificação RIFF
                if chunk_len % 2 == 1:
                    offset += 1

            if not fmt_encontrado:
                return False, 0.0, "Cabeçalho 'fmt ' ausente no contêiner WAV."
            if offset != tamanho_total:
                return False, 0.0, "Arquivo WAV contém dados residuais fora dos blocos declarados."

            if not data_encontrado:
                return False, 0.0, "Chunk de áudio 'data' ausente no contêiner WAV."

            # Detecção de overflow ou tamanho indeterminado (típico de pipes truncados)
            if data_size == 0xFFFFFFFF:
                return False, 0.0, "Tamanho de dados inválido no WAV (overflow flag 0xFFFFFFFF detectada)."

            if data_size == 0:
                return False, 0.0, "Chunk de áudio 'data' vazio (0 frames)."

            if data_offset + data_size > tamanho_total:
                return False, 0.0, (
                    f"Arquivo WAV corrompido: tamanho físico ({tamanho_total}) "
                    f"menor que os dados declarados ({data_offset + data_size})."
                )

            bytes_por_amostra = (bits_per_sample // 8) * canais
            if data_size % bytes_por_amostra:
                return False, 0.0, "Arquivo WAV contém uma amostra PCM incompleta."
            total_amostras = data_size // bytes_por_amostra
            duracao = total_amostras / float(sample_rate)

            if duracao > LIMIT_DURATION_SECONDS:
                return False, 0.0, f"Duração decodificada ({duracao:.1f}s) excede o limite máximo de 2 horas."

            # Verificação de amostras sonoras não-nulas (não pode ser silêncio zerado absoluto)
            f.seek(data_offset)
            tem_som = False
            bytes_lidos = 0
            while bytes_lidos < data_size:
                tamanho_leitura = min(65536, data_size - bytes_lidos)
                bloco = f.read(tamanho_leitura)
                if not bloco:
                    break
                bytes_lidos += len(bloco)
                if any(b != 0 for b in bloco):
                    tem_som = True
                    break

            if not tem_som:
                return False, 0.0, "Áudio decodificado consiste exclusivamente em silêncio zerado (ausência de voz)."

            return True, round(duracao, 3), None

    except Exception as e:
        return False, 0.0, f"Falha na validação técnica do arquivo WAV: {type(e).__name__}."


def _preparar_ambiente_sanitizado(temp_dir: Path) -> dict[str, str]:
    """Cria um ambiente de variáveis mínimo e isolado para execução segura do VLC (Checklist V3).

    1. Remove qualquer variável com prefixo VLC_*.
    2. Remove chaves que possam conter segredos, tokens ou senhas.
    3. Redireciona APPDATA, LOCALAPPDATA, USERPROFILE, HOME, TEMP e TMP para a pasta temporária isolada.
    4. Preserva variáveis essenciais do sistema operacional para carregamento determinístico de DLLs.
    """
    chaves_preservadas = {
        "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)", "PROGRAMDATA",
    }

    env_limpo: dict[str, str] = {}
    for k, v in os.environ.items():
        k_upper = k.upper()
        # Remove prefixo VLC_*
        if k_upper.startswith("VLC_"):
            continue
        # Remove variáveis contendo padrões de credenciais
        if any(pat in k_upper for pat in _ENV_SENSITIVE_PATTERNS):
            continue
        # Preserva chaves essenciais do sistema
        if k_upper in chaves_preservadas:
            env_limpo[k] = v

    # Isola diretórios de cache, config, logs e perfis no temp_dir
    str_temp = str(temp_dir)
    env_limpo["APPDATA"] = str_temp
    env_limpo["LOCALAPPDATA"] = str_temp
    env_limpo["USERPROFILE"] = str_temp
    env_limpo["HOME"] = str_temp
    env_limpo["TEMP"] = str_temp
    env_limpo["TMP"] = str_temp

    return env_limpo


def decodificar_com_vlc(
    vlc_bin: str,
    caminho_input: Path,
    caminho_wav_out: Path,
    sample_rate: int = 16000,
    timeout_segundos: float = 60.0,
) -> Tuple[bool, float, Optional[str]]:
    """Decodifica áudio OGG com inconsistência estrutural via VLC isolado (Checklist V2-V8).

    Argumentos:
        vlc_bin: Caminho absoluto para o binário do VLC.
        caminho_input: Caminho do arquivo OGG de entrada na pasta temporária.
        caminho_wav_out: Caminho de saída desejado para o arquivo WAV.
        sample_rate: Taxa de amostragem em Hz (padrão 16000).
        timeout_segundos: Prazo de execução (padrão 60s).

    Retorna:
        (sucesso: bool, duracao: float, erro: Optional[str])
    """
    if not Path(vlc_bin).is_file():
        return False, 0.0, "Executável VLC não encontrado no caminho homologado."

    if not caminho_input.is_file():
        return False, 0.0, "Arquivo de áudio de entrada não encontrado."

    temp_dir = caminho_input.parent
    dst_escaped = caminho_wav_out.as_posix()
    sout_arg = (
        f"#transcode{{acodec=s16l,channels=1,samplerate={sample_rate}}}:"
        f"std{{access=file,mux=wav,dst='{dst_escaped}'}}"
    )

    # Checklist V2 e V5: flags rígidas de isolamento e transcode
    cmd = [
        vlc_bin,
        "-I", "dummy",
        "--dummy-quiet",
        "--ignore-config",
        "--no-one-instance",
        "--no-one-instance-when-started-from-file",
        "--no-media-library",
        "--no-metadata-network-access",
        "--no-sub-autodetect-file",
        "--no-video",
        "--no-spu",
        "--no-osd",
        "--no-stats",
        "--no-interact",
        "--play-and-exit",
        "--stop-time", str(VLC_STOP_TIME_SECONDS),
        "--no-repeat",
        "--no-loop",
        "--no-auto-preparse",
        "--demux", "ogg",
        caminho_input.resolve().as_uri(),  # Checklist V4: input via file MRL
        "--sout", sout_arg,
        "vlc://quit",
    ]

    env_sanitizado = _preparar_ambiente_sanitizado(temp_dir)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    with _VLC_LOCK:
        proc: Optional[subprocess.Popen] = None
        try:
            # Checklist V7: Gerenciamento estrito de subprocess.Popen
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(temp_dir),
                env=env_sanitizado,
                shell=False,
                creationflags=creationflags,
            )

            try:
                _, stderr_bytes = proc.communicate(timeout=timeout_segundos)
            except (subprocess.TimeoutExpired, TimeoutError):
                # Checklist V7: mata o processo e aguarda término antes de qualquer cleanup
                proc.kill()
                proc.wait()
                return False, 0.0, f"Tempo limite excedido na decodificação VLC ({timeout_segundos:.0f}s)."

            if proc.returncode != 0:
                return False, 0.0, "Decodificador VLC não conseguiu recuperar o áudio. Solicite nova exportação da gravação."

            # Checklist V6: exit code 0 não basta; validação estrita da estrutura e payload do WAV
            valido_wav, duracao_wav, erro_wav = validar_wav_pcm(
                caminho_wav=caminho_wav_out,
                sample_rate_esperado=sample_rate,
                canais_esperados=1,
            )
            if not valido_wav:
                return False, 0.0, erro_wav or "Arquivo WAV gerado pelo VLC falhou na validação técnica de integridade."

            return True, duracao_wav, None

        except Exception as e:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            return False, 0.0, f"Erro inesperado no decodificador VLC: {type(e).__name__}."
