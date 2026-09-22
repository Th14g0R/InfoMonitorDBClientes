import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

EQUIPES = ('Amarela', 'Verde', 'Estágio', 'Sem Equipe')
TIPOS_SABADO = {
    'nenhum': 'Nenhum — não trabalha aos sábados',
    'rodizio': 'Rodízio Normal (4h no Sábado)',
    'apoio': 'Apoio Fixo Dev/Com/Fin (8h-12h)',
    'sobreaviso': 'Sobreaviso da Equipe',
}
FAIXAS_SABADO = ('07:30 - 11:30', '08:00 - 12:00', '09:00 - 13:00',
                 '10:00 - 14:00', '13:00 - 17:00', '14:00 - 18:00', 'Sobreaviso')


def preservar_banco_antes_migracao(caminho):
    origem = Path(caminho).resolve()
    if not origem.exists():
        return
    with closing(sqlite3.connect(origem.as_uri() + '?mode=ro', uri=True)) as conn:
        colunas = {row[1] for row in conn.execute('PRAGMA table_info(funcionarios)')}
        if colunas and 'nao_trabalha_sabado' not in colunas:
            pasta = origem.parent / 'backups'
            pasta.mkdir(exist_ok=True)
            destino = pasta / f'sistema-antes-ajustes-horarios-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
            with closing(sqlite3.connect(destino)) as backup:
                conn.backup(backup)


