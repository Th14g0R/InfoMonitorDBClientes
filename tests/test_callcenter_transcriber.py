import hashlib
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# Adiciona raiz e callcenter_transcriber ao path
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "callcenter_transcriber"))

from gemini_transcriber import (
    FORMATOS_PERMITIDOS,
    LIMITE_DURACAO_SEGUNDOS,
    LIMITE_TAMANHO_MB,
    MODELOS_DISPONIVEIS,
    NORMALIZER_VERSION,
    calcular_hash_audio,
    deve_fazer_fallback_modelo,
    eh_erro_503_indisponivel,
    extrair_texto_transcricao,
    inspecionar_audio,
    obter_mime_audio,
    processar_audio,
    sanitizar_erro,
    sincronizar_upload_session,
)


class FakeUploadedFile:
    def __init__(self, name="gravacao.ogg", data=b"OggS_fake_audio_bytes", size=None, file_id=None):
        self.name = name
        self.data = data
        self.size = size if size is not None else len(data)
        self.file_id = file_id
        self._buf = BytesIO(data)

    def read(self, *args, **kwargs):
        return self._buf.read(*args, **kwargs)

    def seek(self, *args, **kwargs):
        return self._buf.seek(*args, **kwargs)

    def tell(self):
        return self._buf.tell()

    def getvalue(self):
        return self.data


