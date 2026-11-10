"""Testes unitários automatizados para o validador de conformidade OGG (RFC 3533).

Cobre todos os critérios estruturais e de integridade com fluxos sintéticos puros (100% sem PII):
- Validação positiva de Vorbis e Opus válidos.
- Suporte a múltiplos fluxos encadeados sequenciais (Chaining) somando duração por stream.
- Oráculo independente de CRC usando Mutagen (mutagen.ogg.OggPage.write) e vetor constante RFC 3533.
- Rejeição de corrupção de CRC-32.
- Rejeição de truncamento de cabeçalho, lacing e corpo.
- Rejeição de versão != 0 (RFC 3533).
- Rejeição de quebras de encadeamento e gaps espúrios.
- Rejeição de multiplexação concorrente e BOS sobreposto.
- Validação de flag BOS obrigatória e exigência de seq == 0 no início do fluxo.
- Rejeição de reuso de número serial em streams encadeados consecutivos.
- Aviso não-bloqueante para encerramento sem flag EOS.
- Rejeição de dados residuais / lixo após EOS.
- Rejeição de saltos ou quebras no número de sequência (seq).
"""
import struct
import sys
import unittest
from pathlib import Path

# Suporte para importação dos módulos da aplicação
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "callcenter_transcriber"))

from callcenter_transcriber.ogg_validation import (
    calcular_ogg_crc,
    obter_prefixo_valido_pre_defeito,
    validar_ogg,
)


def _montar_pagina_ogg(
    serial: int = 10001,
    seq: int = 0,
    header_type: int = 0x00,
    granule: int = 0,
    version: int = 0,
    packets: list = None,
    corromper_crc: bool = False
) -> bytes:
    """Monta uma página OGG sintética válida RFC 3533 com lacing correto."""
    if packets is None:
        packets = [b"\x00" * 32]

    # Constrói tabela de lacing
    lacing = []
    body_parts = []
    for pkt in packets:
        quot, rem = divmod(len(pkt), 255)
        lacing.extend([255] * quot + [rem])
        body_parts.append(pkt)

    num_segments = len(lacing)
    lacing_bytes = bytes(lacing)
    body_bytes = b"".join(body_parts)

    head_raw = (
        struct.pack("<4sBBQIIIB", b"OggS", version, header_type, granule, serial, seq, 0, num_segments)
        + lacing_bytes
        + body_bytes
    )

    crc = calcular_ogg_crc(head_raw)
    if corromper_crc:
        crc = (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF

    return head_raw[:22] + struct.pack("<I", crc) + head_raw[26:]


def _gerar_fluxo_vorbis_sintetico(
    serial: int = 554433,
    num_paginas_dados: int = 2,
    sample_rate: int = 8000,
    amostras_por_pagina: int = 800
) -> bytes:
    """Gera um fluxo OGG Vorbis sintético válido com BOS, setup e dados com EOS."""
    # Pacote de identificação Vorbis:
    # 0..6: \x01vorbis, 7..10: version 0, 11: 1 canal (mono), 12..15: sample rate, 16..22: bitrates, etc.
    sr_bytes = struct.pack("<I", sample_rate)
    vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
    pag0 = _montar_pagina_ogg(serial=serial, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])

    paginas = [pag0]
    for i in range(1, num_paginas_dados + 1):
        is_eos = (i == num_paginas_dados)
        ht = 0x04 if is_eos else 0x00
        granule = i * amostras_por_pagina
        pag = _montar_pagina_ogg(
            serial=serial,
            seq=i,
            header_type=ht,
            granule=granule,
            packets=[b"\x12\x34\x56\x78" * 16]
        )
        paginas.append(pag)

    return b"".join(paginas)


def _gerar_fluxo_opus_sintetico(serial: int = 998877) -> bytes:
    """Gera um fluxo OGG Opus sintético válido com OpusHead e página de áudio com EOS."""
    # OpusHead (19 bytes): OpusHead (8), version 1 (1), channels 2 (1), pre_skip 312 (2), sample_rate 48000 (4), gain 0 (2), family 0 (1)
    opus_head = struct.pack("<8sBBHIhB", b"OpusHead", 1, 2, 312, 48000, 0, 0)
    pag0 = _montar_pagina_ogg(serial=serial, seq=0, header_type=0x02, granule=0, packets=[opus_head])
    pag1 = _montar_pagina_ogg(serial=serial, seq=1, header_type=0x04, granule=48000 + 312, packets=[b"\xf8" * 64])
    return pag0 + pag1


