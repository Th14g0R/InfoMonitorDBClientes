import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from escalas import (migrar_horarios, aplicar_troca, jornada_no_dia, intervalos_cobertura,
                     ajustar_escala, preservar_banco_antes_migracao, registrar_substituicao,
                     obter_proximo_domingo, gerar_rodizio_domingo)


class EscalasTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE cargos (id INTEGER PRIMARY KEY, nome TEXT);
            CREATE TABLE funcionarios (id INTEGER PRIMARY KEY, nome TEXT, ativo INTEGER DEFAULT 1,
                cargo_id INTEGER, jornada_id INTEGER, foto_url TEXT);
            CREATE TABLE jornadas (id INTEGER PRIMARY KEY, descricao TEXT, tipo TEXT,
                manha_inicio TEXT, manha_fim TEXT, almoco_inicio TEXT DEFAULT '', almoco_fim TEXT DEFAULT '',
                tarde_inicio TEXT DEFAULT '', tarde_fim TEXT DEFAULT '');
            CREATE TABLE escala_sabado (id INTEGER PRIMARY KEY, data_sabado TEXT, funcionario_id INTEGER,
                horario TEXT, observacao TEXT);
            CREATE TABLE trocas_sabado (id INTEGER PRIMARY KEY, data_sabado TEXT,
                funcionario_substituido_id INTEGER, funcionario_substituto_id INTEGER, motivo TEXT);
            INSERT INTO jornadas (id, descricao, tipo, manha_inicio, manha_fim) VALUES
                (1, 'Manhã', 'Semana', '08:00', '12:00'), (2, 'Tarde', 'Semana', '13:00', '17:00');
            INSERT INTO funcionarios (id, nome, jornada_id) VALUES (1, 'Ana', 1), (2, 'Bruno', 2);
            INSERT INTO trocas_sabado (data_sabado, motivo) VALUES ('2026-09-12', 'Histórico anterior');
        ''')
        migrar_horarios(self.conn)
        self.conn.commit()

    def test_substituicao_periodo_preserva_sabado_e_retorna_ao_suporte(self):
        self.conn.executescript("""
            INSERT INTO cargos VALUES (1, 'Comercial'), (2, 'Suporte Técnico');
            UPDATE funcionarios SET cargo_id = id;
            INSERT INTO escala_sabado VALUES (1, '2026-09-19', 2, '09:00 - 13:00', '');
        """)
        registrar_substituicao(self.conn, 1, 2, '2026-09-14', '2026-10-13', 'Férias', 9)
        self.assertEqual(jornada_no_dia(self.conn, 2, '2026-09-14')[:2], ('08:00', '12:00'))
        self.assertEqual(intervalos_cobertura(self.conn, '2026-09-14'), [])
        self.assertEqual(intervalos_cobertura(self.conn, '2026-09-19')[0][3], [('09:00', '13:00')])
        self.assertEqual(intervalos_cobertura(self.conn, '2026-10-14')[0][3], [('13:00', '17:00')])
        self.assertEqual(self.conn.execute('SELECT jornada_id, cargo_id FROM funcionarios WHERE id=2').fetchone(), (2, 2))
        with self.assertRaises(ValueError):
            registrar_substituicao(self.conn, 1, 2, '2026-10-13', '2026-10-15', 'Conflito', 9)
        with self.assertRaises(ValueError):
            aplicar_troca(self.conn, '2026-09-15', 1, 2, 'Conflito', 9)

    def test_migracao_preserva_dados_e_nao_duplica_jornada(self):
        migrar_horarios(self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM funcionarios').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jornadas').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT aplicada FROM trocas_sabado').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT SUM(nao_trabalha_sabado) FROM funcionarios').fetchone()[0], 0)

    def test_troca_da_semana_nao_altera_jornada_padrao(self):
        aplicar_troca(self.conn, '2026-09-14', 1, 2, 'Troca', 9)
        self.assertEqual(jornada_no_dia(self.conn, 1, '2026-09-14')[:2], ('13:00', '17:00'))
        self.assertEqual(jornada_no_dia(self.conn, 1, '2026-09-15')[:2], ('08:00', '12:00'))
        self.assertEqual(self.conn.execute('SELECT registrado_por FROM trocas_sabado WHERE aplicada = 1').fetchone()[0], 9)
        aplicar_troca(self.conn, '2026-09-14', 1, 2, 'Desfaz a troca', 9)
        self.assertEqual(jornada_no_dia(self.conn, 1, '2026-09-14')[:2], ('08:00', '12:00'))

    def test_sabado_substitui_titular_e_usa_escala_na_cobertura(self):
        self.conn.execute("INSERT INTO escala_sabado VALUES (1, '2026-09-19', 1, '09:00 - 13:00', '')")
        aplicar_troca(self.conn, '2026-09-19', 1, 2, 'Substituição', 9)
        cobertura = {row[0]: row[3] for row in intervalos_cobertura(self.conn, '2026-09-19')}
        self.assertEqual(cobertura[1], [])
        self.assertEqual(cobertura[2], [('09:00', '13:00')])

    def test_sabado_troca_turnos_quando_os_dois_estao_escalados(self):
        self.conn.executescript("""INSERT INTO escala_sabado VALUES
            (1, '2026-09-19', 1, '08:00 - 12:00', ''),
            (2, '2026-09-19', 2, '14:00 - 18:00', '');""")
        aplicar_troca(self.conn, '2026-09-19', 1, 2, 'Troca de turno', 9)
        self.assertEqual(self.conn.execute('SELECT funcionario_id FROM escala_sabado ORDER BY id').fetchall(), [(2,), (1,)])

    def test_rejeita_troca_de_sabado_para_quem_nao_trabalha(self):
        self.conn.execute("INSERT INTO escala_sabado VALUES (1, '2026-09-19', 1, '08:00 - 12:00', '')")
        self.conn.execute('UPDATE funcionarios SET nao_trabalha_sabado = 1 WHERE id = 2')
        with self.assertRaises(ValueError):
            aplicar_troca(self.conn, '2026-09-19', 1, 2, 'Inválida', 9)
        self.assertEqual(self.conn.execute('SELECT funcionario_id FROM escala_sabado').fetchone()[0], 1)

    def test_ajuste_direto_registra_antes_e_depois(self):
        self.conn.execute("INSERT INTO escala_sabado VALUES (1, '2026-09-19', 1, '08:00 - 12:00', '')")
        ajustar_escala(self.conn, 1, None, '14:00 - 18:00', None, 9)
        detalhes = self.conn.execute('SELECT detalhes FROM trocas_sabado WHERE aplicada = 1').fetchone()[0]
        self.assertIn('08:00 - 12:00 → 14:00 - 18:00', detalhes)
        self.assertEqual(intervalos_cobertura(self.conn, '2026-09-19')[0][3], [('14:00', '18:00')])

    def test_domingo_nao_usa_jornada_da_semana(self):
        self.assertTrue(all(not row[3] for row in intervalos_cobertura(self.conn, '2026-09-20')))

    def test_intervalos_cobertura_feriado_sem_expediente(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS eventos (id INTEGER PRIMARY KEY, data_evento TEXT, nome TEXT, tipo TEXT, hora_inicio TEXT, hora_fim TEXT);
            CREATE TABLE IF NOT EXISTS evento_participantes (id INTEGER PRIMARY KEY, evento_id INTEGER, funcionario_id INTEGER);
            INSERT INTO eventos (id, data_evento, nome, tipo) VALUES (1, '2026-09-14', 'Feriado Nacional', 'feriado_sem_expediente');
        """)
        resultado = intervalos_cobertura(self.conn, '2026-09-14')
        self.assertTrue(resultado)
        self.assertTrue(all(not row[3] for row in resultado))

    def test_intervalos_cobertura_feriado_escala_reduzida(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS eventos (id INTEGER PRIMARY KEY, data_evento TEXT, nome TEXT, tipo TEXT, hora_inicio TEXT, hora_fim TEXT);
            CREATE TABLE IF NOT EXISTS evento_participantes (id INTEGER PRIMARY KEY, evento_id INTEGER, funcionario_id INTEGER);
            INSERT INTO eventos (id, data_evento, nome, tipo, hora_inicio, hora_fim) VALUES
                (2, '2026-09-15', 'Plantão Feriado', 'feriado_escala_reduzida', '08:00', '12:00');
            INSERT INTO evento_participantes (evento_id, funcionario_id) VALUES (2, 1);
        """)
        resultado = dict((row[0], row[3]) for row in intervalos_cobertura(self.conn, '2026-09-15'))
        self.assertEqual(resultado[1], [('08:00', '12:00')])
        self.assertEqual(resultado[2], [])

    def test_backup_copia_banco_antigo_sem_alterar_origem(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix='.test-security-backup-') as pasta:
            origem = Path(pasta) / 'sistema.db'
            with closing(sqlite3.connect(origem)) as conn, conn:
                conn.execute('CREATE TABLE funcionarios (id INTEGER, nome TEXT)')
                conn.execute("INSERT INTO funcionarios VALUES (1, 'Registro preservado')")
            preservar_banco_antes_migracao(origem)
            copias = list((Path(pasta) / 'backups').glob('*.db'))
            self.assertEqual(len(copias), 1)
            with closing(sqlite3.connect(copias[0])) as conn:
                self.assertEqual(conn.execute('SELECT nome FROM funcionarios').fetchone()[0], 'Registro preservado')
            with closing(sqlite3.connect(origem)) as conn:
                self.assertEqual(len(conn.execute('PRAGMA table_info(funcionarios)').fetchall()), 2)

    def test_obter_proximo_domingo(self):
        # 2026-10-16 é uma sexta-feira; o próximo domingo é 2026-10-18
        self.assertEqual(obter_proximo_domingo('2026-10-16'), '2026-10-18')
        # 2026-10-18 já é um domingo
        self.assertEqual(obter_proximo_domingo('2026-10-18'), '2026-10-18')

    def test_gerar_rodizio_domingo_rotacao_e_ausencias(self):
        # Cria 3 funcionários que participam do rodízio de domingo (TEF)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS ausencias (
                id INTEGER PRIMARY KEY, funcionario_id INTEGER, data_inicio TEXT, data_fim TEXT, motivo TEXT
            );
            UPDATE funcionarios SET participa_sobreaviso_domingo = 1 WHERE id IN (1, 2);
            INSERT INTO funcionarios (id, nome, ativo, prioridade, participa_sobreaviso_domingo)
            VALUES (3, 'Carlos', 1, 'P1', 1);
        """)
        # Domingo 1: 2026-10-18
        total = gerar_rodizio_domingo(self.conn, '2026-10-18', qtd_domingos=3, autor='admin')
        self.assertEqual(total, 3)

        escalas = self.conn.execute("SELECT data_domingo, funcionario_id FROM escala_domingo ORDER BY data_domingo").fetchall()
        # Ordenados por prioridade / nome: 1 (Ana), 2 (Bruno), 3 (Carlos)
        self.assertEqual(escalas, [
            ('2026-10-18', 1),
            ('2026-10-25', 2),
            ('2026-11-01', 3)
        ])

        # Agora cadastra ausência para Bruno (id=2) no segundo domingo (2026-10-25)
        self.conn.execute("INSERT INTO ausencias (funcionario_id, data_inicio, data_fim, motivo) VALUES (2, '2026-10-24', '2026-10-26', 'Férias')")
        # Regenera a partir de 2026-10-25
        gerar_rodizio_domingo(self.conn, '2026-10-25', qtd_domingos=1, autor='admin')
        # Deve pular Bruno (id=2) e escalar Carlos (id=3)
        row = self.conn.execute("SELECT funcionario_id FROM escala_domingo WHERE data_domingo = '2026-10-25'").fetchone()
        self.assertEqual(row[0], 3)

    def test_gerar_rodizio_domingo_validacoes(self):
        # Data que não é domingo
        with self.assertRaises(ValueError):
            gerar_rodizio_domingo(self.conn, '2026-10-17')  # Sábado
        # Nenhum participante com participa_sobreaviso_domingo = 1
        with self.assertRaises(ValueError):
            gerar_rodizio_domingo(self.conn, '2026-10-18')
