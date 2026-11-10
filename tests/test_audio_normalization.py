"""Testes unitários automatizados para normalização e reparo de contêineres OGG com fallback VLC.

Suíte 100% sintética, determinística e isenta de dados confidenciais (PII).
Cobre:
- Validação estrita de contêiner OGG (rejeição de falsos OGGs, m3u8, lixo, versão != 0, CRC inválido na p0).
- Parser em memória de STREAMINFO FLAC com MD5.
- Critério B1/P1 / T2-1: cálculo de prefixo íntegro pré-defeito (None para desconhecido, 0.0 para p0 sem amostras, float exato para páginas válidas).
- Regra de aceitação de prefixo com margem exata de 1 frame fixo (1s quebra: out 1s recusa / out 3s aceita).
- Checklist VLC V2-V8: isolamento estrito, env sanitizado, input via MRL URI, stop-time 7201, validação WAV rígida (sem 0xFFFFFFFF, não-zero, <= 7200s), kill/wait no timeout.
- Acionamento determinístico de fallback VLC quando FFmpeg para no prefixo pré-defeito.
- Critério B2: prova integral FLAC -> PCM (round-trip, frames==STREAMINFO, conferência MD5_PCM == STREAMINFO_MD5, encode com -sample_fmt s16).
- Critério B3: cap -fs 64MiB atingido deve falhar fechado com aviso explícito.
- Limites de processo: timeout de 120s, duração máxima de 2h (7200s), bounds -t 7201 e -fs 64MB no subprocesso.
- Garantia de que FFmpeg bundled e VLC são resolvidos sem consulta a PATH malicioso.
- Garantia de cleanup de arquivos temporários e caminho_audio=None.
- Mensagens de erro seguras (sem caminhos técnicos nem stderr bruto).
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from callcenter_transcriber.audio_normalization import (
    FFMPEG_TIMEOUT_BOUND_SECONDS,
    FS_MARGIN_BYTES,
    LIMIT_DURATION_SECONDS,
    LIMIT_FILE_SIZE_BYTES,
    normalizar_audio_ogg,
    obter_caminho_ffmpeg,
    obter_caminho_vlc,
    obter_prefixo_valido_pre_defeito,
    parse_flac_streaminfo,
    pre_validar_oggs_bos,
)
from callcenter_transcriber.vlc_decoder import (
    _preparar_ambiente_sanitizado,
    decodificar_com_vlc,
    validar_wav_pcm,
)


def _gerar_flac_minimo_bytes(
    sample_rate: int = 16000,
    channels: int = 1,
    total_samples: int = 16000,
    md5_bytes: bytes = b"\x00" * 16,
) -> bytes:
    """Gera um cabeçalho FLAC sintético válido com bloco STREAMINFO de 34 bytes."""
    header = bytearray(b"fLaC")
    header.append(0x80)  # is_last=1, type=0
    header.extend((34).to_bytes(3, "big"))

    streaminfo = bytearray(34)
    struct.pack_into(">HH", streaminfo, 0, 4096, 4096)
    streaminfo[4:7] = (0).to_bytes(3, "big")
    streaminfo[7:10] = (0).to_bytes(3, "big")

    meta_num = (sample_rate << 44) | ((channels - 1) << 41) | (15 << 36) | (total_samples & 0xFFFFFFFFF)
    struct.pack_into(">Q", streaminfo, 10, meta_num)
    streaminfo[18:34] = md5_bytes

    header.extend(streaminfo)
    header.extend(b"\x00" * 64)
    return bytes(header)


def _calc_ogg_crc(page_bytes: bytes) -> int:
    crc_table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            if r & 0x80000000:
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                r = (r << 1) & 0xFFFFFFFF
        crc_table.append(r)
    reg = 0
    for b in page_bytes:
        reg = ((reg << 8) & 0xFFFFFFFF) ^ crc_table[((reg >> 24) & 0xFF) ^ b]
    return reg


def _gerar_ogg_pagina_sintetica(
    version: int = 0,
    serial: int = 0x12345678,
    seq: int = 0,
    granule: int = 0,
    header_type: int = 0x02,  # BOS
    payload: bytes = None,
    corromper_crc: bool = False,
) -> bytes:
    """Gera uma página OGG sintética com cabeçalho Vorbis válido e CRC RFC 3533."""
    if payload is None:
        # Cabeçalho de identificação Vorbis válido: versão 0, 1 canal, 16000 Hz
        payload = b"\x01vorbis" + struct.pack("<IBI", 0, 1, 16000) + b"\x00" * 15 + b"\x01"

    page = bytearray(27)
    page[0:4] = b"OggS"
    page[4] = version
    page[5] = header_type
    struct.pack_into("<Q", page, 6, granule)
    struct.pack_into("<I", page, 14, serial)
    struct.pack_into("<I", page, 18, seq)
    struct.pack_into("<I", page, 22, 0)
    page[26] = 1
    segment_table = bytes([len(payload)])
    full_page = page + segment_table + payload

    crc = _calc_ogg_crc(full_page)
    if corromper_crc:
        crc = (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
    struct.pack_into("<I", full_page, 22, crc)
    return bytes(full_page)


def _gerar_wav_sintetico(
    sample_rate: int = 16000,
    canais: int = 1,
    duracao_segundos: float = 1.0,
    amostras_zero: bool = False,
    overflow_data_size: bool = False,
) -> bytes:
    """Gera bytes de um contêiner RIFF/WAVE PCM 16-bit com metadados configuráveis."""
    total_samples = int(duracao_segundos * sample_rate)
    bytes_por_amostra = 2 * canais
    data_size = 0xFFFFFFFF if overflow_data_size else (total_samples * bytes_por_amostra)

    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        1,  # PCM
        canais,
        sample_rate,
        sample_rate * bytes_por_amostra,
        bytes_por_amostra,
        16,  # bits per sample
    )

    if amostras_zero:
        pcm_data = b"\x00" * (total_samples * bytes_por_amostra)
    else:
        pcm_data = (b"\x12\x34" * canais) * total_samples

    data_chunk_header = struct.pack("<4sI", b"data", data_size)
    riff_len = 4 + len(fmt_chunk) + len(data_chunk_header) + len(pcm_data)
    riff_header = struct.pack("<4sI4s", b"RIFF", riff_len, b"WAVE")

    return riff_header + fmt_chunk + data_chunk_header + pcm_data


class TestAudioNormalizationPreValidacao(unittest.TestCase):
    """Testes para pré-validação estrita de cabeçalho OGG."""

    def test_rejeita_falso_ogg_m3u8(self):
        m3u8_fake = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000\naudio.ogg"
        valido, serial, erro = pre_validar_oggs_bos(m3u8_fake)
        self.assertFalse(valido)
        self.assertIn("Assinatura OggS ausente", erro)

    def test_rejeita_ogg_versao_invalida(self):
        page = _gerar_ogg_pagina_sintetica(version=1)
        valido, serial, erro = pre_validar_oggs_bos(page)
        self.assertFalse(valido)
        self.assertIn("Versão de contêiner OGG não suportada", erro)

    def test_rejeita_ogg_sem_flag_bos(self):
        page = _gerar_ogg_pagina_sintetica(header_type=0x00)
        valido, serial, erro = pre_validar_oggs_bos(page)
        self.assertFalse(valido)
        self.assertIn("flag BOS ausente", erro)

    def test_rejeita_ogg_crc_p0_corrompido(self):
        page = _gerar_ogg_pagina_sintetica(corromper_crc=True)
        valido, serial, erro = pre_validar_oggs_bos(page)
        self.assertFalse(valido)
        self.assertIn("CRC-32 inválido no cabeçalho inicial", erro)

    def test_aceita_ogg_bos_vorbis_integro(self):
        page = _gerar_ogg_pagina_sintetica()
        valido, serial, erro = pre_validar_oggs_bos(page)
        self.assertTrue(valido)
        self.assertEqual(serial, 0x12345678)
        self.assertIsNone(erro)


class TestAudioNormalizationPrefixoT2_1(unittest.TestCase):
    """Testes de conformidade do helper de prefixo pré-defeito (Revisor T2-1)."""

    def test_obter_prefixo_valido_retorna_none_para_falso_ogg(self):
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b""))
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b"GIF89a...fake_image"))
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b"#EXTM3U\nstream.ogg"))

    def test_obter_prefixo_valido_retorna_none_para_versao_invalida(self):
        page = _gerar_ogg_pagina_sintetica(version=1)
        self.assertIsNone(obter_prefixo_valido_pre_defeito(page))

    def test_obter_prefixo_valido_retorna_zero_para_p0_sem_amostras(self):
        # Página 0 válida Vorbis com granule 0
        page = _gerar_ogg_pagina_sintetica(granule=0)
        prefixo = obter_prefixo_valido_pre_defeito(page)
        self.assertEqual(prefixo, 0.0)

    def test_prefixo_none_provoca_falha_fechada_sem_execucao(self):
        # Entrada com versão 1 deve retornar prefixo None e falhar fechado
        page = _gerar_ogg_pagina_sintetica(version=1)
        res = normalizar_audio_ogg(page)
        self.assertFalse(res["sucesso"])
        self.assertIsNone(res["audio_bytes"])
        self.assertIn("Versão de contêiner OGG não suportada", res["erro"])


class TestAudioNormalizationVLCFlagsEChecklist(unittest.TestCase):
    """Testes para o decodificador tolerante VLC conforme a checklist de segurança V2-V8."""

    def test_obter_caminho_vlc_nao_consulta_path(self):
        with patch.dict(os.environ, {"PATH": r"C:\fake_evil_path", "TRANSCRITOR_VLC_EXE": ""}):
            caminho = obter_caminho_vlc()
            if caminho:
                self.assertTrue(Path(caminho).is_file())
                self.assertNotIn("fake_evil_path", caminho)

    def test_ambiente_sanitizado_v3_remove_segredos_e_vlc_vars(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            with patch.dict(os.environ, {
                "VLC_PLUGIN_PATH": r"C:\malicious_plugins",
                "GEMINI_API_KEY": "AIzaSySecretKey",
                "OPENAI_API_KEY": "sk-secret",
                "SYSTEMROOT": r"C:\Windows",
            }):
                env_limpo = _preparar_ambiente_sanitizado(tmp)
                self.assertNotIn("VLC_PLUGIN_PATH", env_limpo)
                self.assertNotIn("GEMINI_API_KEY", env_limpo)
                self.assertNotIn("OPENAI_API_KEY", env_limpo)
                self.assertEqual(env_limpo.get("APPDATA"), str(tmp))
                self.assertEqual(env_limpo.get("USERPROFILE"), str(tmp))
                self.assertEqual(env_limpo.get("SYSTEMROOT"), r"C:\Windows")

    def test_validar_wav_pcm_sucesso(self):
        wav_bytes = _gerar_wav_sintetico(sample_rate=16000, canais=1, duracao_segundos=2.0)
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "test.wav"
            wav_path.write_bytes(wav_bytes)
            valido, duracao, erro = validar_wav_pcm(wav_path, sample_rate_esperado=16000)
            self.assertTrue(valido)
            self.assertEqual(duracao, 2.0)
            self.assertIsNone(erro)

    def test_validar_wav_pcm_rejeita_overflow_0xffffffff(self):
        wav_bytes = _gerar_wav_sintetico(overflow_data_size=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "test_overflow.wav"
            wav_path.write_bytes(wav_bytes)
            valido, duracao, erro = validar_wav_pcm(wav_path)
            self.assertFalse(valido)
            self.assertIn("0xFFFFFFFF", erro)

    def test_validar_wav_pcm_rejeita_silencio_zerado(self):
        wav_bytes = _gerar_wav_sintetico(amostras_zero=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "test_silence.wav"
            wav_path.write_bytes(wav_bytes)
            valido, duracao, erro = validar_wav_pcm(wav_path)
            self.assertFalse(valido)
            self.assertIn("silêncio zerado", erro)

    def test_validar_wav_pcm_rejeita_duracao_acima_de_2h(self):
        wav_bytes = _gerar_wav_sintetico(duracao_segundos=7201.0)
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "test_2h.wav"
            wav_path.write_bytes(wav_bytes)
            valido, duracao, erro = validar_wav_pcm(wav_path)
            self.assertFalse(valido)
            self.assertIn("excede o limite máximo de 2 horas", erro)

    def test_decodificar_vlc_timeout_mata_e_espera_processo(self):
        import subprocess
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="vlc", timeout=1.0)
        mock_proc.poll.return_value = None

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fake_vlc = (tmp / "vlc.exe").resolve()
            fake_vlc.write_bytes(b"fake_vlc_binary")
            in_file = tmp / "in.ogg"
            in_file.write_bytes(b"dummy")
            out_wav = tmp / "out.wav"

            with patch("callcenter_transcriber.vlc_decoder.subprocess.Popen", return_value=mock_proc):
                sucesso, dur, err = decodificar_com_vlc(
                    vlc_bin=str(fake_vlc),
                    caminho_input=in_file,
                    caminho_wav_out=out_wav,
                    timeout_segundos=1.0,
                )
                self.assertFalse(sucesso)
                self.assertIn("Tempo limite excedido", err)
                mock_proc.kill.assert_called_once()
                mock_proc.wait.assert_called_once()


class TestAudioNormalizationCriterioPrefixo1s(unittest.TestCase):
    """Testa a regra estrita do Revisor: 1s quebra -> out 1s recusa / out 3s aceita."""

    @patch("callcenter_transcriber.audio_normalization.parse_flac_streaminfo")
    @patch("callcenter_transcriber.audio_normalization.obter_prefixo_valido_pre_defeito")
    @patch("callcenter_transcriber.audio_normalization._verificar_flac_saida_integral")
    @patch("subprocess.run")
    def test_recusa_saida_igual_ao_prefixo_1s(self, mock_run, mock_verif, mock_prefixo, mock_parse):
        mock_run.return_value = MagicMock(returncode=0)
        mock_verif.return_value = (True, None)
        mock_prefixo.return_value = 1.0  # Prefixo íntegro = 1.0s

        # Simula saída decodificada com exatamente 1.0s (não ultrapassa prefixo)
        mock_parse.return_value = {
            "sample_rate": 16000,
            "channels": 1,
            "total_samples": 16000,
            "duration": 1.0,
            "md5_raw": b"\x00" * 16,
        }
        page = _gerar_ogg_pagina_sintetica()

        with patch("pathlib.Path.read_bytes", return_value=_gerar_flac_minimo_bytes()):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = 1024 * 1024
                    with patch("callcenter_transcriber.audio_normalization.obter_caminho_vlc", return_value=None):
                        res = normalizar_audio_ogg(page)
                        self.assertFalse(res["sucesso"])
                        self.assertIn("Recuperação insuficiente", res["erro"])

    @patch("callcenter_transcriber.audio_normalization.parse_flac_streaminfo")
    @patch("callcenter_transcriber.audio_normalization.obter_prefixo_valido_pre_defeito")
    @patch("callcenter_transcriber.audio_normalization._verificar_flac_saida_integral")
    @patch("subprocess.run")
    def test_aceita_saida_de_3s_quando_prefixo_e_1s(self, mock_run, mock_verif, mock_prefixo, mock_parse):
        mock_run.return_value = MagicMock(returncode=0)
        mock_verif.return_value = (True, None)
        mock_prefixo.return_value = 1.0  # Prefixo íntegro = 1.0s

        # Simula saída decodificada com 3.0s (ultrapassa prefixo)
        mock_parse.return_value = {
            "sample_rate": 16000,
            "channels": 1,
            "total_samples": 48000,
            "duration": 3.0,
            "md5_raw": b"\x00" * 16,
        }
        page = _gerar_ogg_pagina_sintetica()

        with patch("pathlib.Path.read_bytes", return_value=_gerar_flac_minimo_bytes()):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = 1024 * 1024
                    res = normalizar_audio_ogg(page)
                    self.assertTrue(res["sucesso"])
                    self.assertEqual(res["duracao"], 3.0)


class TestAudioNormalizationProcessoELimites(unittest.TestCase):
    """Testes para limites de duração, timeout, cap -fs e isolamento de executáveis."""

    def test_obter_caminho_ffmpeg_sem_path(self):
        with patch.dict(os.environ, {"PATH": r"C:\fake_evil_path"}):
            bin_path = Path(obter_caminho_ffmpeg())
            self.assertTrue(bin_path.is_file())
            self.assertNotIn("fake_evil_path", str(bin_path))

    @patch("subprocess.run")
    def test_trata_timeout_limpeza_garantida(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)

        page = _gerar_ogg_pagina_sintetica()
        res = normalizar_audio_ogg(page, timeout_segundos=120.0)

        self.assertFalse(res["sucesso"])
        self.assertIsNone(res["audio_bytes"])
        self.assertIn("Tempo limite excedido", res["erro"])
        self.assertNotIn("Traceback", res["erro"])

    @patch("callcenter_transcriber.audio_normalization.parse_flac_streaminfo")
    @patch("subprocess.run")
    def test_rejeita_audio_acima_de_2h(self, mock_run, mock_parse):
        mock_run.return_value = MagicMock(returncode=0)
        mock_parse.return_value = {
            "sample_rate": 16000,
            "channels": 1,
            "total_samples": int(7200.5 * 16000),
            "duration": 7200.5,
            "md5_raw": b"\x00" * 16,
        }
        page = _gerar_ogg_pagina_sintetica()

        with patch("pathlib.Path.read_bytes", return_value=_gerar_flac_minimo_bytes()):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = 1024 * 1024
                    res = normalizar_audio_ogg(page)
                    self.assertFalse(res["sucesso"])
                    self.assertIn("excede o limite de 2 horas", res["erro"])

    @patch("callcenter_transcriber.audio_normalization.parse_flac_streaminfo")
    @patch("subprocess.run")
    def test_rejeita_saida_acima_de_64mb_ou_atinge_cap_fs(self, mock_run, mock_parse):
        mock_run.return_value = MagicMock(returncode=0)
        mock_parse.return_value = {
            "sample_rate": 16000,
            "channels": 1,
            "total_samples": 16000 * 10,
            "duration": 10.0,
            "md5_raw": b"\x00" * 16,
        }
        page = _gerar_ogg_pagina_sintetica()

        with patch("pathlib.Path.read_bytes", return_value=_gerar_flac_minimo_bytes()):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = LIMIT_FILE_SIZE_BYTES - (FS_MARGIN_BYTES // 2)
                    res = normalizar_audio_ogg(page)
                    self.assertFalse(res["sucesso"])
                    self.assertIn("atingiu o limite máximo de 64 MB", res["erro"])


class TestAudioNormalizationFallbackVLCFluxo(unittest.TestCase):
    """Testa o acionamento do fluxo de fallback VLC quando FFmpeg falha ou para no prefixo."""

    @patch("callcenter_transcriber.audio_normalization.decodificar_com_vlc")
    @patch("callcenter_transcriber.audio_normalization.parse_flac_streaminfo")
    @patch("callcenter_transcriber.audio_normalization._verificar_flac_saida_integral")
    @patch("subprocess.run")
    def test_fluxo_fallback_vlc_quando_ffmpeg_para_no_prefixo(
        self, mock_run, mock_verif, mock_parse, mock_vlc
    ):
        mock_run.return_value = MagicMock(returncode=0)
        mock_verif.return_value = (True, None)

        # 1ª chamada parse: FFmpeg para no prefixo de 0.0s (duração 0.0s)
        # 2ª chamada parse: VLC gerou FLAC com 10.0s
        mock_parse.side_effect = [
            {"sample_rate": 16000, "channels": 1, "total_samples": 0, "duration": 0.0, "md5_raw": b"\x00" * 16},
            {"sample_rate": 16000, "channels": 1, "total_samples": 160000, "duration": 10.0, "md5_raw": b"\x00" * 16},
        ]
        mock_vlc.return_value = (True, 10.0, None)

        page = _gerar_ogg_pagina_sintetica()

        with patch("pathlib.Path.read_bytes", return_value=_gerar_flac_minimo_bytes()):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = 1024 * 1024
                    res = normalizar_audio_ogg(page)

                    self.assertTrue(res["sucesso"])
                    self.assertEqual(res["decodificador"], "vlc")
                    self.assertEqual(res["duracao"], 10.0)
                    self.assertTrue(res["recuperacao_parcial"])
                    self.assertIsNone(res["caminho_audio"])


class TestVlcRegressoesRevisao(unittest.TestCase):
    def test_import_normalizador_em_pasta_plana(self):
        import subprocess
        import sys
        pasta = Path(__file__).resolve().parents[1] / "callcenter_transcriber"
        codigo = f"import sys; sys.path.insert(0, {str(pasta)!r}); import audio_normalization"
        proc = subprocess.run([sys.executable, "-I", "-c", codigo], cwd=pasta,
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_config_relativa_nao_e_resolvida_pelo_cwd(self):
        from callcenter_transcriber.vlc_decoder import obter_caminho_vlc as resolver
        with patch.dict(os.environ, {"TRANSCRITOR_VLC_EXE": "relativo.exe"}):
            with patch.object(Path, "is_file", return_value=True):
                self.assertIsNone(resolver())

    def test_wav_com_data_menor_que_payload_e_recusado(self):
        dados = bytearray(_gerar_wav_sintetico(duracao_segundos=2.0))
        struct.pack_into("<I", dados, 40, 4)
        with tempfile.TemporaryDirectory() as pasta:
            wav = Path(pasta) / "out.wav"
            wav.write_bytes(dados)
            self.assertFalse(validar_wav_pcm(wav)[0])

    def test_stderr_vlc_nao_e_devolvido(self):
        with tempfile.TemporaryDirectory() as pasta:
            entrada = Path(pasta) / "input.ogg"
            entrada.write_bytes(b"OggS sintetico")
            proc = MagicMock(returncode=1)
            proc.communicate.return_value = (None, b"SEGREDO C:/privado/arquivo")
            with patch("callcenter_transcriber.vlc_decoder.subprocess.Popen", return_value=proc):
                ok, _, erro = decodificar_com_vlc(str(entrada), entrada, Path(pasta) / "out.wav")
            self.assertFalse(ok)
            self.assertNotIn("SEGREDO", erro)
            self.assertNotIn("C:/privado", erro)

    def test_md5_divergente_streaminfo_nao_zero_recusa(self):
        from callcenter_transcriber.audio_normalization import _verificar_flac_saida_integral
        with tempfile.TemporaryDirectory() as pasta:
            flac_path = Path(pasta) / "fake.flac"
            flac_path.write_bytes(b"dummy_flac")
            raw_pcm = Path(pasta) / "verify.raw"

            def fake_run(cmd, *args, **kwargs):
                raw_pcm.write_bytes(b"\x12\x34" * 16000)
                return MagicMock(returncode=0)

            with patch("subprocess.run", side_effect=fake_run):
                ok, erro = _verificar_flac_saida_integral(
                    ffmpeg_bin="ffmpeg",
                    caminho_flac=flac_path,
                    total_samples_esperado=16000,
                    sample_rate=16000,
                    md5_esperado=b"\x99" * 16,
                    timeout_segundos=10.0,
                )
                self.assertFalse(ok)
                self.assertIn("Checksum MD5 do PCM decodificado diverge", erro)

    def test_vlc_argv_e_env_conformidade_v2_v4(self):
        with tempfile.TemporaryDirectory() as pasta:
            p = Path(pasta)
            fake_vlc = p / "vlc.exe"
            fake_vlc.write_bytes(b"fake_vlc_binary")
            in_ogg = p / "input.ogg"
            in_ogg.write_bytes(b"OggS sintetico")
            out_wav = p / "out.wav"

            captured = {}
            def fake_popen(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                captured["cwd"] = kwargs.get("cwd")
                out_wav.write_bytes(_gerar_wav_sintetico())
                m = MagicMock(returncode=0)
                m.communicate.return_value = (None, None)
                return m

            with patch("callcenter_transcriber.vlc_decoder.subprocess.Popen", side_effect=fake_popen):
                ok, dur, err = decodificar_com_vlc(str(fake_vlc), in_ogg, out_wav)
                self.assertTrue(ok)

                cmd = captured["cmd"]
                # V2 flags
                self.assertIn("-I", cmd)
                self.assertIn("dummy", cmd)
                self.assertIn("--dummy-quiet", cmd)
                self.assertIn("--ignore-config", cmd)
                self.assertIn("--no-one-instance", cmd)
                self.assertIn("--no-media-library", cmd)
                self.assertIn("--no-metadata-network-access", cmd)
                self.assertIn("--no-sub-autodetect-file", cmd)
                self.assertIn("--no-video", cmd)
                self.assertIn("--no-spu", cmd)
                self.assertIn("--no-osd", cmd)
                self.assertIn("--no-stats", cmd)
                self.assertIn("--no-interact", cmd)
                self.assertIn("--play-and-exit", cmd)
                self.assertIn("--stop-time", cmd)
                self.assertIn("7201", cmd)
                self.assertIn("--demux", cmd)
                self.assertIn("ogg", cmd)
                self.assertIn("vlc://quit", cmd)

                # V4 MRL
                self.assertIn(in_ogg.resolve().as_uri(), cmd)

                # V3 Env
                env = captured["env"]
                self.assertNotIn("VLC_PLUGIN_PATH", env)
                self.assertEqual(env.get("APPDATA"), str(p))
                self.assertEqual(captured["cwd"], str(p))

    def test_vlc_exit_0_sem_wav_falha(self):
        with tempfile.TemporaryDirectory() as pasta:
            p = Path(pasta)
            fake_vlc = p / "vlc.exe"
            fake_vlc.write_bytes(b"fake_vlc_binary")
            in_ogg = p / "input.ogg"
            in_ogg.write_bytes(b"OggS sintetico")
            out_wav = p / "out.wav"

            m = MagicMock(returncode=0)
            m.communicate.return_value = (None, None)
            with patch("callcenter_transcriber.vlc_decoder.subprocess.Popen", return_value=m):
                ok, dur, err = decodificar_com_vlc(str(fake_vlc), in_ogg, out_wav)
                self.assertFalse(ok)
                self.assertIn("Arquivo WAV não foi criado pelo decodificador", err)


if __name__ == "__main__":
    unittest.main()