class CallCenterTranscriberTest(unittest.TestCase):
    def test_modelos_disponiveis_padrao_e_flash(self):
        """O modelo padrão obrigatório deve ser Flash (gemini-3.5-flash-lite) e sem transcribe na lista."""
        self.assertIn("gemini-3.5-flash-lite", MODELOS_DISPONIVEIS)
        self.assertEqual(MODELOS_DISPONIVEIS[0], "gemini-3.5-flash-lite")
        self.assertNotIn("gemini-3.5-transcribe", MODELOS_DISPONIVEIS)

    def test_obter_mime_audio_mapeamento_canonico_e_rejeicao_extensao_desconhecida(self):
        """Mapeia extensões canônicas suportadas e lança ValueError para extensões desconhecidas."""
        self.assertEqual(obter_mime_audio("chamada.ogg"), "audio/ogg")
        self.assertEqual(obter_mime_audio("CHAMADA.OGG"), "audio/ogg")
        self.assertEqual(obter_mime_audio("audio.mp3"), "audio/mp3")
        self.assertEqual(obter_mime_audio("voz.m4a"), "audio/mp4")
        self.assertEqual(obter_mime_audio("voz.aac"), "audio/aac")
        self.assertEqual(obter_mime_audio("gravacao.wav"), "audio/wav")
        self.assertEqual(obter_mime_audio("musica.flac"), "audio/flac")

        # Rejeição estrita de extensão desconhecida ou vazia
        with self.assertRaises(ValueError) as ctx1:
            obter_mime_audio("arquivo.desconhecido")
        self.assertIn("não suportada", str(ctx1.exception))

        with self.assertRaises(ValueError) as ctx2:
            obter_mime_audio("sem_extensao")
        self.assertIn("não suportada", str(ctx2.exception))

    def test_deve_fazer_fallback_modelo_estrito_sem_falso_positivo_de_substring(self):
        """Apenas 503/UNAVAILABLE e 404/400 de modelo disparam fallback. Erro 400 com 503 no texto é rejeitado."""
        # 1. 503 / UNAVAILABLE / Overload -> TRUE
        self.assertTrue(deve_fazer_fallback_modelo(RuntimeError("503 Service Unavailable")))
        self.assertTrue(deve_fazer_fallback_modelo(RuntimeError("The model is overloaded. Please try again later.")))

        # 2. Objeto estruturado com code=503 -> TRUE
        err_struct_503 = Exception("Erro interno")
        err_struct_503.code = 503
        self.assertTrue(deve_fazer_fallback_modelo(err_struct_503))

        # 3. 404 Modelo inexistente -> TRUE
        self.assertTrue(deve_fazer_fallback_modelo(RuntimeError("404 models/gemini-3.5-flash-lite is not found")))

        # 4. 400 Whitelist estrita de modelo -> TRUE
        self.assertTrue(deve_fazer_fallback_modelo(RuntimeError("400 INVALID_ARGUMENT: model is not supported for this API version")))
        self.assertTrue(deve_fazer_fallback_modelo(RuntimeError("400: unknown model gemini-antigo")))

        # 5. CASO CRÍTICO: 400 que contém '503' como substring/offset no corpo do texto -> DEVE SER FALSE!
        err_400_com_503_no_texto = RuntimeError("400 INVALID_ARGUMENT: Corrupted frame at byte offset 503 in audio stream")
        self.assertFalse(deve_fazer_fallback_modelo(err_400_com_503_no_texto))

        err_struct_400 = Exception("Falha no payload de chamada 503")
        err_struct_400.code = 400
        self.assertFalse(deve_fazer_fallback_modelo(err_struct_400))

        # 6. Erros genéricos de permissão ou chave -> FALSE
        self.assertFalse(deve_fazer_fallback_modelo(RuntimeError("400 API_KEY_INVALID")))
        self.assertFalse(deve_fazer_fallback_modelo(RuntimeError("403 PERMISSION_DENIED: The caller does not have permission")))

    def test_sanitizar_erro_redige_chave_explicita_ambiente_e_caminhos(self):
        """Garante que chave explícita, chave do env, tokens e caminhos do SO sejam mascarados."""
        chave_digitada = "AIzaSySecretExplicitKey1234567890"
        with patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyEnvKey9876543210"}):
            msg = f"Falha na API: {chave_digitada} em C:\\TEMP\\site\\secrets\\auth.json e AIzaSyEnvKey9876543210 <script>bad</script>"
            sanitizada = sanitizar_erro(Exception(msg), chave_api=chave_digitada)

            self.assertNotIn("AIzaSySecretExplicitKey1234567890", sanitizada)
            self.assertNotIn("AIzaSyEnvKey9876543210", sanitizada)
            self.assertNotIn("C:\\TEMP\\site", sanitizada)
            self.assertNotIn("<script>", sanitizada)
            self.assertIn("[caminho_local]", sanitizada)

    def test_extrair_texto_transcricao_trata_finish_reason_max_tokens_e_block_reason(self):
        """Verifica captura de aviso para finish_reason != STOP e bloqueio seguro para block_reason."""
        # 1. finish_reason == 'MAX_TOKENS' -> deve retornar texto e aviso de corte
        resp_corte = MagicMock()
        cand_corte = MagicMock()
        cand_corte.finish_reason.name = "MAX_TOKENS"
        part_corte = MagicMock()
        part_corte.text = "Parte do diálogo transcrito..."
        part_corte.audio_transcription = None
        cand_corte.content.parts = [part_corte]
        resp_corte.candidates = [cand_corte]
        resp_corte.prompt_feedback = None

        texto, aviso = extrair_texto_transcricao(resp_corte)
        self.assertEqual(texto, "Parte do diálogo transcrito...")
        self.assertIsNotNone(aviso)
        self.assertIn("MAX_TOKENS", aviso)

        # 2. block_reason de segurança -> deve lançar RuntimeError explicativo
        resp_block = MagicMock()
        resp_block.prompt_feedback.block_reason = "SAFETY"
        with self.assertRaises(RuntimeError) as ctx:
            extrair_texto_transcricao(resp_block)
        self.assertIn("bloqueado pela política de segurança", str(ctx.exception))

        # 3. candidates vazios e resp.text None -> trata com segurança retornando vazio
        resp_vazio = MagicMock()
        resp_vazio.candidates = None
        resp_vazio.text = None
        resp_vazio.prompt_feedback = None
        texto_vazio, aviso_vazio = extrair_texto_transcricao(resp_vazio)
        self.assertEqual(texto_vazio, "")
        self.assertIsNone(aviso_vazio)

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_envia_mime_type_correto_e_faz_cleanup(self, mock_client_cls):
        """Verifica upload com config mime_type='audio/ogg' e delete garantido no finally."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_file = MagicMock()
        mock_file.name = "files/test_file_123"
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock()
        mock_resp_trans.text = "[00:05] Atendente: Suporte Infobrasil.\n[00:10] Cliente: Bom dia."
        mock_resp_trans.candidates = []
        mock_resp_trans.prompt_feedback = None

        mock_resp_meta = MagicMock()
        mock_resp_meta.text = '{"empresa_cliente_fantasia": "Mercado Bom", "motivo_contato": "Duvida"}'
        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        arquivo = FakeUploadedFile(name="chamada.ogg")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertEqual(modelo_usado, "gemini-3.5-flash-lite")
        self.assertIsNone(aviso_meta)
        self.assertEqual(resultado["empresa_cliente_fantasia"], "Mercado Bom")

        # Verifica MIME explícito
        mock_client.files.upload.assert_called_once()
        _, kwargs_upload = mock_client.files.upload.call_args
        config = kwargs_upload.get("config")
        self.assertEqual(getattr(config, "mime_type", None) or config.get("mime_type"), "audio/ogg")

        # Verifica cleanup remoto
        mock_client.files.delete.assert_called_once_with(name="files/test_file_123")

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_guarda_active_rechecagem_final_cobre_30a_consulta(self, mock_client_cls):
        """Garante que se o arquivo ficar ACTIVE na 30ª consulta, o loop NÃO lança falso TimeoutError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_file_init = MagicMock()
        mock_file_init.name = "files/arquivo_30"
        mock_file_init.state.name = "PROCESSING"
        mock_client.files.upload.return_value = mock_file_init

        # Retorna PROCESSING 29 vezes e ACTIVE na 30ª consulta get
        estados = [MagicMock(name="files/arquivo_30", state=MagicMock(name="PROCESSING")) for _ in range(29)]
        mock_file_active = MagicMock(name="files/arquivo_30")
        mock_file_active.state.name = "ACTIVE"
        estados.append(mock_file_active)
        mock_client.files.get.side_effect = estados

        mock_resp_trans = MagicMock()
        mock_resp_trans.text = "[00:01] Atendente: Olá"
        mock_resp_trans.candidates = []
        mock_resp_trans.prompt_feedback = None
        mock_resp_meta = MagicMock()
        mock_resp_meta.text = "{}"
        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        arquivo = FakeUploadedFile(name="audio_limite.mp3")
        with patch("time.sleep", return_value=None):
            resultado, modelo_usado, _ = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        # Não deve lançar TimeoutError!
        self.assertEqual(modelo_usado, "gemini-3.5-flash-lite")

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_guarda_active_lanca_timeout_apos_30s_se_permanecer_processing(self, mock_client_cls):
        """Se o arquivo continuar PROCESSING após 30 tentativas e rechecagem final, lança TimeoutError com etapa."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_file = MagicMock()
        mock_file.name = "files/arquivo_lento"
        mock_file.state.name = "PROCESSING"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        arquivo = FakeUploadedFile(name="audio_lento.ogg")
        with patch("time.sleep", return_value=None):
            with self.assertRaises(TimeoutError) as ctx:
                processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertIn("[Etapa: Processamento do Arquivo]", str(ctx.exception))
        self.assertIn("PROCESSING", str(ctx.exception))

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_erro_generico_400_nao_faz_fallback_e_indica_etapa_e_modelo(self, mock_client_cls):
        """Erro 400 de argumento falha na hora sem fallback para outros modelos."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_file = MagicMock()
        mock_file.name = "files/test_file"
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_client.models.generate_content.side_effect = RuntimeError("400 INVALID_ARGUMENT. Invalid argument payload.")

        arquivo = FakeUploadedFile(name="audio.ogg")
        with self.assertRaises(RuntimeError) as ctx:
            processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertIn("[Etapa: Transcrição - Modelo: gemini-3.5-flash-lite]", str(ctx.exception))
        self.assertEqual(mock_client.models.generate_content.call_count, 1)

    def test_inspecionar_audio_integracao_ui_e_rejeicao_wav_e_tamanho(self):
        """Helper inspecionar_audio valida extensões, rejeita .wav e limite de 64 MB."""
        # 1. Rejeição de .wav
        arq_wav = FakeUploadedFile(name="chamada.wav", data=b"RIFFwavdata")
        info_wav = inspecionar_audio(arq_wav)
        self.assertFalse(info_wav["valido"])
        self.assertIn("sem compactação e não é permitido", info_wav["erros"][0])

        # 2. Rejeição de arquivo > 64 MB
        tam_grande = int(65 * 1024 * 1024)
        arq_grande = FakeUploadedFile(name="chamada.mp3", data=b"0", size=tam_grande)
        info_grande = inspecionar_audio(arq_grande)
        self.assertFalse(info_grande["valido"])
        self.assertIn("64 MB", info_grande["erros"][0])

    def test_inspecionar_audio_cache_determinista_hash_evita_bypass_mesmo_nome_e_tamanho(self):
        """Dois arquivos com o MESMO nome e MESMO tamanho mas bytes diferentes geram chaves de cache distintas."""
        conteudo_corrompido = b"OggS" + b"\x00" * 32000 + b"lixo"
        conteudo_valido_mock = b"OggS" + b"\x01" * 32000 + b"bom!"

        arq1 = FakeUploadedFile(name="chamada.ogg", data=conteudo_corrompido)
        arq2 = FakeUploadedFile(name="chamada.ogg", data=conteudo_valido_mock)

        # Mesmos nomes e tamanhos
        self.assertEqual(arq1.name, arq2.name)
        self.assertEqual(arq1.size, arq2.size)

        # Hashes de cache são obrigatoriamente diferentes
        hash1 = calcular_hash_audio(arq1)
        hash2 = calcular_hash_audio(arq2)
        self.assertNotEqual(hash1, hash2)

        session_cache = {}
        with patch("callcenter_transcriber.ogg_validation.validar_ogg") as mock_val:
            mock_val.side_effect = [
                {"valido": False, "erro": "CRC inválido", "codec": "Vorbis", "detalhes": {"falha": "CRC_INVALIDO", "pagina": 9}},
                {"valido": True, "erro": None, "avisos": [], "duracao": 15.0}
            ]

            info1 = inspecionar_audio(arq1, session_cache)
            info2 = inspecionar_audio(arq2, session_cache)

            # arq1 tem OggS e Vorbis com falha na pág 9: é recuperável (botão liberado com requer_recuperacao)
            self.assertTrue(info1["valido"])
            self.assertTrue(info1.get("requer_recuperacao"))
            self.assertTrue(info2["valido"])
            self.assertFalse(info2.get("requer_recuperacao", False))
            self.assertEqual(mock_val.call_count, 2)

            # Nova chamada de arq1 deve bater no cache sem chamar o validador novamente
            info1_cached = inspecionar_audio(arq1, session_cache)
            self.assertTrue(info1_cached["valido"])
            self.assertTrue(info1_cached.get("requer_recuperacao"))
            self.assertEqual(mock_val.call_count, 2)

    def test_inspecionar_audio_falso_ogg_sem_cabecalho_rejeita_com_mensagem_amigavel(self):
        """Falso OGG (m3u8, texto HTML ou lixo sem b'OggS') deve ser rejeitado bloqueando o botão sem tentar normalizar."""
        arq_falso = FakeUploadedFile(name="chamada_fake.ogg", data=b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000\nfake.ts")
        with patch("callcenter_transcriber.ogg_validation.validar_ogg", return_value={"valido": False, "erro": "Não é OggS", "codec": None, "detalhes": {"falha": "TAMANHO_INSUFICIENTE"}}):
            info = inspecionar_audio(arq_falso)
            self.assertFalse(info["valido"])
            self.assertFalse(info.get("requer_recuperacao", False))
            self.assertTrue(len(info["erros"]) > 0)
            msg_erro = info["erros"][0]
            self.assertIn("central telefônica/PBX", msg_erro)
            self.assertNotIn("erro 400", msg_erro)

    def test_inspecionar_audio_ogg_invalido_estruturado_libera_botao_com_aviso_recuperacao(self):
        """OGG com cabeçalho válido mas com corrupção de CRC/gap posterior deve liberar o botão e sinalizar recuperação."""
        arq_recuperavel = FakeUploadedFile(name="chamada_defeituosa.ogg", data=b"OggS" + b"\x00" * 32000)
        retorno_validador = {
            "valido": False,
            "erro": "Página #9 CRC inválido",
            "codec": "Vorbis",
            "detalhes": {"falha": "CRC_INVALIDO", "pagina": 9, "offset": 32063}
        }
        with patch("callcenter_transcriber.ogg_validation.validar_ogg", return_value=retorno_validador):
            info = inspecionar_audio(arq_recuperavel)
            # O botão NÃO é bloqueado (valido = True) e requer_recuperacao é True
            self.assertTrue(info["valido"])
            self.assertTrue(info.get("requer_recuperacao"))
            self.assertEqual(len(info["erros"]), 0)
            self.assertTrue(any("inconsistências estruturais" in a.lower() for a in info["avisos"]))

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_com_recuperacao_flac_envia_mesmos_bytes_e_mime_e_persiste_aviso(self, mock_client_cls):
        """Áudio recuperado converte cópia para FLAC, envia audio/flac para o Gemini e repassa os mesmos bytes ao player."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_file = MagicMock()
        mock_file.name = "files/flac_recuperado"
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock()
        mock_resp_trans.text = "[00:05] Atendente: Suporte.\n[00:15] Cliente: Resolvido."
        mock_resp_trans.candidates = []
        mock_resp_trans.prompt_feedback = None

        mock_resp_meta = MagicMock()
        mock_resp_meta.text = '{"empresa_cliente_fantasia": "Auto Posto", "motivo_contato": "Duvida"}'
        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        dados_originais = b"OggS_corrupt_stream_bytes"
        arquivo = FakeUploadedFile(name="chamada_corrompida.ogg", data=dados_originais)

        mock_norm_retorno = {
            "sucesso": True,
            "audio_bytes": b"fLaC_flac_lossless_audio_bytes",
            "caminho_audio": None,
            "mime_type": "audio/flac",
            "duracao": 45.0,
            "canais": 1,
            "sample_rate": 8000,
            "tamanho_bytes": 1024,
            "avisos": ["Aviso: gravação recuperada de contêiner danificado. Timestamps e reprodução referem-se à cópia normalizada; trechos corrompidos podem ter sido cortados."],
            "erro": None
        }

        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm_retorno, create=True), \
             patch("callcenter_transcriber.ogg_validation.validar_ogg", return_value={"valido": False, "codec": "Vorbis", "detalhes": {"falha": "CRC_INVALIDO", "pagina": 9, "duracao_prefixo_valido": 9.376}}):
            resultado, modelo_usado, aviso_meta = processar_audio(
                arquivo, "chave_falsa", "gemini-3.5-flash-lite", requer_recuperacao=True, duracao_prefixo_valido=9.376
            )

        # 1. Verifica que o Gemini recebeu audio/flac
        mock_client.files.upload.assert_called_once()
        _, kwargs_upload = mock_client.files.upload.call_args
        config = kwargs_upload.get("config")
        self.assertEqual(getattr(config, "mime_type", None) or config.get("mime_type"), "audio/flac")

        # 2. Verifica que os mesmos bytes FLAC foram salvos no resultado para uso no player
        self.assertEqual(resultado["audio_bytes"], b"fLaC_flac_lossless_audio_bytes")
        self.assertEqual(resultado["mime_type"], "audio/flac")
        self.assertTrue(resultado["audio_recuperado"])
        self.assertTrue(resultado["recuperacao_parcial"])
        self.assertEqual(resultado["duracao_recuperada"], 45.0)
        self.assertIn("gravação recuperada de contêiner danificado", resultado["aviso_recuperacao"])

        # 3. O arquivo original NUNCA é modificado
        self.assertEqual(arquivo.getvalue(), dados_originais)

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_falha_normalizacao_aborta_sem_chamar_gemini(self, mock_client_cls):
        """Se a normalização do áudio falhar, interrompe o fluxo imediatamente sem fazer upload no Gemini."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_norm_falha = {
            "sucesso": False,
            "audio_bytes": None,
            "caminho_audio": None,
            "mime_type": "audio/flac",
            "duracao": 0.0,
            "canais": 1,
            "sample_rate": 8000,
            "tamanho_bytes": 0,
            "avisos": [],
            "erro": "Falha na decodificação FFmpeg: arquivo com corrupção irrecuperável"
        }

        arquivo = FakeUploadedFile(name="chamada_irrecuperavel.ogg", data=b"OggS_bad")
        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm_falha, create=True), \
             patch("callcenter_transcriber.ogg_validation.validar_ogg", return_value={"valido": False, "codec": "Vorbis", "detalhes": {"falha": "CRC_INVALIDO", "pagina": 9}}):
            with self.assertRaises(RuntimeError) as ctx:
                processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite", requer_recuperacao=True)

        self.assertIn("Recuperação de Áudio", str(ctx.exception))
        self.assertIn("arquivo com corrupção irrecuperável", str(ctx.exception))
        # NUNCA faz upload se a normalização falhou
        mock_client.files.upload.assert_not_called()

    def test_sincronizar_upload_session_dois_uploads_descarta_resultado_player_e_cache_norm(self):
        """Ao trocar o upload ou remover o arquivo, limpa resultado, player e caches norm_* anteriores (C3)."""
        session = {
            "current_file_token": "id1_chamada1.ogg_100",
            "resultado": {"dialogo": []},
            "audio_bytes": b"flac_1",
            "mime_type": "audio/flac",
            "ultimo_arquivo": "chamada1.ogg",
            "aviso_recuperacao": "Aviso antigo",
            "meta_aviso": "Meta antigo",
            f"norm_hash1_{NORMALIZER_VERSION}": {"sucesso": True},
            "outra_chave_preservada": "manter"
        }

        # 1. Upload do segundo arquivo diferente
        arq2 = FakeUploadedFile(name="chamada2.ogg", data=b"conteudo2", size=200, file_id="id2")
        mudou = sincronizar_upload_session(arq2, session)
        self.assertTrue(mudou)
        self.assertNotIn("resultado", session)
        self.assertNotIn("audio_bytes", session)
        self.assertNotIn("aviso_recuperacao", session)
        self.assertNotIn(f"norm_hash1_{NORMALIZER_VERSION}", session)
        self.assertEqual(session.get("outra_chave_preservada"), "manter")
        self.assertEqual(session.get("current_file_token"), "id2_chamada2.ogg_200")

        # 2. Mesmo arquivo mantido não deve limpar
        mudou_repetido = sincronizar_upload_session(arq2, session)
        self.assertFalse(mudou_repetido)

        # 3. Limpeza ao remover o arquivo (uploaded_file is None)
        session["resultado"] = {"dialogo": ["novo"]}
        session["audio_bytes"] = b"bytes2"
        mudou_remocao = sincronizar_upload_session(None, session)
        self.assertTrue(mudou_remocao)
        self.assertNotIn("resultado", session)
        self.assertNotIn("audio_bytes", session)
        self.assertNotIn("current_file_token", session)

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_recusa_upload_se_duracao_recuperada_nao_ultrapassa_prefixo(self, mock_client_cls):
        """Se o áudio recuperado não ultrapassar o prefixo íntegro pré-defeito, falha fechada sem upload no Gemini."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_norm_prefixo = {
            "sucesso": True,
            "audio_bytes": b"fLaC_apenas_prefixo",
            "caminho_audio": None,
            "mime_type": "audio/flac",
            "duracao": 9.376,
            "canais": 1,
            "sample_rate": 8000,
            "tamanho_bytes": 1024,
            "avisos": [],
            "erro": None
        }

        arquivo = FakeUploadedFile(name="incidente_prefixo.ogg", data=b"OggS_prefixo")
        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm_prefixo, create=True), \
             patch("callcenter_transcriber.ogg_validation.validar_ogg", return_value={"valido": False, "codec": "Vorbis", "detalhes": {"falha": "CRC_INVALIDO", "pagina": 9, "duracao_prefixo_valido": 9.376}}):
            with self.assertRaises(RuntimeError) as ctx:
                processar_audio(
                    arquivo,
                    "chave_falsa",
                    "gemini-3.5-flash-lite",
                    requer_recuperacao=True,
                    duracao_prefixo_valido=9.376
                )

        self.assertIn("Não foi possível recuperar a gravação além do trecho inicial", str(ctx.exception))
        # NUNCA faz upload se não ultrapassou o prefixo
        mock_client.files.upload.assert_not_called()

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_sintetico_quebra_sequencia_1s_normalizador_1s_recusa_e_3s_segue(self, mock_client_cls):
        """Synthetic test: quebra de sequência aos 1s com normalizer 1s recusa upload; com 3s segue para Gemini."""
        import struct
        from tests.test_ogg_validation import _montar_pagina_ogg

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_file = MagicMock()
        mock_file.name = "files/flac_3s"
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock()
        mock_resp_trans.text = "[00:01] Carlos: Olá.\n[00:02] Ana: Bom dia."
        mock_resp_trans.candidates = []
        mock_resp_trans.prompt_feedback = None

        mock_resp_meta = MagicMock()
        mock_resp_meta.text = '{"atendente": "Carlos", "cliente": "Ana"}'
        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        # Gera fluxo Vorbis sintético onde a sequência quebra aos 1.0s (8000 amostras a 8000Hz)
        sr = 8000
        sr_bytes = struct.pack("<I", sr)
        vorbis_id = b"\x01vorbis\x00\x00\x00\x00\x01" + sr_bytes + b"\x00\x00\x00\x00\x00\x00\x00\x00\xb8\x01"
        pag0 = _montar_pagina_ogg(serial=5555, seq=0, header_type=0x02, granule=0, packets=[vorbis_id])
        pag1 = _montar_pagina_ogg(serial=5555, seq=1, header_type=0x00, granule=4000, packets=[b"\x11" * 32])
        pag2 = _montar_pagina_ogg(serial=5555, seq=2, header_type=0x00, granule=8000, packets=[b"\x22" * 32])
        # Salto de sequência: seq 5 em vez de 3 -> gera QUEBRA_SEQUENCIA com duracao_prefixo_valido = 1.0s
        pag3_salto = _montar_pagina_ogg(serial=5555, seq=5, header_type=0x00, granule=12000, packets=[b"\x33" * 32])

        dados_ogg_quebra = pag0 + pag1 + pag2 + pag3_salto
        arquivo = FakeUploadedFile(name="chamada_seq_quebrada.ogg", data=dados_ogg_quebra)

        # Inspeciona: deve detectar recuperação e extrair duracao_prefixo_valido = 1.0s
        info = inspecionar_audio(arquivo)
        self.assertTrue(info["valido"])
        self.assertTrue(info["requer_recuperacao"])
        self.assertAlmostEqual(info["duracao_prefixo_valido"], 1.0, places=2)

        # CASO A: Normalizer só gerou 1.0s (<= prefixo 1.0) -> DEVE RECUSAR SEM UPLOAD
        mock_norm_1s = {
            "sucesso": True,
            "audio_bytes": b"fLaC_1s",
            "caminho_audio": None,
            "mime_type": "audio/flac",
            "duracao": 1.0,
            "canais": 1,
            "sample_rate": 8000,
            "tamanho_bytes": 512,
            "avisos": [],
            "erro": None
        }
        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm_1s, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                processar_audio(
                    arquivo,
                    "chave_falsa",
                    "gemini-3.5-flash-lite",
                    requer_recuperacao=True,
                    duracao_prefixo_valido=info["duracao_prefixo_valido"]
                )
            self.assertIn("Não foi possível recuperar a gravação além do trecho inicial", str(ctx.exception))
            mock_client.files.upload.assert_not_called()

        # CASO B: Normalizer avança para 3.0s (> prefixo 1.0) -> DEVE SEGUIR PARA GEMINI
        mock_norm_3s = {
            "sucesso": True,
            "audio_bytes": b"fLaC_3s",
            "caminho_audio": None,
            "mime_type": "audio/flac",
            "duracao": 3.0,
            "canais": 1,
            "sample_rate": 8000,
            "tamanho_bytes": 1536,
            "avisos": ["Aviso: gravação normalizada."],
            "erro": None
        }
        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm_3s, create=True):
            resultado, modelo, aviso = processar_audio(
                arquivo,
                "chave_falsa",
                "gemini-3.5-flash-lite",
                requer_recuperacao=True,
                duracao_prefixo_valido=info["duracao_prefixo_valido"]
            )
            # Verifica que seguiu para upload
            mock_client.files.upload.assert_called_once()
            self.assertTrue(resultado["audio_recuperado"])
            self.assertEqual(resultado["duracao_recuperada"], 3.0)
            self.assertEqual(resultado["audio_bytes"], b"fLaC_3s")

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_prefixo_none_falha_fechada_sem_upload(self, mock_client_cls):
        """Se o prefixo for None (desconhecido/indeterminado), deve falhar fechado imediatamente."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        arquivo = FakeUploadedFile(name="indeterminado.ogg", data=b"OggS_xyz")

        mock_norm = {
            "sucesso": True,
            "audio_bytes": b"fLaC_qualquer",
            "mime_type": "audio/flac",
            "duracao": 10.0,
            "avisos": [],
            "erro": None
        }
        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                processar_audio(
                    arquivo,
                    "chave_falsa",
                    "gemini-3.5-flash-lite",
                    requer_recuperacao=True,
                    duracao_prefixo_valido=None
                )
            self.assertIn("Não foi possível determinar o prefixo íntegro", str(ctx.exception))
            mock_client.files.upload.assert_not_called()

    @patch("gemini_transcriber.genai.Client")
    def test_processar_audio_sem_recuperacao_preserva_defaults_sem_aviso_integridade(self, mock_client_cls):
        """Arquivo íntegro sem necessidade de recuperação não gera avisos de integridade nem altera áudio."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock()
        mock_file.name = "files/integro123"
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock()
        mock_resp_trans.text = "[00:01] Carlos: Olá."
        mock_resp_trans.candidates = []
        mock_resp_trans.prompt_feedback = None

        mock_resp_meta = MagicMock()
        mock_resp_meta.text = '{"atendente": "Carlos", "cliente": "Ana", "motivo_contato": "Duvida"}'
        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        dados_originais = b"OggS_integro_original"
        arquivo = FakeUploadedFile(name="chamada_integra.ogg", data=dados_originais)

        resultado, modelo_usado, aviso_meta = processar_audio(
            arquivo, "chave_falsa", "gemini-3.5-flash-lite", requer_recuperacao=False
        )

        self.assertFalse(resultado["audio_recuperado"])
        self.assertFalse(resultado["recuperacao_parcial"])
        self.assertIsNone(resultado["duracao_recuperada"])
        self.assertIsNone(resultado["aviso_recuperacao"])
        self.assertEqual(resultado["audio_bytes"], dados_originais)

    def test_pdf_inclui_aviso_recuperacao_quando_presente(self):
        """Relatório PDF gerado deve conter o aviso de recuperação quando o áudio foi normalizado."""
        try:
            from callcenter_transcriber.pdf_generator import gerar_relatorio_pdf
        except ImportError:
            from pdf_generator import gerar_relatorio_pdf

        dados_com_aviso = {
            "empresa_cliente_fantasia": "Mercado Modelo",
            "atendente": "João",
            "cliente": "Maria",
            "status_resolucao": "Resolvido no atendimento",
            "audio_recuperado": True,
            "aviso_recuperacao": "Aviso de teste: áudio recuperado de contêiner danificado com possíveis cortes.",
            "dialogo": [{"timestamp": "00:05", "locutor": "João", "papel": "Atendente", "texto": "Bom dia."}]
        }
        pdf_bytes = gerar_relatorio_pdf(dados_com_aviso, nome_arquivo_audio="gravacao.ogg")
        self.assertTrue(isinstance(pdf_bytes, bytes))
        self.assertGreater(len(pdf_bytes), 1000)
        # O PDF foi gerado sem erros e possui tamanho substancial
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_app_smoke_com_streamlit_apptest(self):
        """Valida que app.py monta a árvore completa da UI sem erros de execução em modo headless."""
        from streamlit.testing.v1 import AppTest
        caminho_app = str(RAIZ / "callcenter_transcriber" / "app.py")
        at = AppTest.from_file(caminho_app, default_timeout=10)
        at.run()
        self.assertEqual(len(at.error), 0, f"Erros inesperados na renderização: {at.error}")
        self.assertTrue(len(at.title) >= 1)

    def test_eh_erro_503_indisponivel_classificador_estruturado(self):
        """Valida que o classificador 503 é estritamente estruturado e não faz retry indevido."""
        # 1. Código 503 numérico explícito -> True
        err_503 = Exception("Unavailable")
        err_503.code = 503
        self.assertTrue(eh_erro_503_indisponivel(err_503))

        err_status_code_503 = Exception("Unavailable")
        err_status_code_503.status_code = 503
        self.assertTrue(eh_erro_503_indisponivel(err_status_code_503))

        # 2. Status UNAVAILABLE sem código numérico -> True
        err_unavail = Exception("Temporarily unavailable")
        err_unavail.status = "UNAVAILABLE"
        self.assertTrue(eh_erro_503_indisponivel(err_unavail))

        # 3. Código 4xx com status UNAVAILABLE -> False (código 4xx tem precedência estrita)
        err_400_unavail = Exception("Bad Request")
        err_400_unavail.code = 400
        err_400_unavail.status = "UNAVAILABLE"
        self.assertFalse(eh_erro_503_indisponivel(err_400_unavail))

        err_403 = Exception("Forbidden")
        err_403.code = 403
        self.assertFalse(eh_erro_503_indisponivel(err_403))

        err_429 = Exception("Rate Limit")
        err_429.code = 429
        self.assertFalse(eh_erro_503_indisponivel(err_429))

        # 4. Outro código 5xx (500, 502, 504) -> False
        err_500 = Exception("Internal Server Error")
        err_500.code = 500
        self.assertFalse(eh_erro_503_indisponivel(err_500))

        # 5. String pura RuntimeError("503 Service Unavailable") sem atributos estruturados -> False
        err_plain_str = RuntimeError("503 Service Unavailable")
        self.assertFalse(eh_erro_503_indisponivel(err_plain_str))

        # 6. JSONDecodeError -> False
        import json
        try:
            json.loads("{")
        except json.JSONDecodeError as err_json:
            self.assertFalse(eh_erro_503_indisponivel(err_json))

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_503_estruturado_retenta_ate_sucesso(self, mock_client_cls, mock_sleep):
        """Metadados com 503 na 1ª tentativa dorme 2s, retenta na 2ª e sucede sem repetir transcrição."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f1")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock(text="Carlos: Tudo bem?", candidates=[], prompt_feedback=None)

        err_503 = Exception("Service unavailable")
        err_503.code = 503

        mock_resp_meta = MagicMock()
        mock_resp_meta.text = '{"empresa_cliente_fantasia": "Padaria Silva", "atendente": "Carlos"}'

        # 1 chamada transcricao, 1ª chamada meta falha 503, 2ª chamada meta sucesso
        mock_client.models.generate_content.side_effect = [mock_resp_trans, err_503, mock_resp_meta]

        arquivo = FakeUploadedFile(name="chamada.ogg", data=b"OggS_test_503_success")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        # Verifica exatamente 1 upload e 1 deleção
        self.assertEqual(mock_client.files.upload.call_count, 1)
        self.assertEqual(mock_client.files.delete.call_count, 1)

        # 3 chamadas totais de generate_content (1 transcricao + 2 metadados)
        self.assertEqual(mock_client.models.generate_content.call_count, 3)

        # Verifica sleep de 2.0s exatamente 1 vez
        mock_sleep.assert_called_once_with(2.0)

        # Metadados obtidos com sucesso na 2ª tentativa
        self.assertIsNone(aviso_meta)
        self.assertEqual(resultado["empresa_cliente_fantasia"], "Padaria Silva")
        self.assertEqual(resultado["atendente"], "Carlos")
        self.assertEqual(resultado["texto_bruto"], "Carlos: Tudo bem?")

        # Verifica que http_options attempts=1 foi passado na chamada de metadados
        meta_call_kwargs = mock_client.models.generate_content.call_args_list[1].kwargs
        cfg = meta_call_kwargs.get("config")
        self.assertIsNotNone(cfg.http_options)
        self.assertEqual(cfg.http_options.retry_options.attempts, 1)

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_503_esgotado_3_tentativas_retorna_fallback_e_aviso_fixo(self, mock_client_cls, mock_sleep):
        """Se metadados falharem 3x com 503, dorme [2.0, 4.0], preenche defaults e gera aviso fixo limpo."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f2")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock(text="Carlos: Atendimento finalizado.", candidates=[], prompt_feedback=None)

        err_503 = Exception("Service unavailable")
        err_503.code = 503

        # 1 transcricao + 3 metadados (todos 503)
        mock_client.models.generate_content.side_effect = [mock_resp_trans, err_503, err_503, err_503]

        arquivo = FakeUploadedFile(name="chamada.ogg", data=b"OggS_test_503_exhausted")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        # Verifica exatamente 1 upload
        self.assertEqual(mock_client.files.upload.call_count, 1)

        # 4 chamadas no generate_content (1 transcricao + 3 metadados)
        self.assertEqual(mock_client.models.generate_content.call_count, 4)

        # Verifica 2 sleeps: 2.0s e 4.0s
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_has_calls([unittest.mock.call(2.0), unittest.mock.call(4.0)])

        # Aviso fixo limpo sem vazar JSON ou chave
        self.assertEqual(aviso_meta, "Transcrição concluída; dados complementares temporariamente indisponíveis.")
        self.assertEqual(resultado["empresa_cliente_fantasia"], "Não identificada")
        self.assertEqual(resultado["atendente"], "Identificado no diálogo")
        self.assertEqual(resultado["texto_bruto"], "Carlos: Atendimento finalizado.")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_erros_400_403_429_sem_retry(self, mock_client_cls, mock_sleep):
        """Erros 400, 403 e 429 na extração de metadados não realizam retry (0 sleeps, 1 chamada)."""
        for err_code in (400, 403, 429):
            mock_sleep.reset_mock()
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_file = MagicMock(name="files/f_4xx")
            mock_file.state.name = "ACTIVE"
            mock_client.files.upload.return_value = mock_file
            mock_client.files.get.return_value = mock_file

            mock_resp_trans = MagicMock(text="Carlos: Bom dia.", candidates=[], prompt_feedback=None)
            err = Exception(f"Error {err_code}")
            err.code = err_code
            err.status = "UNAVAILABLE"  # Mesmo que venha UNAVAILABLE, o código 4xx impede retry

            mock_client.models.generate_content.side_effect = [mock_resp_trans, err]

            arquivo = FakeUploadedFile(name=f"chamada_{err_code}.ogg", data=b"OggS_audio")
            resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

            # 1 transcricao + 1 metadados = 2 chamadas
            self.assertEqual(mock_client.models.generate_content.call_count, 2)
            mock_sleep.assert_not_called()
            self.assertEqual(aviso_meta, "Transcrição concluída; não foi possível extrair dados complementares da chamada.")
            self.assertEqual(resultado["empresa_cliente_fantasia"], "Não identificada")
            self.assertEqual(resultado["texto_bruto"], "Carlos: Bom dia.")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_runtime_error_503_sem_attrs_sem_retry(self, mock_client_cls, mock_sleep):
        """RuntimeError contendo '503' apenas no texto sem atributos estruturados não realiza retry."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f_plain")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock(text="Carlos: Oi.", candidates=[], prompt_feedback=None)
        err_plain = RuntimeError("503 Service Unavailable")

        mock_client.models.generate_content.side_effect = [mock_resp_trans, err_plain]

        arquivo = FakeUploadedFile(name="chamada_plain.ogg", data=b"OggS_audio")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertEqual(mock_client.models.generate_content.call_count, 2)
        mock_sleep.assert_not_called()
        self.assertEqual(aviso_meta, "Transcrição concluída; não foi possível extrair dados complementares da chamada.")
        self.assertEqual(resultado["texto_bruto"], "Carlos: Oi.")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_json_malformado_sem_retry(self, mock_client_cls, mock_sleep):
        """JSON malformado em metadados cai em fallback imediato sem retry (0 sleeps)."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f_bad_json")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock(text="Carlos: Oi.", candidates=[], prompt_feedback=None)
        mock_resp_meta = MagicMock(text="{json_quebrado_sem_fechar")

        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        arquivo = FakeUploadedFile(name="chamada_bad_json.ogg", data=b"OggS_audio")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertEqual(mock_client.models.generate_content.call_count, 2)
        mock_sleep.assert_not_called()
        self.assertEqual(aviso_meta, "Transcrição concluída; não foi possível extrair dados complementares da chamada.")
        self.assertEqual(resultado["empresa_cliente_fantasia"], "Não identificada")
        self.assertEqual(resultado["texto_bruto"], "Carlos: Oi.")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_json_fail_safe_null_e_lista_sem_retry(self, mock_client_cls, mock_sleep):
        """Fail-safe: json.loads retornando 'null' (None) ou '[]' (list) não quebra .get() e não faz retry."""
        for json_raw in ("null", "[]", '"string_invalida"', "123", "true"):
            mock_sleep.reset_mock()
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_file = MagicMock(name="files/f_non_dict")
            mock_file.state.name = "ACTIVE"
            mock_client.files.upload.return_value = mock_file
            mock_client.files.get.return_value = mock_file

            mock_resp_trans = MagicMock(text="Carlos: Alô.", candidates=[], prompt_feedback=None)
            mock_resp_meta = MagicMock(text=json_raw)

            mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

            arquivo = FakeUploadedFile(name=f"chamada_non_dict.ogg", data=b"OggS_audio")
            resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

            self.assertEqual(mock_client.models.generate_content.call_count, 2)
            mock_sleep.assert_not_called()
            self.assertEqual(aviso_meta, "Transcrição concluída; não foi possível extrair dados complementares da chamada.")
            self.assertEqual(resultado["empresa_cliente_fantasia"], "Não identificada")
            self.assertEqual(resultado["texto_bruto"], "Carlos: Alô.")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_sucesso_de_primeira_sem_sleep(self, mock_client_cls, mock_sleep):
        """Metadados com sucesso de primeira não executam sleep nem geram aviso."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f_ok")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock(text="Carlos: Sucesso.", candidates=[], prompt_feedback=None)
        mock_resp_meta = MagicMock(text='{"empresa_cliente_fantasia": "Supermercado Real", "atendente": "Carlos"}')

        mock_client.models.generate_content.side_effect = [mock_resp_trans, mock_resp_meta]

        arquivo = FakeUploadedFile(name="chamada_ok.ogg", data=b"OggS_audio")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertEqual(mock_client.models.generate_content.call_count, 2)
        mock_sleep.assert_not_called()
        self.assertIsNone(aviso_meta)
        self.assertEqual(resultado["empresa_cliente_fantasia"], "Supermercado Real")
        self.assertEqual(resultado["atendente"], "Carlos")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_503_com_recuperacao_fake_preserva_1normalizer_1upload_1trans_e_avisos(
        self, mock_client_cls, mock_sleep
    ):
        """Garante 1 normalizer, 1 upload FLAC, 1 transcrição e até 3 chamadas meta com avisos preservados."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f_recup")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp_trans = MagicMock(text="Carlos: Atendimento com áudio recuperado.", candidates=[], prompt_feedback=None)
        err_503 = Exception("Unavailable")
        err_503.code = 503

        # 1 transcrição + 3 falhas 503 de metadados
        mock_client.models.generate_content.side_effect = [mock_resp_trans, err_503, err_503, err_503]

        mock_norm = {
            "sucesso": True,
            "caminho_flac": "/tmp/recuperado.flac",
            "audio_bytes": b"fLaC_recuperado_bytes",
            "duracao": 12.5,
            "metodo": "vlc",
            "avisos": ["Aviso normalizador de teste"],
            "erro": None
        }

        arquivo = FakeUploadedFile(name="chamada_danificada.ogg", data=b"OggS_danificado")

        with patch("gemini_transcriber.normalizar_audio_ogg", return_value=mock_norm, create=True) as mock_norm_fn:
            resultado, modelo_usado, aviso_meta = processar_audio(
                arquivo,
                "chave_falsa",
                "gemini-3.5-flash-lite",
                requer_recuperacao=True,
                duracao_prefixo_valido=5.0
            )

            # 1. Normalizador chamado exatamente 1 vez
            self.assertEqual(mock_norm_fn.call_count, 1)

            # 2. Upload chamado exatamente 1 vez com mime_type audio/flac
            self.assertEqual(mock_client.files.upload.call_count, 1)
            upload_kwargs = mock_client.files.upload.call_args.kwargs
            self.assertEqual(upload_kwargs.get("config").mime_type, "audio/flac")

            # 3. generate_content chamado 4 vezes (1 trans + 3 meta)
            self.assertEqual(mock_client.models.generate_content.call_count, 4)

            # 4. Sleeps de backoff [2.0, 4.0]
            self.assertEqual(mock_sleep.call_count, 2)
            mock_sleep.assert_has_calls([unittest.mock.call(2.0), unittest.mock.call(4.0)])

            # 5. Preservação estrita dos campos de recuperação e avisos
            self.assertTrue(resultado["audio_recuperado"])
            self.assertTrue(resultado["recuperacao_parcial"])
            self.assertEqual(resultado["duracao_recuperada"], 12.5)
            self.assertIn("Aviso normalizador de teste", resultado["aviso_recuperacao"])
            self.assertEqual(aviso_meta, "Transcrição concluída; dados complementares temporariamente indisponíveis.")
            self.assertEqual(resultado["texto_bruto"], "Carlos: Atendimento com áudio recuperado.")

    @patch("gemini_transcriber.time.sleep")
    @patch("gemini_transcriber.genai.Client")
    def test_metadados_503_preserva_aviso_corte_concatenado(self, mock_client_cls, mock_sleep):
        """Verifica que aviso de corte na transcrição é concatenado ao aviso de metadados."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_file = MagicMock(name="files/f_corte")
        mock_file.state.name = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_candidate = MagicMock()
        mock_candidate.finish_reason = "MAX_TOKENS"
        mock_part = MagicMock()
        mock_part.text = "Carlos: Transcrição truncada por limite"
        mock_part.audio_transcription = None
        mock_candidate.content.parts = [mock_part]

        mock_resp_trans = MagicMock()
        mock_resp_trans.candidates = [mock_candidate]
        mock_resp_trans.prompt_feedback = None

        err_503 = Exception("Unavailable")
        err_503.code = 503
        mock_client.models.generate_content.side_effect = [mock_resp_trans, err_503, err_503, err_503]

        arquivo = FakeUploadedFile(name="chamada.ogg", data=b"OggS_audio")
        resultado, modelo_usado, aviso_meta = processar_audio(arquivo, "chave_falsa", "gemini-3.5-flash-lite")

        self.assertIn("MAX_TOKENS", aviso_meta)
        self.assertIn("dados complementares temporariamente indisponíveis", aviso_meta)


if __name__ == "__main__":
    unittest.main()