def migrar_horarios(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS substituicoes_periodo (
        id INTEGER PRIMARY KEY, titular_id INTEGER NOT NULL, substituto_id INTEGER NOT NULL,
        data_inicio TEXT NOT NULL, data_fim TEXT NOT NULL, motivo TEXT NOT NULL,
        registrado_por INTEGER NOT NULL, cargo_id INTEGER,
        manha_inicio TEXT NOT NULL, manha_fim TEXT NOT NULL,
        tarde_inicio TEXT NOT NULL, tarde_fim TEXT NOT NULL
    )''')
    colunas = {row[1] for row in conn.execute('PRAGMA table_info(funcionarios)')}
    if 'nao_trabalha_sabado' not in colunas:
        conn.execute('ALTER TABLE funcionarios ADD COLUMN nao_trabalha_sabado INTEGER NOT NULL DEFAULT 0')
    colunas = {row[1] for row in conn.execute('PRAGMA table_info(trocas_sabado)')}
    for nome, tipo in (('detalhes', 'TEXT'), ('aplicada', 'INTEGER NOT NULL DEFAULT 0'),
                       ('registrado_por', 'INTEGER')):
        if nome not in colunas:
            conn.execute(f'ALTER TABLE trocas_sabado ADD COLUMN {nome} {tipo}')
    conn.execute('''CREATE TABLE IF NOT EXISTS jornadas_dia (
        data TEXT NOT NULL, funcionario_id INTEGER NOT NULL,
        manha_inicio TEXT NOT NULL, manha_fim TEXT NOT NULL,
        tarde_inicio TEXT NOT NULL DEFAULT '', tarde_fim TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (data, funcionario_id)
    )''')
    conn.execute('''INSERT INTO jornadas
        (descricao, tipo, manha_inicio, manha_fim, almoco_inicio, almoco_fim, tarde_inicio, tarde_fim)
        SELECT '08:00 às 12:00 (Meio período)', 'Semana', '08:00', '12:00', '', '', '', ''
        WHERE NOT EXISTS (SELECT 1 FROM jornadas WHERE tipo = 'Semana'
            AND manha_inicio = '08:00' AND manha_fim = '12:00'
            AND COALESCE(tarde_inicio, '') = '' AND COALESCE(tarde_fim, '') = '')''')


def validar_data(valor):
    if not isinstance(valor, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', valor):
        raise ValueError('Informe uma data válida.')
    return date.fromisoformat(valor)


def jornada_no_dia(conn, funcionario_id, data):
    if validar_data(data).weekday() < 5:
        substituicao = conn.execute('''SELECT manha_inicio, manha_fim, tarde_inicio, tarde_fim
            FROM substituicoes_periodo WHERE substituto_id = ? AND ? BETWEEN data_inicio AND data_fim''',
            (funcionario_id, data)).fetchone()
        if substituicao:
            return tuple(substituicao)
    ajuste = conn.execute('''SELECT manha_inicio, manha_fim, tarde_inicio, tarde_fim
        FROM jornadas_dia WHERE data = ? AND funcionario_id = ?''', (data, funcionario_id)).fetchone()
    if ajuste:
        return tuple(ajuste)
    padrao = conn.execute('''SELECT j.manha_inicio, j.manha_fim, j.tarde_inicio, j.tarde_fim
        FROM funcionarios f JOIN jornadas j ON j.id = f.jornada_id
        WHERE f.id = ? AND j.tipo = 'Semana' ''', (funcionario_id,)).fetchone()
    if not padrao:
        raise ValueError('Os dois funcionários precisam ter uma jornada da semana cadastrada.')
    return tuple(valor or '' for valor in padrao)


def descrever_jornada(horarios):
    faixas = [f'{horarios[i]} - {horarios[i + 1]}' for i in (0, 2) if horarios[i] and horarios[i + 1]]
    return ' / '.join(faixas) or 'Folga'


def registrar_historico(conn, data, titular, substituto, motivo, detalhes, autor):
    conn.execute('''INSERT INTO trocas_sabado
        (data_sabado, funcionario_substituido_id, funcionario_substituto_id,
         motivo, detalhes, aplicada, registrado_por) VALUES (?, ?, ?, ?, ?, 1, ?)''',
        (data, titular, substituto, motivo, detalhes, autor))


def validar_participante_sabado(conn, funcionario_id):
    pessoa = conn.execute('SELECT ativo, nao_trabalha_sabado FROM funcionarios WHERE id = ?', (funcionario_id,)).fetchone()
    if not pessoa or not pessoa[0] or pessoa[1]:
        raise ValueError('Selecione um funcionário ativo que trabalhe aos sábados.')


def ajustar_escala(conn, escala_id, funcionario_id, horario, observacao, autor):
    item = conn.execute('SELECT data_sabado, funcionario_id, horario, observacao FROM escala_sabado WHERE id = ?', (escala_id,)).fetchone()
    if not item:
        raise ValueError('Item da escala não encontrado.')
    funcionario_id = funcionario_id if funcionario_id is not None else item[1]
    observacao = item[3] if observacao is None else observacao
    validar_participante_sabado(conn, funcionario_id)
    if horario not in FAIXAS_SABADO or len(observacao or '') > 500:
        raise ValueError('Horário ou observação inválidos.')
    if (funcionario_id, horario, observacao) != (item[1], item[2], item[3]):
        conn.execute('UPDATE escala_sabado SET funcionario_id=?, horario=?, observacao=? WHERE id=?',
                     (funcionario_id, horario, observacao, escala_id))
        registrar_historico(conn, item[0], item[1], funcionario_id, observacao,
                           f'Ajuste de escala: {item[2]} → {horario}', autor)
    return item[0]


def aplicar_troca(conn, data, titular, substituto, motivo, autor):
    dia = validar_data(data)
    if titular == substituto:
        raise ValueError('Selecione dois funcionários diferentes.')
    pessoas = {row[0]: row for row in conn.execute(
        'SELECT id, nome, nao_trabalha_sabado FROM funcionarios WHERE id IN (?, ?) AND ativo = 1',
        (titular, substituto))}
    if len(pessoas) != 2:
        raise ValueError('Selecione dois funcionários ativos.')
    if dia.weekday() == 6:
        raise ValueError('Não há jornada cadastrada para domingo.')
    if dia.weekday() == 5:
        turnos = {pessoa: [row[0] for row in conn.execute(
            'SELECT horario FROM escala_sabado WHERE data_sabado = ? AND funcionario_id = ? ORDER BY horario',
            (data, pessoa))] for pessoa in (titular, substituto)}
        if not turnos[titular]:
            raise ValueError('O primeiro funcionário não está escalado nesse sábado.')
        if pessoas[substituto][2] or (turnos[substituto] and pessoas[titular][2]):
            raise ValueError('Funcionário com sábado "Nenhum" não pode receber um turno de sábado.')
        anteriores = {pessoa: ' / '.join(turnos[pessoa]) or 'Folga' for pessoa in turnos}
        conn.execute('''UPDATE escala_sabado
            SET funcionario_id = CASE funcionario_id WHEN ? THEN ? ELSE ? END
            WHERE data_sabado = ? AND funcionario_id IN (?, ?)''',
            (titular, substituto, titular, data, titular, substituto))
    else:
        if conn.execute('''SELECT 1 FROM substituicoes_periodo
            WHERE ? BETWEEN data_inicio AND data_fim AND substituto_id IN (?, ?)''',
            (data, titular, substituto)).fetchone():
            raise ValueError('Há substituição por período nesta data; remova-a antes de trocar jornadas.')
        horarios = {pessoa: jornada_no_dia(conn, pessoa, data) for pessoa in pessoas}
        anteriores = {pessoa: descrever_jornada(horarios[pessoa]) for pessoa in pessoas}
        for pessoa, outro in ((titular, substituto), (substituto, titular)):
            conn.execute('''INSERT INTO jornadas_dia
                (data, funcionario_id, manha_inicio, manha_fim, tarde_inicio, tarde_fim)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(data, funcionario_id) DO UPDATE SET
                manha_inicio=excluded.manha_inicio, manha_fim=excluded.manha_fim,
                tarde_inicio=excluded.tarde_inicio, tarde_fim=excluded.tarde_fim''',
                (data, pessoa, *horarios[outro]))
    detalhes = '; '.join(f'{pessoas[pessoa][1]}: {anteriores[pessoa]} → {anteriores[outro]}'
                         for pessoa, outro in ((titular, substituto), (substituto, titular)))
    registrar_historico(conn, data, titular, substituto, motivo, detalhes, autor)


def registrar_substituicao(conn, titular, substituto, inicio, fim, motivo, autor):
    if validar_data(inicio) > validar_data(fim):
        raise ValueError('A data final deve ser igual ou posterior à inicial.')
    if titular == substituto:
        raise ValueError('Selecione dois funcionários diferentes.')
    if not motivo or len(motivo) > 500:
        raise ValueError('Informe um motivo de até 500 caracteres.')
    pessoas = conn.execute('SELECT id FROM funcionarios WHERE id IN (?, ?) AND ativo = 1',
                          (titular, substituto)).fetchall()
    if len(pessoas) != 2:
        raise ValueError('Selecione dois funcionários ativos.')
    if conn.execute('''SELECT 1 FROM substituicoes_periodo
        WHERE data_inicio <= ? AND data_fim >= ?
        AND (titular_id IN (?, ?) OR substituto_id IN (?, ?))''',
        (fim, inicio, titular, substituto, titular, substituto)).fetchone():
        raise ValueError('Um dos funcionários já participa de substituição neste período.')
    if conn.execute('''SELECT 1 FROM jornadas_dia WHERE funcionario_id = ? AND data BETWEEN ? AND ?
        AND strftime('%w', data) BETWEEN '1' AND '5' ''', (substituto, inicio, fim)).fetchone():
        raise ValueError('O substituto possui ajustes de jornada neste período.')
    jornada = conn.execute('''SELECT f.cargo_id, j.manha_inicio, j.manha_fim,
        COALESCE(j.tarde_inicio, ''), COALESCE(j.tarde_fim, '')
        FROM funcionarios f JOIN jornadas j ON j.id = f.jornada_id
        WHERE f.id = ? AND j.tipo = 'Semana' ''', (titular,)).fetchone()
    if not jornada:
        raise ValueError('O titular precisa ter jornada da semana cadastrada.')
    conn.execute('''INSERT INTO substituicoes_periodo (titular_id, substituto_id, data_inicio,
        data_fim, motivo, registrado_por, cargo_id, manha_inicio, manha_fim, tarde_inicio, tarde_fim)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (titular, substituto, inicio, fim, motivo, autor, *jornada))


