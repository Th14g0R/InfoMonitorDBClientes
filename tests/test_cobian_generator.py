"""Testes unitários do módulo gerador e serializador de listas Cobian 11 (cobian_generator)."""
import base64
import datetime
import os
import struct
import unittest

from cobian_generator import (
    codificar_horario_cobian,
    decodificar_horario_cobian,
    sanitizar_campo_cobian,
    validar_porta_cobian,
    validar_host_cobian,
    validar_base64_estrito,
    montar_tarefa_cobian,
    gerar_arquivo_cobian_lst,
    obter_preset_fbk,
    obter_preset_fdb,
    extrair_hash_senha_compativel,
)
from backups_ftp_admin import _validar_e_processar_lista


class CobianGeneratorTest(unittest.TestCase):
    def test_codificar_horario_exato_sem_magic_number(self):
        base_test = datetime.date(2026, 11, 13)
        bin_13h = codificar_horario_cobian('13:00', base_date=base_test)
        self.assertEqual(len(bin_13h), 64)
        self.assertTrue(set(bin_13h).issubset({'0', '1'}))

        dt_dec_13 = decodificar_horario_cobian(bin_13h)
        self.assertEqual(dt_dec_13.hour, 13)
        self.assertEqual(dt_dec_13.minute, 0)
        self.assertEqual(dt_dec_13.second, 0)

        bin_23h = codificar_horario_cobian('23:00', base_date=base_test)
        dt_dec_23 = decodificar_horario_cobian(bin_23h)
        self.assertEqual(dt_dec_23.hour, 23)
        self.assertEqual(dt_dec_23.minute, 0)
        self.assertEqual(dt_dec_23.second, 0)

    def test_decodificacao_amostras_reais_com_tolerancia_documentada(self):
        REAL_13H = '0100000011100110101000000111000101010101010101110101100100010100'
        REAL_23H = '0100000011100110101000000111111010101010101011001010111001101001'

        dt_real_13 = decodificar_horario_cobian(REAL_13H)
        self.assertEqual(dt_real_13.hour, 13)
        self.assertEqual(dt_real_13.minute, 0)
        self.assertLessEqual(dt_real_13.second, 1)

        dt_real_23 = decodificar_horario_cobian(REAL_23H)
        self.assertEqual(dt_real_23.hour, 23)
        self.assertEqual(dt_real_23.minute, 0)
        self.assertLessEqual(dt_real_23.second, 1)

    def test_codificar_horario_invalido_lanca_erro(self):
        for invalido in ('25:00', '12:60', 'abc', '12', '', '  ', '12:00:00'):
            with self.assertRaises(ValueError):
                codificar_horario_cobian(invalido)
        with self.assertRaises(TypeError):
            codificar_horario_cobian(1200)

    def test_sanitizacao_estrita_aspas_virgulas_e_caracteres_de_controle(self):
        # 1. Rejeição de quebras de linha e controles ASCII
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\r\nInjetado")
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\nInjetado")
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\x00Nulo")

        # 2. Rejeição de caracteres de controle estendidos: DEL (\x7f), NEL (\x85), Line/Paragraph Sep (\u2028, \u2029)
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\x7fDEL")
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\x85NEL")
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\u2028SepLinha")
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Nome\u2029SepParag")

        # 3. Rejeição obrigatória de aspas duplas (impede fuga de sintaxe Cobian)
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian('Nome"ComAspas')
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian('C:\\Infobrasil\\"dados')

        # 4. Rejeição de vírgula em campos CSV embutidos
        with self.assertRaises(ValueError):
            sanitizar_campo_cobian("Servico,Injetado", permitir_virgula=False)
        # Permite quando explicitamente permitido
        self.assertEqual(sanitizar_campo_cobian("Texto,Permitido", permitir_virgula=True), "Texto,Permitido")

        # 5. Rejeição de coerção silenciosa (tipo não str)
        with self.assertRaises(TypeError):
            sanitizar_campo_cobian(12345)
        with self.assertRaises(TypeError):
            sanitizar_campo_cobian(True)

        # 6. Texto válido aceito
        limpo = sanitizar_campo_cobian("Tarefa Principal - Matriz")
        self.assertEqual(limpo, "Tarefa Principal - Matriz")

    def test_validacao_porta_e_host(self):
        self.assertEqual(validar_porta_cobian(8821), 8821)
        self.assertEqual(validar_porta_cobian('8821'), 8821)
        with self.assertRaises(ValueError):
            validar_porta_cobian(0)
        with self.assertRaises(ValueError):
            validar_porta_cobian(65536)
        with self.assertRaises(ValueError):
            validar_porta_cobian('porta')
        with self.assertRaises(TypeError):
            validar_porta_cobian(True)

        self.assertEqual(validar_host_cobian('ims.iprojectti.com.br'), 'ims.iprojectti.com.br')
        self.assertEqual(validar_host_cobian('192.168.1.100'), '192.168.1.100')
        with self.assertRaises(ValueError):
            validar_host_cobian('host com espaco')
        with self.assertRaises(ValueError):
            validar_host_cobian('host,com,virgula')

    def test_gramatica_estrita_espacos_chave_igual_valor(self):
        dados = {
            'alias': 'teste-123',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'teste-123',
            'diretorio_inicial': '/',
            'hash_senha': '',
            'tarefas': [
                {
                    'nome': 'IMS-teste-123-13h',
                    'ativo': True,
                    'origem': r'C:\Infobrasil\infoBackup\backup\teste-123\backup.fbk',
                    'horario': '13:00',
                    'tipo_origem': 'fbk',
                    'pre_evento_tipo': 'execute',
                    'pre_evento_caminho': r'C:\Infobrasil\infoBackup\infoBackup.exe',
                    'pre_evento_pausa': 1800,
                    'pos_evento_tipo': 'nenhum',
                    'pos_evento_caminho': '',
                    'comentario': 'teste-123',
                }
            ],
        }
        bytes_saida = gerar_arquivo_cobian_lst(dados, base_date=datetime.date(2026, 11, 13))
        self.assertTrue(bytes_saida.startswith(b'\xff\xfe'))
        texto = bytes_saida[2:].decode('utf-16le')
        linhas = texto.splitlines()
        self.assertEqual(linhas[0], '{11//')
        self.assertEqual(linhas[-1], '//11}')

        linhas_props = [l for l in linhas[1:-1] if l.strip()]
        for linha in linhas_props:
            self.assertRegex(linha, r'^[^\r\n=]*[^\s=]=([^\s].*)?$', f"Linha com espaços ou fora do padrão 'Chave=Valor': {linha}")

    def test_arquivo_gerado_passa_no_validador_oficial(self):
        fake_hash = base64.b64encode(b"fictitious_password_12345").decode('ascii')
        dados = {
            'alias': 'cliente-validador',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'cliente-validador',
            'diretorio_inicial': '/',
            'hash_senha': fake_hash,
            'tarefas': obter_preset_fbk('cliente-validador'),
        }
        bytes_saida = gerar_arquivo_cobian_lst(dados, base_date=datetime.date(2026, 11, 13))
        texto_dec, enc, sha = _validar_e_processar_lista(bytes_saida, 'cliente-validador.lst')
        self.assertEqual(enc, 'UTF-16LE')
        self.assertIn('Name=IMS-cliente-validador-13h', texto_dec)
        self.assertIn('Name=IMS-cliente-validador-23h', texto_dec)
        self.assertIn('FTP Host=ims.iprojectti.com.br', texto_dec)
        self.assertIn('FTP Port=8821', texto_dec)
        self.assertIn('FTP User name=cliente-validador', texto_dec)
        self.assertIn(f'FTP Password={fake_hash}', texto_dec)

    def test_presets_fbk_e_fdb(self):
        fbk = obter_preset_fbk('alfa')
        self.assertEqual(len(fbk), 2)
        self.assertEqual(fbk[0]['horario'], '13:00')
        self.assertEqual(fbk[1]['horario'], '23:00')
        self.assertIn('infoBackup.exe', fbk[0]['pre_evento_caminho'])
        self.assertEqual(fbk[0]['pre_evento_pausa'], 1800)

        fdb = obter_preset_fdb('beta')
        self.assertEqual(len(fdb), 1)
        self.assertEqual(fdb[0]['horario'], '23:00')
        self.assertEqual(fdb[0]['pre_evento_tipo'], 'stop_service')
        self.assertEqual(fdb[0]['pos_evento_tipo'], 'start_service')
        self.assertIn('FirebirdServerDefaultInstance', fdb[0]['pre_evento_caminho'])

    def test_validacao_tipos_estritos_na_montagem_da_tarefa(self):
        t_base = {
            'nome': 'Tarefa Valida',
            'ativo': True,
            'origem': r'C:\Infobrasil\backup.fbk',
            'horario': '13:00',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'usr',
        }
        # 1. ativo deve ser bool estrito
        with self.assertRaises(TypeError):
            montar_tarefa_cobian(dict(t_base, ativo=1))
        with self.assertRaises(TypeError):
            montar_tarefa_cobian(dict(t_base, ativo='true'))

        # 2. pre_evento_pausa deve ser int (não bool, não str) e em 0..86400
        with self.assertRaises(TypeError):
            montar_tarefa_cobian(dict(t_base, pre_evento_pausa=True))
        with self.assertRaises(TypeError):
            montar_tarefa_cobian(dict(t_base, pre_evento_pausa='1800'))
        with self.assertRaises(ValueError):
            montar_tarefa_cobian(dict(t_base, pre_evento_pausa=-1))
        with self.assertRaises(ValueError):
            montar_tarefa_cobian(dict(t_base, pre_evento_pausa=90000))

        # 3. pre_evento_tipo allowlist e caminho obrigatório
        with self.assertRaises(ValueError):
            montar_tarefa_cobian(dict(t_base, pre_evento_tipo='tipo_invalido'))
        with self.assertRaises(ValueError):
            montar_tarefa_cobian(dict(t_base, pre_evento_tipo='execute', pre_evento_caminho=''))

        # 4. pos_evento_tipo allowlist e caminho obrigatório
        with self.assertRaises(ValueError):
            montar_tarefa_cobian(dict(t_base, pos_evento_tipo='invalido'))
        with self.assertRaises(ValueError):
            montar_tarefa_cobian(dict(t_base, pos_evento_tipo='start_service', pos_evento_caminho=''))

        # 5. Rejeição de override de destino dentro da tarefa no gerador global
        with self.assertRaises(ValueError):
            gerar_arquivo_cobian_lst({
                'alias': 'alfa',
                'host': 'ims.iprojectti.com.br',
                'porta': '8821',
                'usuario': 'alfa',
                'tarefas': [dict(t_base, host='tentativa_override.com')]
            })

    def test_extrair_hash_senha_compativel_tripla_correspondencia_e_base64(self):
        fake_pwd_48 = base64.b64encode(b"fictitious_password_for_extrair_hash").decode('ascii')
        dados = {
            'alias': 'cliente-hash',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'cliente-hash',
            'diretorio_inicial': '/',
            'hash_senha': fake_pwd_48,
            'tarefas': obter_preset_fbk('cliente-hash'),
        }
        texto_lst = gerar_arquivo_cobian_lst(dados, base_date=datetime.date(2026, 11, 13))[2:].decode('utf-16le')

        # 1. Correspondência exata: host + porta + usuario
        h = extrair_hash_senha_compativel(texto_lst, 'ims.iprojectti.com.br', '8821', 'cliente-hash')
        self.assertEqual(h, fake_pwd_48)

        # 2. Porta diferente: recusa
        with self.assertRaises(ValueError):
            extrair_hash_senha_compativel(texto_lst, 'ims.iprojectti.com.br', '21', 'cliente-hash')

        # 3. Usuário diferente: recusa
        with self.assertRaises(ValueError):
            extrair_hash_senha_compativel(texto_lst, 'ims.iprojectti.com.br', '8821', 'outro-usuario')

        # 4. Host diferente: recusa
        with self.assertRaises(ValueError):
            extrair_hash_senha_compativel(texto_lst, 'outro.host.com.br', '8821', 'cliente-hash')

        # 5. Hash vazio na lista anterior: recusa
        dados_vazio = dict(dados, hash_senha='')
        texto_vazio = gerar_arquivo_cobian_lst(dados_vazio, base_date=datetime.date(2026, 11, 13))[2:].decode('utf-16le')
        with self.assertRaises(ValueError):
            extrair_hash_senha_compativel(texto_vazio, 'ims.iprojectti.com.br', '8821', 'cliente-hash')

        # 6. Hash com caracteres não-Base64: recusa
        dados_bad_b64 = dict(dados, hash_senha='hash*invalido!com$pontos')
        with self.assertRaises(ValueError):
            gerar_arquivo_cobian_lst(dados_bad_b64)

    def test_comparacao_integral_com_fixture_sanitizada_versionada(self):
        caminho_fixture = os.path.join(os.path.dirname(__file__), 'fixtures', 'amostra_cobian_sanitizada.lst')
        self.assertTrue(os.path.exists(caminho_fixture), f"Fixture sanitizada não encontrada em {caminho_fixture}")

        with open(caminho_fixture, 'rb') as f:
            fixture_bytes = f.read()

        fixture_txt = fixture_bytes[2:].decode('utf-16le')

        # Extrai propriedades da tarefa 1 da fixture
        props_fixture = {}
        for ln in fixture_txt.splitlines()[1:]:
            if ln.strip() == '//11}':
                break
            if '=' in ln:
                k, v = ln.split('=', 1)
                props_fixture[k.strip()] = v.strip()

        # Gera senha sintética de 200 chars a partir de dados fictícios (sem segredos reais)
        fake_bytes = (b"QA_FICTITIOUS_COBIAN_PASSWORD_TEST_ONLY_1234567890_ABCD" * 3)[:150]
        synthetic_pwd_200 = base64.b64encode(fake_bytes).decode('ascii')

        # Gera com mesma data base injetável (2026-11-13)
        dados = {
            'alias': 'fake-user-qa',
            'host': 'qa.example.invalid',
            'porta': '8821',
            'usuario': 'fake-user-qa',
            'diretorio_inicial': '/',
            'hash_senha': synthetic_pwd_200,
            'tarefas': obter_preset_fbk('fake-user-qa'),
        }
        bytes_gerados = gerar_arquivo_cobian_lst(dados, base_date=datetime.date(2026, 11, 13))
        txt_gerado = bytes_gerados[2:].decode('utf-16le')

        props_geradas = {}
        for ln in txt_gerado.splitlines()[1:]:
            if ln.strip() == '//11}':
                break
            if '=' in ln:
                k, v = ln.split('=', 1)
                props_geradas[k.strip()] = v.strip()

        # 1. Checa presença de todas as chaves estruturais
        chaves_essenciais = [
            'Id', 'Name', 'Group', 'Enabled', 'Include subdirectories', 'Create separated backups',
            'Use attributes', 'Use VSC', 'Backup type', 'Priority', 'Source', 'Destination',
            'Schedule type', 'Select days of the week', 'Order of Day of the week', 'Day of the week',
            'Date/Time', 'Day of the month', 'Month', 'Timer', 'Timer from', 'Timer to',
            'Full copies', 'Differental copies', 'One full every', 'Use fixed day', 'Fixed day',
            'Compression', 'Compress individually', 'Split', 'Custom size', 'Comment',
            'Encryption', 'Pre backup events', 'Post backup events', 'Clear archive attribute'
        ]
        for k in chaves_essenciais:
            self.assertIn(k, props_geradas)

        # 2. Valores fixos coincidem
        self.assertEqual(props_geradas['Name'], 'IMS-fake-user-qa-13h')
        self.assertEqual(props_geradas['Group'], 'IMS')
        self.assertEqual(props_geradas['Enabled'], 'true')
        self.assertEqual(props_geradas['Date/Time'], props_fixture['Date/Time'])
        self.assertEqual(props_geradas['Timer from'], props_fixture['Timer from'])
        self.assertEqual(props_geradas['Timer to'], props_fixture['Timer to'])
        self.assertEqual(props_geradas['Destination'], props_fixture['Destination'])
        self.assertEqual(props_geradas['Pre backup events'], props_fixture['Pre backup events'])

    def test_validar_base64_estrito_rejeita_comprimento_1_e_invalidos(self):
        """Validação estrita de Base64 com b64decode(validate=True) e verificação canônica."""
        # 1. Comprimento 1: inválido
        with self.assertRaises(ValueError):
            validar_base64_estrito('A')

        # 2. Comprimento não múltiplo de 4: inválido
        with self.assertRaises(ValueError):
            validar_base64_estrito('AA')
        with self.assertRaises(ValueError):
            validar_base64_estrito('AAA')
        with self.assertRaises(ValueError):
            validar_base64_estrito('AAAAA')

        # 3. Padding inválido ou não canônico
        with self.assertRaises(ValueError):
            validar_base64_estrito('A===')
        with self.assertRaises(ValueError):
            validar_base64_estrito('AB==')  # Bits de padding não-zero

        # 4. String vazia ou tipo incorreto
        with self.assertRaises(ValueError):
            validar_base64_estrito('')
        with self.assertRaises(TypeError):
            validar_base64_estrito(12345)

        # 5. Base64 canônico válido de comprimentos variados: sucesso
        valido_4 = 'AAAA'
        self.assertEqual(validar_base64_estrito(valido_4), valido_4)
        valido_pad = 'AA=='
        self.assertEqual(validar_base64_estrito(valido_pad), valido_pad)

    def test_extrair_hash_senha_rejeita_destinos_duplicados_ambiguos_e_sem_ftp(self):
        """extrair_hash_senha_compativel rejeita duplicações/ambiguidades de host/destino e tarefas sem FTP."""
        # Gera lista base válida com duas tarefas
        fake_b64 = 'AAAA'
        dados = {
            'alias': 'teste-dupl',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'cli-dupl',
            'diretorio_inicial': '/',
            'hash_senha': fake_b64,
            'tarefas': obter_preset_fbk('teste-dupl'),
        }
        bytes_ok = gerar_arquivo_cobian_lst(dados, base_date=datetime.date(2026, 11, 13))
        txt_ok = bytes_ok[2:].decode('utf-16le')

        # 1. Base válida extrai hash sem erro
        h = extrair_hash_senha_compativel(txt_ok, 'ims.iprojectti.com.br', '8821', 'cli-dupl')
        self.assertEqual(h, fake_b64)

        # 2. Inserção de segundo FTP Host na MESMA Destination -> recusa imediata
        txt_inj_host = txt_ok.replace(
            'FTP Host=ims.iprojectti.com.br',
            'FTP Host=ims.iprojectti.com.br""""",""""FTP Host=other.example'
        )
        with self.assertRaises(ValueError) as ctx1:
            extrair_hash_senha_compativel(txt_inj_host, 'ims.iprojectti.com.br', '8821', 'cli-dupl')
        self.assertIn('duplicados ou ambíguos', str(ctx1.exception))

        # 3. Inserção de segunda linha Destination na mesma tarefa -> recusa
        txt_inj_dest = txt_ok.replace(
            'Destination="2,',
            'Destination="2,fake"\r\nDestination="2,'
        )
        with self.assertRaises(ValueError) as ctx2:
            extrair_hash_senha_compativel(txt_inj_dest, 'ims.iprojectti.com.br', '8821', 'cli-dupl')
        self.assertIn('múltiplas linhas de destino', str(ctx2.exception))

        # 4. Tarefa sem destino FTP (destino local "0,C:\\backup") -> recusa
        txt_sem_ftp = txt_ok.replace(
            'Destination="2,',
            'Destination="0,C:\\backup\\destino"'
        )
        with self.assertRaises(ValueError) as ctx3:
            extrair_hash_senha_compativel(txt_sem_ftp, 'ims.iprojectti.com.br', '8821', 'cli-dupl')
        self.assertIn('não possui destino FTP válido', str(ctx3.exception))

        # 5. Tentativa de gerar lista com hash inválido de comprimento 1 (hash_senha='A') -> recusa
        dados_a = dict(dados, hash_senha='A')
        with self.assertRaises(ValueError) as ctx4:
            gerar_arquivo_cobian_lst(dados_a)
        self.assertIn('múltiplo de 4', str(ctx4.exception))

    def test_comentario_tarefa_permite_virgula_e_pontuacao_segura(self):
        """Comentário da tarefa (Comment) é texto fora de CSV e deve permitir vírgula e pontuação natural."""
        dados = {
            'alias': 'cli-coment',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'cli-coment',
            'diretorio_inicial': '/',
            'hash_senha': 'AAAA',
            'tarefas': [{
                'nome': 'Tarefa Com Virgula',
                'ativo': True,
                'origem': 'C:\\dados\\backup.fbk',
                'horario': '13:00',
                'tipo_origem': 'fbk',
                'comentario': 'Observação natural, com vírgulas, pontos e hífen - ok!',
            }]
        }
        bytes_out = gerar_arquivo_cobian_lst(dados)
        txt_out = bytes_out[2:].decode('utf-16le')
        self.assertIn('Comment=Observação natural, com vírgulas, pontos e hífen - ok!', txt_out)

        # Aspas duplas continuam proibidas em comentário
        dados_aspas = dict(dados, tarefas=[dict(dados['tarefas'][0], comentario='Com "aspas" proibidas')])
        with self.assertRaises(ValueError):
            gerar_arquivo_cobian_lst(dados_aspas)

        # Quebra de linha continua proibida em comentário
        dados_crlf = dict(dados, tarefas=[dict(dados['tarefas'][0], comentario='Com\r\nquebra de linha')])
        with self.assertRaises(ValueError):
            gerar_arquivo_cobian_lst(dados_crlf)


if __name__ == '__main__':
    unittest.main()