class TestOggValidation(unittest.TestCase):

    def test_valido_sintetico_vorbis(self):
        """Fluxo Vorbis sintético bem-formado deve ser aceito como válido."""
        dados = _gerar_fluxo_vorbis_sintetico(num_paginas_dados=3)
        res = validar_ogg(dados)
        self.assertTrue(res["valido"], msg=f"Falha inesperada: {res.get('erro')}")
        self.assertIsNone(res["erro"])
        self.assertEqual(res["codec"], "Vorbis")
        self.assertEqual(res["canais"], 1)
        self.assertEqual(res["sample_rate"], 8000)
        self.assertGreater(res["duracao"], 0.0)
        self.assertEqual(res["avisos"], [])

    def test_valido_sintetico_opus(self):
        """Fluxo Opus sintético bem-formado deve ser aceito como válido."""
        dados = _gerar_fluxo_opus_sintetico()
        res = validar_ogg(dados)
        self.assertTrue(res["valido"], msg=f"Falha inesperada: {res.get('erro')}")
        self.assertIsNone(res["erro"])
        self.assertEqual(res["codec"], "Opus")
        self.assertEqual(res["canais"], 2)
        self.assertEqual(res["sample_rate"], 48000)
        self.assertAlmostEqual(res["duracao"], 1.0, places=1)

    def test_valido_encadeado_dois_streams_diferentes(self):
        """Dois fluxos sequenciais encadeados (Chaining) com seriais diferentes devem ser aceitos."""
        # Stream 1: serial 1001, 8000 Hz, 2 páginas de dados = 1600 amostras = 0.20s
        s1 = _gerar_fluxo_vorbis_sintetico(serial=1001, num_paginas_dados=2, sample_rate=8000, amostras_por_pagina=800)
        # Stream 2: serial 2002, 8000 Hz, 3 páginas de dados = 2400 amostras = 0.30s
        s2 = _gerar_fluxo_vorbis_sintetico(serial=2002, num_paginas_dados=3, sample_rate=8000, amostras_por_pagina=800)

        dados_encadeados = s1 + s2
        res = validar_ogg(dados_encadeados)

        self.assertTrue(res["valido"], msg=f"Encadeamento falhou: {res.get('erro')}")
        self.assertIsNone(res["erro"])
        self.assertEqual(res["detalhes"]["total_streams"], 2)
        self.assertEqual(res["detalhes"]["seriais"], [1001, 2002])
        # Duração total deve ser a soma das durações dos fluxos: 0.20s + 0.30s = 0.50s
        self.assertAlmostEqual(res["duracao"], 0.50, places=2)

    def test_oraculo_mutagen_ogg_page_write_independente(self):
        """Gera páginas com a biblioteca Mutagen oficial e valida que nossa validação aceita e confere o CRC."""
        import mutagen.ogg

        # Monta Stream 1 com mutagen independente
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        p0 = mutagen.ogg.OggPage.from_packets([vorbis_id])[0]
        p0.serial = 771122
        p0.sequence = 0
        p0.first = True
        p0.position = 0
        b0 = p0.write()

        # Verifica conferência exata do CRC com Mutagen
        mutagen_crc0 = struct.unpack("<I", b0[22:26])[0]
        self.assertEqual(calcular_ogg_crc(b0), mutagen_crc0)

        p1 = mutagen.ogg.OggPage.from_packets([b"\xaa\xbb\xcc\xdd" * 20])[0]
        p1.serial = 771122
        p1.sequence = 1
        p1.last = True
        p1.position = 8000
        b1 = p1.write()

        mutagen_crc1 = struct.unpack("<I", b1[22:26])[0]
        self.assertEqual(calcular_ogg_crc(b1), mutagen_crc1)

        # Valida o fluxo gerado 100% pelo Mutagen
        fluxo_mutagen = b0 + b1
        res = validar_ogg(fluxo_mutagen)
        self.assertTrue(res["valido"], msg=f"Falha na validação do fluxo gerado pelo Mutagen: {res.get('erro')}")
        self.assertEqual(res["codec"], "Vorbis")
        self.assertEqual(res["canais"], 1)
        self.assertEqual(res["sample_rate"], 8000)
        self.assertAlmostEqual(res["duracao"], 1.0, places=2)

    def test_vetor_conhecido_crc_rfc3533(self):
        """Valida que o cálculo de CRC-32 RFC 3533 confere contra vetor estático conhecido."""
        # Página OggS mínima: version 0, BOS (0x02), granule 0, serial 1, seq 0, num_segments 1, lacing [4], corpo 'test'
        cabecalho_base = struct.pack("<4sBBQIIIB", b"OggS", 0, 0x02, 0, 1, 0, 0, 1) + b"\x04" + b"test"
        crc_calculado = calcular_ogg_crc(cabecalho_base)
        self.assertEqual(crc_calculado, 0xe78c45a3)

    def test_rejeita_crc_invalido(self):
        """Página com CRC corrompido deve ser rejeitada com falha CRC_INVALIDO."""
        serial = 12345
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=serial, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        # Corrompe CRC na página 1
        pag1_corrupt = _montar_pagina_ogg(serial=serial, seq=1, header_type=0x04, granule=1000, corromper_crc=True)
        res = validar_ogg(pag0 + pag1_corrupt)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "CRC_INVALIDO")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_truncamento_cabecalho(self):
        """Arquivo menor que 27 bytes deve ser rejeitado com falha TAMANHO_INSUFICIENTE."""
        res = validar_ogg(b"OggS\x00\x02\x00\x00")
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "TAMANHO_INSUFICIENTE")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_truncamento_corpo(self):
        """Arquivo truncado no meio do pacote deve ser rejeitado com falha TRUNCAMENTO_CORPO."""
        dados = _gerar_fluxo_vorbis_sintetico(num_paginas_dados=1)
        res = validar_ogg(dados[:-10])  # Corta 10 bytes do final
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "TRUNCAMENTO_CORPO")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_versao_invalida(self):
        """Página com version != 0 deve ser rejeitada conforme RFC 3533 com VERSAO_INVALIDA."""
        serial = 12345
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0_v1 = _montar_pagina_ogg(serial=serial, seq=0, header_type=0x02, granule=0, version=1, packets=[vorbis_id])
        res = validar_ogg(pag0_v1)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "VERSAO_INVALIDA")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_quebra_encadeamento_gap(self):
        """Gaps de dados espúrios entre páginas dentro de um fluxo ativo devem gerar QUEBRA_ENCADEAMENTO."""
        dados = _gerar_fluxo_vorbis_sintetico(num_paginas_dados=2)
        # Encontra o término da primeira página e injeta bytes espúrios antes da próxima página
        ponto_corte = dados.find(b"OggS", 4)
        dados_com_gap = dados[:ponto_corte] + b"LIXO_ESPURIO_123" + dados[ponto_corte:]
        res = validar_ogg(dados_com_gap)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "QUEBRA_ENCADEAMENTO")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_multiplexacao_multiplos_seriais(self):
        """Intercalação de múltiplos seriais concorrentes antes de EOS deve ser recusada."""
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=1001, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        # Página de outro serial intercalada sem fechar o stream 1001
        pag1_outro_serial = _montar_pagina_ogg(serial=9999, seq=1, header_type=0x00, granule=1000)
        res = validar_ogg(pag0 + pag1_outro_serial)
        self.assertFalse(res["valido"])
        self.assertIn(res["detalhes"]["falha"], ("MULTIPLEXACAO_CONCORRENTE", "MULTIPLEXACAO_BOS_INTERNO"))
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_falta_bos_primeira_pagina(self):
        """Primeira página sem flag BOS (0x02) deve ser rejeitada com BOS_AUSENTE."""
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0_sem_bos = _montar_pagina_ogg(serial=1001, seq=0, header_type=0x00, granule=0, packets=[vorbis_id])
        res = validar_ogg(pag0_sem_bos)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "BOS_AUSENTE")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_bos_seq_diferente_de_zero(self):
        """Página BOS com número de sequência != 0 viola a RFC 3533 e deve ser rejeitada com BOS_SEQ_NAO_ZERO."""
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0_seq_invalida = _montar_pagina_ogg(serial=1001, seq=1, header_type=0x02, granule=0, packets=[vorbis_id])
        res = validar_ogg(pag0_seq_invalida)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "BOS_SEQ_NAO_ZERO")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_serial_duplicado_em_chain(self):
        """Stream encadeado após EOS que reutiliza o mesmo serial de um fluxo anterior deve ser rejeitado."""
        s1 = _gerar_fluxo_vorbis_sintetico(serial=3333, num_paginas_dados=1)
        # s2 tenta reutilizar o mesmo serial 3333
        s2_mesmo_serial = _gerar_fluxo_vorbis_sintetico(serial=3333, num_paginas_dados=1)
        res = validar_ogg(s1 + s2_mesmo_serial)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "SERIAL_DUPLICADO_CHAIN")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_aviso_falta_eos_ultima_pagina(self):
        """Fluxo contíguo válido mas sem flag EOS na última página deve passar com aviso informativo."""
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=1001, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        pag1_sem_eos = _montar_pagina_ogg(serial=1001, seq=1, header_type=0x00, granule=1000)
        res = validar_ogg(pag0 + pag1_sem_eos)
        self.assertTrue(res["valido"])
        self.assertTrue(any("EOS" in a for a in res["avisos"]))

    def test_ultimo_sem_eos_granule_menos_um_preserva_duracao_real(self):
        """Página com granule 0xFFFFFFFFFFFFFFFF (-1 sentinela) não deve sobrescrever a duração válida anterior."""
        serial = 884422
        # Page 0 (BOS): Vorbis ID header (8000 Hz)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=serial, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        # Page 1: Pacote de áudio com granule = 8000 (1.0 segundo de áudio a 8 kHz)
        pag1 = _montar_pagina_ogg(serial=serial, seq=1, header_type=0x00, granule=8000, packets=[b"\x12\x34" * 16])
        # Page 2: Pacote sem encerramento e sem EOS, com granule sentinela 0xFFFFFFFFFFFFFFFF (-1)
        pag2_sentinela = _montar_pagina_ogg(serial=serial, seq=2, header_type=0x00, granule=0xFFFFFFFFFFFFFFFF, packets=[b"\x56\x78" * 16])

        dados = pag0 + pag1 + pag2_sentinela
        res = validar_ogg(dados)

        self.assertTrue(res["valido"], msg=f"Falha inesperada: {res.get('erro')}")
        self.assertIsNone(res["erro"])
        # A duração deve ser preservada em 1.0s (do granule 8000), e não corrompida por 0xFFFFFFFFFFFFFFFF
        self.assertEqual(res["duracao"], 1.0)
        self.assertTrue(any("EOS" in a for a in res["avisos"]))

    def test_rejeita_lixo_apos_eos(self):
        """Bytes adicionais após a página EOS devem invalidar o contêiner com falha LIXO_RESIDUAL."""
        dados = _gerar_fluxo_vorbis_sintetico(num_paginas_dados=1)
        res = validar_ogg(dados + b"EXTRAS_RESIDUAIS")
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "LIXO_RESIDUAL")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    def test_rejeita_quebra_sequencia_seq(self):
        """Salto no número de sequência de página (seq) deve ser rejeitado com QUEBRA_SEQUENCIA."""
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01\x40\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=1001, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        pag1_pula_seq = _montar_pagina_ogg(serial=1001, seq=5, header_type=0x04, granule=1000)
        res = validar_ogg(pag0 + pag1_pula_seq)
        self.assertFalse(res["valido"])
        self.assertEqual(res["detalhes"]["falha"], "QUEBRA_SEQUENCIA")
        self.assertTrue(isinstance(res["erro"], str) and len(res["erro"]) > 0)

    # -------------------------------------------------------------------------
    # Testes do Contrato Canônico: obter_prefixo_valido_pre_defeito
    # -------------------------------------------------------------------------
    def test_obter_prefixo_valido_retorna_none_para_dados_invalidos_ou_truncados(self):
        """Retorna None para entradas não-OGG, versão inválida, flag BOS ausente ou p0 corrompida (FALHA FECHADA)."""
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b""))
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b"lixo sem ogg"))
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b"OggS\x01" + b"\x00" * 30))  # versao 1
        self.assertIsNone(obter_prefixo_valido_pre_defeito(b"OggS\x00\x00" + b"\x00" * 30))  # sem flag BOS

        # Página 0 com CRC corrompido
        sr_bytes = struct.pack("<I", 8000)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        p0_bad_crc = _montar_pagina_ogg(serial=123, seq=0, header_type=0x02, granule=0, packets=[vorbis_id], corromper_crc=True)
        self.assertIsNone(obter_prefixo_valido_pre_defeito(p0_bad_crc))

        # Página 0 com codec desconhecido
        p0_codec_desconhecido = _montar_pagina_ogg(serial=123, seq=0, header_type=0x02, granule=0, packets=[b"\x00DESCONHECIDO\x00"])
        self.assertIsNone(obter_prefixo_valido_pre_defeito(p0_codec_desconhecido))

    def test_obter_prefixo_valido_retorna_zero_quando_defeito_ocorre_apos_p0_em_granule_zero(self):
        """Retorna 0.0 legítimo se p0 for válida mas a falha ocorrer imediatamente antes de qualquer áudio (granule 0)."""
        sr_bytes = struct.pack("<I", 8000)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=999, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        # p1 com granule 0 e CRC corrompido
        pag1_bad = _montar_pagina_ogg(serial=999, seq=1, header_type=0x00, granule=0, corromper_crc=True)
        res = obter_prefixo_valido_pre_defeito(pag0 + pag1_bad)
        self.assertEqual(res, 0.0)

    def test_obter_prefixo_valido_calcula_duracao_exata_pre_defeito_vorbis(self):
        """Calcula duração do último granule válido antes de falha de CRC em fluxo Vorbis."""
        sr_bytes = struct.pack("<I", 8000)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=777, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        pag1 = _montar_pagina_ogg(serial=777, seq=1, header_type=0x00, granule=4000, packets=[b"\x11" * 32])  # 0.5s
        pag2 = _montar_pagina_ogg(serial=777, seq=2, header_type=0x00, granule=8000, packets=[b"\x22" * 32])  # 1.0s
        # pag3 com CRC corrompido
        pag3_bad = _montar_pagina_ogg(serial=777, seq=3, header_type=0x00, granule=12000, packets=[b"\x33" * 32], corromper_crc=True)

        dur = obter_prefixo_valido_pre_defeito(pag0 + pag1 + pag2 + pag3_bad)
        self.assertEqual(dur, 1.0)

    def test_obter_prefixo_valido_calcula_duracao_exata_pre_defeito_opus(self):
        """Calcula duração do último granule válido antes de falha em fluxo Opus (descontando pre_skip)."""
        opus_head = struct.pack("<8sBBHIhB", b"OpusHead", 1, 2, 312, 48000, 0, 0)
        pag0 = _montar_pagina_ogg(serial=444, seq=0, header_type=0x02, granule=0, packets=[opus_head])
        # 1 segundo a 48000Hz + pre_skip 312 = granule 48312
        pag1 = _montar_pagina_ogg(serial=444, seq=1, header_type=0x00, granule=48312, packets=[b"\xf8" * 64])
        # pag2 com CRC corrompido
        pag2_bad = _montar_pagina_ogg(serial=444, seq=2, header_type=0x00, granule=96000, corromper_crc=True)

        dur = obter_prefixo_valido_pre_defeito(pag0 + pag1 + pag2_bad)
        self.assertEqual(dur, 1.0)

    def test_obter_prefixo_valido_salto_seq_interrompe_no_ultimo_valido(self):
        """Salto no número de sequência (seq) interrompe o cálculo preservando o último granule íntegro."""
        sr_bytes = struct.pack("<I", 8000)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=333, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        pag1 = _montar_pagina_ogg(serial=333, seq=1, header_type=0x00, granule=4000, packets=[b"\xaa" * 32])  # 0.5s
        pag2 = _montar_pagina_ogg(serial=333, seq=2, header_type=0x00, granule=8000, packets=[b"\xbb" * 32])  # 1.0s
        # Salto de seq: seq 8 em vez de 3
        pag3_pulo = _montar_pagina_ogg(serial=333, seq=8, header_type=0x00, granule=16000, packets=[b"\xcc" * 32])

        dur = obter_prefixo_valido_pre_defeito(pag0 + pag1 + pag2 + pag3_pulo)
        self.assertEqual(dur, 1.0)

    def test_obter_prefixo_valido_ignora_granule_menos_um(self):
        """Página intermediária com granule 0xFFFFFFFFFFFFFFFF (-1) não deve sobrescrever o granule válido."""
        sr_bytes = struct.pack("<I", 8000)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=222, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        pag1 = _montar_pagina_ogg(serial=222, seq=1, header_type=0x00, granule=4000, packets=[b"\xaa" * 32])  # 0.5s
        # pag2 com sentinela -1
        pag2_sentinela = _montar_pagina_ogg(serial=222, seq=2, header_type=0x00, granule=0xFFFFFFFFFFFFFFFF, packets=[b"\xbb" * 32])
        # pag3 corrompida
        pag3_bad = _montar_pagina_ogg(serial=222, seq=3, header_type=0x00, granule=12000, corromper_crc=True)

        dur = obter_prefixo_valido_pre_defeito(pag0 + pag1 + pag2_sentinela + pag3_bad)
        self.assertEqual(dur, 0.5)


if __name__ == "__main__":
    unittest.main()