def intervalos_cobertura(conn, data):
    dia = validar_data(data)
    pessoas = conn.execute('''SELECT f.id, f.nome, f.foto_url FROM funcionarios f
        LEFT JOIN substituicoes_periodo s ON s.substituto_id = f.id
            AND ? BETWEEN s.data_inicio AND s.data_fim AND ? < 5
        LEFT JOIN cargos c ON c.id = CASE WHEN s.id IS NOT NULL THEN s.cargo_id ELSE f.cargo_id END
        WHERE f.ativo = 1 AND (c.nome LIKE '%Suporte%' OR
            CASE WHEN s.id IS NOT NULL THEN s.cargo_id ELSE f.cargo_id END IS NULL)''',
        (data, dia.weekday())).fetchall()
    resultado = []
    for pessoa, nome, foto in pessoas:
        if dia.weekday() == 6:
            intervalos = []
        elif dia.weekday() == 5:
            intervalos = []
            for (horario,) in conn.execute(
                    'SELECT horario FROM escala_sabado WHERE funcionario_id = ? AND data_sabado = ?', (pessoa, data)):
                partes = re.fullmatch(r'(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})', horario or '')
                if partes:
                    intervalos.append(partes.groups())
        else:
            try:
                horarios = jornada_no_dia(conn, pessoa, data)
            except ValueError:
                continue
            intervalos = [(horarios[i], horarios[i + 1]) for i in (0, 2) if horarios[i] and horarios[i + 1]]
        resultado.append((pessoa, nome, foto, intervalos))
    return resultado
