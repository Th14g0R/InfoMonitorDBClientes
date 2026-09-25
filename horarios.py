from flask import Blueprint, render_template, render_template_string, request, redirect, url_for, session, flash, jsonify
import sqlite3
import functools
from html import escape
from contextlib import closing
import os
from datetime import datetime, date, timedelta
from werkzeug.utils import secure_filename
from seguranca import csrf_token, registrar_acao_admin
from escalas import (EQUIPES, TIPOS_SABADO, FAIXAS_SABADO, preservar_banco_antes_migracao,
                     migrar_horarios, validar_data, aplicar_troca, intervalos_cobertura,
                     validar_participante_sabado, ajustar_escala, registrar_substituicao)

horarios_bp = Blueprint('horarios', __name__, template_folder='templates')
DATA_DIR = os.path.abspath(os.getenv('DATA_DIR', '.'))
os.makedirs(DATA_DIR, exist_ok=True)
DB_NAME = os.path.join(DATA_DIR, 'sistema.db')
MAX_FOTO_BYTES = int(os.getenv('MAX_FOTO_KB', '2048')) * 1024

UPLOAD_FOLDER = (os.path.join(DATA_DIR, 'fotos') if os.getenv('DATA_DIR', '').strip()
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'fotos'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
EXTENSOES_PERMITIDAS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def extensao_permitida(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in EXTENSOES_PERMITIDAS

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

PERMISSOES_MENU = {'perm_servidores', 'perm_gestao_bancos', 'perm_horarios'}


def usuario_tem_permissao(codigo_permissao):
    """Valida uma permissão do menu diretamente no banco, sem confiar em dados antigos da sessão."""
    if codigo_permissao not in PERMISSOES_MENU:
        return False
    if not session.get('logged_in') or not session.get('user_id'):
        return False
    conn = get_db()
    usuario = conn.execute(
        "SELECT eh_master, ativo, perm_servidores, perm_gestao_bancos, perm_horarios FROM usuarios WHERE id = ?",
        (session['user_id'],)
    ).fetchone()
    conn.close()
    if not usuario or not usuario['ativo']:
        return False
    return bool(usuario['eh_master']) or bool(usuario[codigo_permissao])


def pode_editar_horarios():
    """Checa Master ou perm_horarios sempre com os dados atuais do banco."""
    return usuario_tem_permissao('perm_horarios')

horarios_bp.add_app_template_global(pode_editar_horarios, name='pode_editar_horarios')
horarios_bp.add_app_template_global(usuario_tem_permissao, name='usuario_tem_permissao')

@horarios_bp.context_processor
def opcoes_funcionario():
    return dict(equipes=EQUIPES, tipos_sabado=TIPOS_SABADO, hoje=date.today().isoformat())


def init_db():
    preservar_banco_antes_migracao(DB_NAME)
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cargos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT UNIQUE NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS jornadas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao TEXT NOT NULL,
                tipo TEXT DEFAULT 'Semana',
                manha_inicio TEXT NOT NULL,
                manha_fim TEXT NOT NULL,
                almoco_inicio TEXT DEFAULT '',
                almoco_fim TEXT DEFAULT '',
                tarde_inicio TEXT DEFAULT '',
                tarde_fim TEXT DEFAULT ''
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS funcionarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                prioridade TEXT DEFAULT 'P1',
                foto_url TEXT DEFAULT '',
                ativo INTEGER DEFAULT 1
            )
        ''')

        cursor.execute("PRAGMA table_info(funcionarios)")
        colunas_func = [c[1] for c in cursor.fetchall()]

        if 'cargo_id' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN cargo_id INTEGER")
        if 'jornada_id' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN jornada_id INTEGER")
        if 'equipe_sabado' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN equipe_sabado TEXT DEFAULT 'Amarela'")
        if 'eh_apoiador_sabado' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN eh_apoiador_sabado INTEGER DEFAULT 0")
        if 'eh_sobreaviso' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN eh_sobreaviso INTEGER DEFAULT 0")
        if 'prioridade' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN prioridade TEXT DEFAULT 'P1'")
        if 'foto_url' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN foto_url TEXT DEFAULT ''")
        if 'ativo' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN ativo INTEGER DEFAULT 1")
        if 'data_nascimento' not in colunas_func: cursor.execute("ALTER TABLE funcionarios ADD COLUMN data_nascimento TEXT")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS escala_sabado (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_sabado TEXT NOT NULL,
                cor_equipe TEXT NOT NULL,
                funcionario_id INTEGER,
                horario TEXT DEFAULT '08:00 - 12:00',
                observacao TEXT,
                FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trocas_sabado (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_sabado TEXT NOT NULL,
                funcionario_substituido_id INTEGER,
                funcionario_substituto_id INTEGER,
                motivo TEXT,
                data_registro TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ausencias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                funcionario_id INTEGER NOT NULL,
                motivo TEXT NOT NULL,
                data_inicio TEXT NOT NULL,
                data_fim TEXT NOT NULL,
                hora_inicio TEXT DEFAULT '',
                hora_fim TEXT DEFAULT '',
                FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute("PRAGMA table_info(ausencias)")
        colunas_aus = [c[1] for c in cursor.fetchall()]
        if 'hora_inicio' not in colunas_aus: cursor.execute("ALTER TABLE ausencias ADD COLUMN hora_inicio TEXT DEFAULT ''")
        if 'hora_fim' not in colunas_aus: cursor.execute("ALTER TABLE ausencias ADD COLUMN hora_fim TEXT DEFAULT ''")

        cursor.execute("SELECT COUNT(*) FROM cargos")
        if cursor.fetchone()[0] == 0:
            cargos_p = [("Suporte Técnico",), ("Desenvolvedor",), ("Comercial",), ("Financeiro",), ("Coordenador",)]
            cursor.executemany("INSERT INTO cargos (nome) VALUES (?)", cargos_p)

        cursor.execute("SELECT COUNT(*) FROM jornadas")
        if cursor.fetchone()[0] == 0:
            jornadas_p = [
                ("07:30 às 17:30 (Almoço 11-13)", "Semana", "07:30", "11:00", "11:00", "13:00", "13:00", "17:30"),
                ("08:00 às 18:00 (Almoço 11-13)", "Semana", "08:00", "11:00", "11:00", "13:00", "13:00", "18:00"),
                ("08:00 às 18:00 (Almoço 12-14)", "Semana", "08:00", "12:00", "12:00", "14:00", "14:00", "18:00"),
                ("08:00 às 18:00 (Almoço 13-15)", "Semana", "08:00", "13:00", "13:00", "15:00", "15:00", "18:00"),
                ("09:00 às 19:00 (Almoço 13-15)", "Semana", "09:00", "13:00", "13:00", "15:00", "15:00", "19:00"),
                ("Sábado - 07:30 às 11:30", "Sabado", "07:30", "11:30", "", "", "", ""),
                ("Sábado - 08:00 às 12:00", "Sabado", "08:00", "12:00", "", "", "", ""),
                ("Sábado - 09:00 às 13:00", "Sabado", "09:00", "13:00", "", "", "", ""),
                ("Sábado - 10:00 às 14:00", "Sabado", "10:00", "14:00", "", "", "", ""),
                ("Sábado - 13:00 às 17:00", "Sabado", "13:00", "17:00", "", "", "", ""),
                ("Sábado - 14:00 às 18:00", "Sabado", "14:00", "18:00", "", "", "", "")
            ]
            cursor.executemany("""
                INSERT INTO jornadas (descricao, tipo, manha_inicio, manha_fim, almoco_inicio, almoco_fim, tarde_inicio, tarde_fim)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, jornadas_p)
        migrar_horarios(conn)
        conn.commit()

init_db()

def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # FIX: usa a MESMA sessão/login do painel de bancos (/admin/login), verificando a
        # permissão 'perm_horarios' fresca no banco a cada requisição (efeito imediato ao
        # inativar/alterar permissão de alguém). O antigo login por senha única fixa foi removido.
        if not session.get('logged_in') or not session.get('user_id'):
            return redirect(url_for('bancos.admin_login', next=request.path))
        conn = get_db()
        usuario = conn.execute("SELECT eh_master, ativo, perm_horarios FROM usuarios WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        if not usuario or not usuario['ativo']:
            session.clear()
            return redirect(url_for('bancos.admin_login', next=request.path))
        if not usuario['eh_master'] and not usuario['perm_horarios']:
            return "Você não tem permissão para acessar o Painel de Horários. Fale com o administrador Master. <a href='/horarios'>Voltar</a>", 403
        return f(*args, **kwargs)
    return decorated_function

def hora_para_minutos(h_str):
    if not h_str or ':' not in str(h_str):
        return 0
    try:
        h, m = map(int, str(h_str).split(':'))
        return h * 60 + m
    except (ValueError, TypeError):
        return 0

def obter_proximo_sabado():
    hoje = date.today()
    dias_ate_sabado = (5 - hoje.weekday()) if hoje.weekday() < 5 else 7
    proximo_sabado = hoje + timedelta(days=dias_ate_sabado)
    return proximo_sabado.strftime('%Y-%m-%d')

def calcular_cobertura_diaria(data_referencia=None):
    data_referencia = data_referencia or date.today().isoformat()
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        
        ausencias_lista = []
        if data_referencia:
            cursor.execute("""
                SELECT funcionario_id, hora_inicio, hora_fim FROM ausencias 
                WHERE ? BETWEEN data_inicio AND data_fim
            """, (data_referencia,))
            ausencias_lista = cursor.fetchall()

        funcs = intervalos_cobertura(conn, data_referencia or date.today().isoformat())
        aniversariantes = {row[0] for row in conn.execute(
            "SELECT id FROM funcionarios WHERE strftime('%m-%d', data_nascimento) = ?",
            (data_referencia[5:],))}

    horarios_grade = []
    minutos_inicio = hora_para_minutos("07:30")
    minutos_fim = hora_para_minutos("19:00")

    while minutos_inicio < minutos_fim:
        m_start = minutos_inicio
        m_end = minutos_inicio + 30
        h_label = f"{m_start//60:02d}:{m_start%60:02d} - {m_end//60:02d}:{m_end%60:02d}"
        
        atendentes = []
        for f in funcs:
            f_id, nome, foto, intervalos = f
            
            # Verifica se o funcionário possui alguma ausência afeta a janela m_start/m_end
            esta_ausente = False
            for aus_f_id, h_in, h_out in ausencias_lista:
                if aus_f_id == f_id:
                    if not h_in or not h_out:
                        esta_ausente = True
                        break
                    else:
                        m_aus_in = hora_para_minutos(h_in)
                        m_aus_out = hora_para_minutos(h_out)
                        if m_start < m_aus_out and m_end > m_aus_in:
                            esta_ausente = True
                            break
            
            if esta_ausente:
                continue
                
            if any(hora_para_minutos(inicio) <= m_start and m_end <= hora_para_minutos(fim) for inicio, fim in intervalos):
                atendentes.append({'nome': nome, 'foto': foto or '/static/avatar-padrao.svg',
                                   'aniversariante': f_id in aniversariantes})

        qtd = len(atendentes)
        alerta = "normal"
        if m_start < hora_para_minutos("08:00") or m_start >= hora_para_minutos("18:00"):
            if qtd < 1: alerta = "critico"
        else:
            if qtd <= 2: alerta = "atencao"

        horarios_grade.append({
            'faixa': h_label,
            'quantidade': qtd,
            'atendentes': atendentes,
            'alerta': alerta
        })
        minutos_inicio += 30

    return horarios_grade

# --- TEMPLATE HTML ---
HTML_INTERFACE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Gestão Integrada de Horários e Escalas</title>
    <link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}" type="image/x-icon">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
    <style>
        /* Horarios-specific styles that extend the design system */
        .nav-bar { 
            position: sticky; 
            top: 0; 
            z-index: var(--z-sticky); 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            background: var(--cor-superficie); 
            border: 1px solid var(--cor-borda);
            padding: var(--space-3) var(--space-4); 
            border-radius: var(--raio-lg); 
            margin-bottom: var(--space-5); 
            box-shadow: var(--shadow-sm); 
        }
        .card { background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: var(--space-5); border-radius: var(--raio-lg); box-shadow: var(--shadow-sm); margin-bottom: var(--space-5); }
        table { width: 100%; border-collapse: collapse; margin-top: var(--space-3); }
        th, td { border: 1px solid var(--cor-borda); padding: var(--space-2) var(--space-3); text-align: left; font-size: var(--font-size-sm); vertical-align: middle; }
        th { background-color: var(--cor-fundo); color: var(--cor-texto); font-weight: 600; user-select: none; position: sticky; top: 0; z-index: 1; }
        
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: var(--space-3); margin-top: var(--space-3); align-items: end; }
        .form-grid input, .form-grid select { padding: var(--space-2) var(--space-3); border: 1px solid var(--cor-borda); border-radius: var(--raio-md); width: 100%; box-sizing: border-box; font-family: inherit; font-size: var(--font-size-sm); }
        .form-grid input:focus, .form-grid select:focus { outline: none; border-color: var(--cor-primaria); box-shadow: var(--shadow-focus); }
        
        .col-acoes { width: 120px; text-align: center; white-space: nowrap; }
        .acoes-container { display: flex; gap: var(--space-2); justify-content: center; align-items: center; }

        .badge-amarela { background: var(--cor-aviso-bg); color: var(--cor-aviso); border: 1px solid var(--cor-aviso-borda); }
        .badge-verde { background: var(--cor-sucesso-bg); color: var(--cor-sucesso); border: 1px solid var(--cor-sucesso-borda); }
        .badge-prio { background: var(--cor-roxo-bg); color: var(--cor-roxo); border: 1px solid var(--cor-roxo-borda); font-size: var(--font-size-xs); }
        .badge-inativo { background: var(--cor-fundo); color: var(--cor-texto-suave); border: 1px solid var(--cor-borda); font-size: var(--font-size-xs); }

        .tr-inativo { opacity: 0.6; background-color: var(--cor-fundo); }

        .lista-rolagem { max-height: 360px; overflow: auto; margin-top: var(--space-3); }
        .lista-rolagem table { margin-top: 0; }
        .lista-rolagem thead th { position: sticky; top: 0; z-index: 1; background: var(--cor-superficie); }
        .grid-cobertura { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: var(--space-3); margin-top: var(--space-3); }
        .card-cobertura-slot { border-radius: var(--raio-md); border: 1px solid var(--cor-borda); padding: var(--space-2); text-align: center; background: var(--cor-superficie); }
        .cov-normal { border-top: 4px solid var(--cor-sucesso); background: var(--cor-sucesso-bg); }
        .cov-atencao { border-top: 4px solid var(--cor-aviso); background: var(--cor-aviso-bg); }
        .cov-critico { border-top: 4px solid var(--cor-perigo); background: var(--cor-perigo-bg); }
        .slot-hora { font-weight: 600; font-size: var(--font-size-sm); color: var(--cor-texto); margin-bottom: var(--space-1); }
        .slot-qtd { font-size: var(--font-size-xs); color: var(--cor-texto-suave); margin-bottom: var(--space-2); }

        .avatars-container { display: flex; flex-wrap: wrap; gap: var(--space-1); justify-content: center; align-items: center; min-height: 36px; }
        .avatar-item { position: relative; display: inline-block; }
        .avatar-img { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; border: 1px solid var(--cor-superficie); box-shadow: 0 1px 2px rgba(0,0,0,0.2); transition: transform var(--transition-fast) ease, z-index var(--transition-fast); cursor: pointer; }
        .avatar-item:hover .avatar-img { transform: scale(2.5); z-index: 100; position: relative; }
        .foto-cobertura { padding: 0; border: 0; background: transparent; cursor: zoom-in; line-height: 0; }
        .foto-cobertura:hover .avatar-img { transform: none; }
        .foto-cobertura:focus-visible { outline: 3px solid var(--cor-primaria); outline-offset: 3px; border-radius: 50%; }
        .foto-cobertura.aniversariante .avatar-img { border: 2px solid var(--cor-aviso); box-shadow: 0 0 0 2px var(--cor-aviso-bg); }
        .aniversario-icone { position: absolute; bottom: -3px; right: -3px; font-size: 14px; line-height: 1; pointer-events: none; }
        #foto-ampliada { max-width: min(90vw, 720px); padding: var(--space-5); border: 0; border-radius: var(--raio-xl); color: var(--cor-texto); background: var(--cor-superficie); }
        #foto-ampliada::backdrop { background: rgba(0, 0, 0, .75); }
        #foto-ampliada img { display: block; max-width: 100%; max-height: 75vh; margin: var(--space-3) auto 0; object-fit: contain; }

        .grid-escala { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: var(--space-2); margin-top: var(--space-3); }
        .coluna-turno { background: var(--cor-fundo); border: 1px solid var(--cor-borda); border-radius: var(--raio-md); min-height: 180px; padding: var(--space-2); }
        .coluna-turno-header { font-weight: 600; font-size: var(--font-size-sm); text-align: center; padding: var(--space-1) var(--space-2); background: var(--cor-superficie-hover); border-radius: var(--raio-sm); margin-bottom: var(--space-2); color: var(--cor-texto); }
        
        /* Cores de equipe (Verde/Amarela) mantidas intencionalmente: identificam a equipe real do técnico, não são decorativas. */
        .card-tec { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-left: 4px solid var(--cor-primaria); border-radius: var(--raio-md); padding: var(--space-2); margin-bottom: var(--space-2); box-shadow: var(--shadow-sm); display: flex; align-items: center; gap: var(--space-2); }
        .card-tec[draggable="true"] { cursor: grab; }
        .card-tec[draggable="true"]:active { cursor: grabbing; opacity: 0.6; }
        .card-tec.eq-Verde { border-left-color: #005b41; }
        .card-tec.eq-Amarela { border-left-color: #f3c716; }
        .card-avatar { width: 36px; height: 36px; border-radius: 50%; object-fit: cover; background: var(--cor-borda); flex-shrink: 0; transition: transform var(--transition-fast) ease; }
        .card-avatar:hover { transform: scale(2); z-index: 50; }
        .card-info { flex: 1; overflow: hidden; }
        .card-nome { font-weight: 600; font-size: var(--font-size-sm); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .card-obs { font-size: var(--font-size-xs); color: var(--cor-texto-suave); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        .secao-sobreaviso-box { background: var(--cor-aviso-bg); border: 1px dashed var(--cor-aviso-borda); border-radius: var(--raio-md); padding: var(--space-4); margin-top: var(--space-4); }
        .grid-sobreaviso-apoio { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); margin-top: var(--space-3); }
        .subcoluna-box { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-radius: var(--raio-md); padding: var(--space-3); }
        .subcoluna-title { font-weight: 600; font-size: var(--font-size-xs); margin-bottom: var(--space-2); text-transform: uppercase; letter-spacing: 0.5px; color: var(--cor-texto-suave); }

        .pdf-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: var(--space-3) var(--space-5);
            border-radius: var(--raio-md);
            margin-bottom: var(--space-4);
            color: var(--cor-texto-invertido);
        }

        .pdf-header.tema-verde {
            background-color: #005b41;
        }

        .pdf-header.tema-amarela {
            background-color: #f3c716;
            color: #1a1a1a;
        }

        .pdf-date-badge {
            background: rgba(255, 255, 255, 0.25);
            padding: var(--space-1) var(--space-3);
            border-radius: 4px;
            font-weight: bold;
            font-size: 16px;
        }

        .pdf-title-container {
            text-align: center;
        }

        .pdf-title-container h1 {
            margin: 0;
            font-size: 22px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .pdf-title-container h2 {
            margin: 2px 0 0 0;
            font-size: 15px;
            font-weight: normal;
        }

        .is-exporting .grid-escala {
            display: grid !important;
            grid-template-columns: repeat(6, 1fr) !important;
            gap: 6px !important;
        }

        .is-exporting .grid-escala .card-obs {
            display: none !important;
        }

        .is-exporting .coluna-turno {
            min-height: auto !important;
            padding: 4px !important;
        }

        .is-exporting .card-tec {
            padding: 4px !important;
            margin-bottom: 4px !important;
        }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css', v='horarios-20260925-1') }}">
<script src="{{ url_for('static', filename='ui.js', v='horarios-20260925-1') }}" defer></script>
</head>
<body>
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <div class="nav-bar">
        <h2 style="margin: 0;">🗓️ Controle de Horários, Cobertura e Rodízio</h2>
        <div class="d-flex items-center gap-2">
            <a href="{{ url_for('bancos.exibir_servidor') }}" class="btn btn-primary">🖥️ Servidores</a>
            {% if not session.get('logged_in') %}
                <a href="{{ url_for('bancos.admin_login', next='/admin') }}" class="btn btn-bancos">🗄️ Gestão de Bancos</a>
            {% elif usuario_tem_permissao('perm_gestao_bancos') %}
                <a href="{{ url_for('bancos.admin_painel') }}" class="btn btn-bancos">🗄️ Gestão de Bancos</a>
            {% else %}
                <button type="button" class="btn btn-bancos" onclick="return avisarSemPermissao('Gestão de Bancos')">🗄️ Gestão de Bancos</button>
            {% endif %}
            {% if pode_editar_horarios() %}
                <form action="/horarios/logout" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-danger">🔒 Sair da Edição</button></form>
            {% elif session.get('logged_in') %}
                <button type="button" class="btn btn-success" onclick="return avisarSemPermissao('Gestão de Horários')">🔑 Login</button>
            {% else %}
                <a href="/horarios/login" class="btn btn-success">🔑 Login</a>
            {% endif %}
            <button id="theme-toggle" class="theme-toggle" aria-label="Alternar tema" title="Alternar tema claro/escuro">
                <svg class="moon-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
                <svg class="sun-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
            </button>
        </div>
    </div>

    <div id="conteudo-principal" role="main">

    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for message in messages %}
          <div class="alert-box">⚠️ {{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <!-- 1. ESCALA DE SÁBADOS -->
    <div class="card" id="secao-escala-sabado">
        <div class="section-header">
            <h3>📅 Escala de Sábados (4h Por Técnico)</h3>
        </div>
        
        <div class="info-box info-box-primary d-flex flex-wrap justify-between items-center gap-3">
            <div class="d-flex gap-2 items-center flex-wrap">
                {% if pode_editar_horarios() %}
                    <form action="/horarios/gerar_sugestao_sabado" method="POST" class="form-inline d-flex gap-2 items-center" onsubmit="return validarSabadoForm(this.data_sabado)">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <label>⚡ <strong>Gerar Rodízio:</strong></label>
                        <input type="date" name="data_sabado" id="input_gerar_sabado" required class="form-control" onchange="validarApenasSabado(this)" style="width: auto;">
                        <button type="submit" class="btn btn-success">Montar Escala do Sábado</button>
                    </form>
                {% endif %}
                <button onclick="exportarEscalaPDF()" class="btn btn-export btn-with-icon">🖨️ Exportar Escala</button>
            </div>

            <form action="/horarios#secao-escala-sabado" method="GET" class="d-flex gap-2 items-center flex-wrap">
                {% if busca_func_id %}<input type="hidden" name="busca_funcionario" value="{{ busca_func_id }}">{% endif %}
                <label>🔍 <strong>Ver Data Específica:</strong></label>
                <input type="date" name="data_filtro_sabado" value="{{ data_filtro_sabado }}" onchange="validarApenasSabado(this)" class="form-control" style="width: auto;">
                <button type="submit" class="btn btn-primary">Filtrar</button>
                {% if data_filtro_sabado or modo_todos %}
                    <a href="/horarios#secao-escala-sabado" class="btn btn-danger">Ver Próximo Sábado</a>
                {% else %}
                    <a href="/horarios?modo=todos#secao-escala-sabado" class="btn btn-secondary">Ver Histórico Completo</a>
                {% endif %}
            </form>
        </div>

        {% if data_filtro_sabado_formatada %}
            <div class="alert alert-success d-flex justify-between items-center" style="margin-bottom: var(--space-3);">
                <span>📅 Exibindo Escala do Sábado: {{ data_filtro_sabado_formatada }}</span>
                {% if pode_editar_horarios() and data_filtro_sabado %}
                    <form action="/horarios/excluir_dia_inteiro/{{ data_filtro_sabado }}" method="POST" class="form-inline">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('Apagar a escala do dia {{ data_filtro_sabado_formatada }}?')">🗑️ Limpar Este Dia</button>
                    </form>
                {% endif %}
            </div>
        {% endif %}

        {% if pode_editar_horarios() and data_filtro_sabado %}
            <div class="card" style="margin-bottom: var(--space-4);">
                <h4 style="margin: 0 0 var(--space-3); font-size: var(--font-size-base);">➕ Adicionar / Encaixar Técnico Nesta Escala</h4>
                <form action="/horarios/adicionar_item_escala" method="POST" class="form-grid" style="grid-template-columns: auto auto auto auto 1fr auto; align-items: end; gap: var(--space-2);">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <input type="hidden" name="data_sabado" value="{{ data_filtro_sabado }}">
                    <div>
                        <label class="form-label">Funcionário</label>
                        <select name="funcionario_id" required class="form-select">
                            <option value="">Selecione o Funcionário...</option>
                            {% for f in funcionarios %}
                                {% if f[11] == 1 and f[12] == 0 %}
                                    <option value="{{ f[0] }}">{{ f[1] }} ({{ f[4] }})</option>
                                {% endif %}
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label class="form-label">Horário</label>
                        <select name="horario" required class="form-select">
                            <option value="07:30 - 11:30">07:30 - 11:30</option>
                            <option value="08:00 - 12:00">08:00 - 12:00</option>
                            <option value="09:00 - 13:00">09:00 - 13:00</option>
                            <option value="10:00 - 14:00">10:00 - 14:00</option>
                            <option value="13:00 - 17:00">13:00 - 17:00</option>
                            <option value="14:00 - 18:00">14:00 - 18:00</option>
                            <option value="Sobreaviso">Sobreaviso</option>
                        </select>
                    </div>
                    <div>
                        <label class="form-label">Tipo</label>
                        <select name="tipo_sabado" class="form-select">
                            <option value="rodizio">Rodízio Normal</option>
                            <option value="apoio">Apoio Fixo Dev/Com/Fin</option>
                            <option value="sobreaviso">Sobreaviso Oficial</option>
                        </select>
                    </div>
                    <div>
                        <label class="form-label">Observação</label>
                        <input type="text" name="observacao" placeholder="Obs / Motivo (Opcional)" class="form-control">
                    </div>
                    <button type="submit" class="btn btn-success">Adicionar à Escala</button>
                </form>
            </div>
        {% endif %}

        <div id="area-impressao-escala" style="padding: 10px; background: white;">
            
            <div class="pdf-header tema-{{ cor_equipe_dia|lower }}">
                <div class="pdf-date-badge">{{ data_filtro_sabado_formatada or '08/08/2026' }}</div>
                <div class="pdf-title-container">
                    <h1>ESCALA SÁBADO</h1>
                    <h2>Time {{ cor_equipe_dia }} - Infobrasil</h2>
                </div>
                <div style="width: 80px;"></div>
            </div>

            <div class="grid-escala">
                {% set faixas = ["07:30 - 11:30", "08:00 - 12:00", "09:00 - 13:00", "10:00 - 14:00", "13:00 - 17:00", "14:00 - 18:00"] %}
                {% for faixa in faixas %}
                    <div class="coluna-turno" data-horario="{{ faixa }}" ondragover="allowDrop(event)" ondrop="drop(event)">
                        <div class="coluna-turno-header">⏰ {{ faixa }}</div>
                        {% for e in escala_sabados %}
                            {% if e[4] == faixa and 'Apoio Fixo' not in (e[5] or '') %}
                                <div class="card-tec eq-{{ e[6] }}" id="card-escala-{{ e[0] }}" {% if pode_editar_horarios() %}draggable="true"{% endif %} ondragstart="drag(event)" data-id="{{ e[0] }}">
                                    <img src="{{ e[7] or '/static/avatar-padrao.svg' }}" class="card-avatar">
                                    <div class="card-info">
                                        <div class="card-nome">{{ e[3] }}</div>
                                        <div class="card-obs">{{ e[5] or 'Rodízio' }}</div>
                                    </div>
                                    {% if pode_editar_horarios() %}
                                        <div class="acoes-container">
                                            <a href="/horarios/editar_item_escala/{{ e[0] }}" class="btn-icon btn-icon-warning" title="Editar Item" aria-label="Editar item da escala"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg></a>
                                            <form action="/horarios/excluir_escala_sabado/{{ e[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn-icon btn-icon-danger" onclick="return confirm('Remover item?')" aria-label="Excluir item da escala"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button></form>
                                        </div>
                                    {% endif %}
                                </div>
                            {% endif %}
                        {% endfor %}
                    </div>
                {% endfor %}
            </div>

            <div class="secao-sobreaviso-box">
                <h4 style="margin: 0 0 10px 0; color: #d35400;">⚠️ SUPORTE INTERNO E SOBREAVISO</h4>
                
                <div class="grid-sobreaviso-apoio">
                    <div class="subcoluna-box">
                        <div class="subcoluna-title" style="color: #c0392b;">🚨 SOBREAVISO</div>
                        <div class="coluna-turno" data-horario="Sobreaviso" ondragover="allowDrop(event)" ondrop="drop(event)" style="min-height: 100px;">
                            {% for e in escala_sabados %}
                                {% if e[4] == 'Sobreaviso' or 'Sobreaviso' in (e[5] or '') %}
                                    <div class="card-tec eq-{{ e[6] }}" id="card-escala-{{ e[0] }}" {% if pode_editar_horarios() %}draggable="true"{% endif %} ondragstart="drag(event)" data-id="{{ e[0] }}">
                                        <img src="{{ e[7] or '/static/avatar-padrao.svg' }}" class="card-avatar">
                                        <div class="card-info">
                                            <div class="card-nome">{{ e[3] }}</div>
                                            <div class="card-obs">{{ e[5] or 'Sobreaviso Oficial' }}</div>
                                        </div>
                                        {% if pode_editar_horarios() %}
                                            <div class="acoes-container">
                                                <a href="/horarios/editar_item_escala/{{ e[0] }}" class="btn-icon btn-icon-warning" title="Editar Item" aria-label="Editar item da escala"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg></a>
                                                <form action="/horarios/excluir_escala_sabado/{{ e[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn-icon btn-icon-danger" onclick="return confirm('Remover item?')" aria-label="Excluir item da escala"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button></form>
                                            </div>
                                        {% endif %}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                    </div>

                    <div class="subcoluna-box">
                        <div class="subcoluna-title" style="color: #27ae60;">🛠️ APOIO FIXO (DEV/COM/FIN)</div>
                        <div class="coluna-turno" data-horario="08:00 - 12:00" ondragover="allowDrop(event)" ondrop="drop(event)" style="min-height: 100px;">
                            {% for e in escala_sabados %}
                                {% if 'Apoio Fixo' in (e[5] or '') %}
                                    <div class="card-tec eq-{{ e[6] }}" id="card-escala-{{ e[0] }}" {% if pode_editar_horarios() %}draggable="true"{% endif %} ondragstart="drag(event)" data-id="{{ e[0] }}">
                                        <img src="{{ e[7] or '/static/avatar-padrao.svg' }}" class="card-avatar">
                                        <div class="card-info">
                                            <div class="card-nome">{{ e[3] }}</div>
                                            <div class="card-obs">{{ e[4] }} - {{ e[5] }}</div>
                                        </div>
                                        {% if pode_editar_horarios() %}
                                            <div class="acoes-container">
                                                <a href="/horarios/editar_item_escala/{{ e[0] }}" class="btn-icon btn-icon-warning" title="Editar Item" aria-label="Editar item da escala"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg></a>
                                                <form action="/horarios/excluir_escala_sabado/{{ e[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn-icon btn-icon-danger" onclick="return confirm('Remover item?')" aria-label="Excluir item da escala"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button></form>
                                            </div>
                                        {% endif %}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </div>
                    </div>
                </div>
            </div>
        </div>

    </div>

    <!-- 2. COBERTURA DE ATENDIMENTO -->
    <div class="card" id="secao-cobertura">
        <div class="section-header">
            <h3>📊 Cobertura de Atendimento</h3>
            <form action="/horarios#secao-cobertura" method="GET" class="d-flex gap-2 items-center">
                {% if data_filtro_sabado %}<input type="hidden" name="data_filtro_sabado" value="{{ data_filtro_sabado }}">{% endif %}
                <label>📅 <strong>Consultar dia:</strong></label>
                <input type="date" name="data_cobertura" value="{{ data_cobertura or '' }}" onchange="this.form.submit()" class="form-control" style="width: auto;">
                {% if data_cobertura %}
                    <a href="/horarios#secao-cobertura" class="btn btn-danger btn-sm">Limpar</a>
                {% endif %}
            </form>
        </div>

        {% if data_cobertura %}
            <div class="info-box info-box-primary" style="margin-bottom: 10px;">
                ℹ️ Exibindo cobertura para o dia <strong>{{ data_cobertura_formatada }}</strong>.
            </div>
        {% endif %}

        <div class="grid-cobertura">
            {% for c in cobertura %}
                <div class="card-cobertura-slot cov-{{ c.alerta }}">
                    <div class="slot-hora">{{ c.faixa }}</div>
                    <div class="slot-qtd">{{ c.quantidade }} presente(s)</div>
                    <div class="avatars-container">
                        {% if c.atendentes %}
                            {% for at in c.atendentes %}
                                <button type="button" class="avatar-item foto-cobertura{% if at.aniversariante %} aniversariante{% endif %}" title="{{ at.nome }}{% if at.aniversariante %} — Aniversariante do dia! 🎂{% endif %}" aria-label="Ampliar foto de {{ at.nome }}{% if at.aniversariante %}, aniversariante do dia{% endif %}">
                                    <img src="{{ at.foto }}" class="avatar-img" alt="{{ at.nome }}">
                                    {% if at.aniversariante %}<span class="aniversario-icone" aria-hidden="true">🎂</span>{% endif %}
                                </button>
                            {% endfor %}
                        {% else %}
                            <span style="color: #78281f; font-size:0.7em; font-weight: bold;">⚠️ VAZIO</span>
                        {% endif %}
                    </div>
                </div>
            {% endfor %}
        </div>
    </div>

    <!-- 3. AUSÊNCIAS, FÉRIAS E LICENÇAS -->
    <div class="card" id="secao-ausencias">
        <div class="section-header">
            <h3>🏖️ Ausências, Férias e Licenças</h3>
            <span class="text-muted" style="font-size: 0.85em; font-weight: bold;">{{ 'Resultados do filtro' if filtro_ausencias else 'Ausências vigentes hoje' }}</span>
        </div>

        {% if pode_editar_horarios() %}
            <form action="/horarios/salvar_ausencia" method="POST" class="form-grid" style="margin-bottom: 20px;">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <select name="funcionario_id" required class="form-select">
                    <option value="">Selecione o Técnico...</option>
                    {% for f in funcionarios %}{% if f[11] == 1 %}<option value="{{ f[0] }}">{{ f[1] }}</option>{% endif %}{% endfor %}
                </select>
                <input type="text" name="motivo" placeholder="Motivo (Férias, Atestado, Licença...)" required class="form-control">
                <div>
                    <label class="form-label">Data Início:</label>
                    <input type="date" name="data_inicio" required class="form-control">
                </div>
                <div>
                    <label class="form-label">Hora Início (Opcional):</label>
                    <input type="time" name="hora_inicio" class="form-control">
                </div>
                <div>
                    <label class="form-label">Data Fim:</label>
                    <input type="date" name="data_fim" required class="form-control">
                </div>
                <div>
                    <label class="form-label">Hora Fim (Opcional):</label>
                    <input type="time" name="hora_fim" class="form-control">
                </div>
                <button type="submit" class="btn btn-admin">Cadastrar Ausência</button>
            </form>
        {% endif %}

        <form action="/horarios#secao-ausencias" method="GET" class="form-grid">
            <label class="form-label">Início do período <input type="date" name="ausencia_inicio" value="{{ ausencia_inicio }}" class="form-control"></label>
            <label class="form-label">Fim do período <input type="date" name="ausencia_fim" value="{{ ausencia_fim }}" class="form-control"></label>
            <label class="form-label">Motivo <select name="ausencia_motivo" class="form-select">
                <option value="">Todos os motivos</option>
                {% for motivo in motivos_ausencias %}<option value="{{ motivo }}" {% if motivo == ausencia_motivo %}selected{% endif %}>{{ motivo }}</option>{% endfor %}
            </select></label>
            <button class="btn btn-bancos" type="submit">Filtrar ausências</button>
            <a href="/horarios#secao-ausencias" class="btn btn-secondary">Limpar filtros</a>
        </form>
        <div class="lista-rolagem" tabindex="0" role="region" aria-label="Lista com rolagem"><table>
            <thead>
                <tr>
                    <th>Técnico</th>
                    <th>Motivo</th>
                    <th>Data Início</th>
                    <th>Hora Início</th>
                    <th>Data Fim</th>
                    <th>Hora Fim</th>
                    {% if pode_editar_horarios() %}<th class="col-acoes">Ações</th>{% endif %}
                </tr>
            </thead>
            <tbody>
                {% for a in ausencias %}
                <tr>
                    <td><strong>{{ a[1] }}</strong></td>
                    <td>{{ a[2] }}</td>
                    <td>{{ a[3] }}</td>
                    <td>{{ a[5] or 'Dia Todo' }}</td>
                    <td>{{ a[4] }}</td>
                    <td>{{ a[6] or 'Dia Todo' }}</td>
                    {% if pode_editar_horarios() %}
                    <td class="col-acoes">
                        <div class="acoes-container">
                            <a href="/horarios/editar_ausencia/{{ a[0] }}" class="btn btn-edit btn-sm">✏️ Editar</a>
                            <form action="/horarios/excluir_ausencia/{{ a[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-delete btn-sm" onclick="return confirm('Excluir ausência?')" aria-label="Excluir ausência">❌</button></form>
                        </div>
                    </td>
                    {% endif %}
                </tr>
                {% else %}
                <tr>
                    <td colspan="7" style="text-align: center; color: #777;">Nenhuma ausência encontrada para este período ou motivo.</td>
                </tr>
                {% endfor %}
            </tbody>
        </table></div>
    </div>

    <!-- 4. EQUIPE E JORNADAS CADASTRADAS -->
    <div class="card" id="secao-equipe">
        <div class="section-header">
            <h3>👥 Equipe e Jornadas Cadastradas</h3>
        </div>
        <div class="lista-rolagem" tabindex="0" role="region" aria-label="Lista com rolagem"><table id="tabela-equipe">
            <thead>
                <tr>
                    <th onclick="ordenarTabela('tabela-equipe', 0)" style="cursor: pointer;">Status ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 1)" style="cursor: pointer;">Prioridade ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 2)" style="cursor: pointer;">Nome ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 3)" style="cursor: pointer;">Cargo ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 4)" style="cursor: pointer;">Jornada / Horário ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 5)" style="cursor: pointer;">Equipe ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 6)" style="cursor: pointer;">Tipo no Sábado ↕</th>
                    <th onclick="ordenarTabela('tabela-equipe', 7)" style="cursor:pointer;">Aniversário ↕</th>
                    {% if pode_editar_horarios() %} <th class="col-acoes">Ações</th> {% endif %}
                </tr>
            </thead>
            <tbody>
                {% for f in funcionarios %}
                <tr class="{% if f[11] == 0 %}tr-inativo{% endif %}">
                    <td>
                        {% if f[11] == 1 %}
                            <span class="badge-verde" style="font-size:0.75em;">ATIVO</span>
                        {% else %}
                            <span class="badge-inativo">INATIVO</span>
                        {% endif %}
                    </td>
                    <td><span class="badge-prio">{{ f[8] or 'P1' }}</span></td>
                    <td>
                        <div style="display:flex; align-items:center; gap:8px;">
                            <img src="{{ f[10] or '/static/avatar-padrao.svg' }}" style="width:32px; height:32px; border-radius:50%; object-fit:cover;">
                            <strong>{{ f[1] }}</strong>
                        </div>
                    </td>
                    <td>{{ f[6] or 'Não definido' }}</td>
                    <td>{{ f[7] or 'Não definida' }}</td>
                    <td>
                        {% if f[4] == 'Amarela' %}<span class="badge-amarela">Amarela</span>
                        {% elif f[4] == 'Verde' %}<span class="badge-verde">Verde</span>
                        {% else %}<span class="badge-prio">{{ f[4] or 'Sem Equipe' }}</span>{% endif %}
                    </td>
                    <td>
                        {% if f[12] == 1 %}Nenhum — não trabalha aos sábados
                        {% elif f[9] == 1 %}⚠️ <strong>Sobreaviso</strong>
                        {% elif f[5] == 1 %}🛠️ Apoio Fixo (8h-12h)
                        {% else %}🔄 Rodízio Normal{% endif %}
                    </td>
                    <td data-sort="{{ (f[13][3:5] ~ f[13][:2]) if f[13] else '9999' }}">{{ f[13] or 'Não informada' }}</td>
                    {% if pode_editar_horarios() %}
                    <td class="col-acoes">
                        <div class="acoes-container">
                            <a href="/horarios/editar_funcionario/{{ f[0] }}" class="btn btn-edit btn-sm">✏️ Editar</a>
                            <form action="/horarios/excluir_funcionario/{{ f[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-delete btn-sm" onclick="return confirm('Excluir permanentemente?')" aria-label="Excluir funcionário">❌</button></form>
                        </div>
                    </td>
                    {% endif %}
                </tr>
                {% endfor %}
            </tbody>
        </table></div>

        {% if pode_editar_horarios() %}
            <h4 style="margin-top: 20px;">➕ Cadastrar Novo Funcionário</h4>
            <form action="/horarios/salvar_funcionario" method="POST" enctype="multipart/form-data" class="form-grid form-funcionario">
                {% include 'campos_funcionario.html' %}
                <button type="submit" class="btn btn-admin btn-full">Cadastrar</button>
            </form>
        {% endif %}
    </div>

    <!-- 5. TROCAS E CONSULTA DE DIAS TRABALHADOS -->
    <div class="card" id="secao-trocas">
        <div class="section-header">
            <h3>🔄 Registros de Trocas e Consulta de Dias Trabalhados</h3>
        </div>
        <form action="/horarios#secao-trocas" method="GET" class="d-flex gap-2 flex-wrap" style="margin-bottom: 15px;">
            <label class="form-label">Data das trocas <input type="date" name="data_trocas" value="{{ data_trocas }}" class="form-control"></label>
            <select name="busca_funcionario" class="form-select" style="width: auto;">
                <option value="">Filtrar Histórico por Funcionário...</option>
                {% for f in funcionarios %}
                    <option value="{{ f[0] }}" {% if busca_func_id == f[0]|string %}selected{% endif %}>{{ f[1] }}</option>
                {% endfor %}
            </select>
            <button type="submit" class="btn btn-bancos">🔍 Consultar Histórico</button>
            {% if busca_func_id or data_trocas %}<a href="/horarios#secao-trocas" class="btn btn-danger">Limpar Filtro</a>{% endif %}
        </form>

        {% if pode_editar_horarios() %}
            <h4>➕ Trocar horários em uma data</h4>
            <form action="/horarios/registrar_troca" method="POST" class="form-grid">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="date" name="data_sabado" value="{{ data_cobertura }}" aria-label="Data da troca" required class="form-control">
                <select name="substituido_id" required class="form-select">
                    <option value="">Primeiro funcionário...</option>
                    {% for f in funcionarios if f[11] == 1 %}<option value="{{ f[0] }}">{{ f[1] }}</option>{% endfor %}
                </select>
                <select name="substituto_id" required class="form-select">
                    <option value="">Trocar com...</option>
                    {% for f in funcionarios if f[11] == 1 %}<option value="{{ f[0] }}">{{ f[1] }}</option>{% endfor %}
                </select>
                <input type="text" name="motivo" placeholder="Motivo da troca" class="form-control">
                <button type="submit" class="btn btn-admin">Salvar Troca</button>
            </form>
            <p class="text-muted" style="font-size: 0.85em;">A troca vale apenas para essa data. Aos sábados, se o segundo funcionário estiver de folga, ele assume o turno do primeiro; se ambos estiverem escalados, os turnos são trocados. A cobertura usa os horários resultantes e mantém as ausências registradas.</p>
        {% endif %}

        <h4>Substituição de atribuições por período (segunda a sexta)</h4>
        <p>O substituto assume o cargo e a jornada do titular no período informado. Ao terminar, volta automaticamente às atribuições habituais. Cadastre também a ausência do titular em Ausências. Os sábados seguem a escala normal.</p>
        {% if pode_editar_horarios() %}
        <form action="/horarios/substituicao_periodo" method="POST" class="form-grid">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label class="form-label">Titular <select name="titular_id" required class="form-select"><option value="">Selecione...</option>{% for f in funcionarios if f[11] == 1 %}<option value="{{ f[0] }}">{{ f[1] }}</option>{% endfor %}</select></label>
            <label class="form-label">Substituto <select name="substituto_id" required class="form-select"><option value="">Selecione...</option>{% for f in funcionarios if f[11] == 1 %}<option value="{{ f[0] }}">{{ f[1] }}</option>{% endfor %}</select></label>
            <label class="form-label">Início <input type="date" name="data_inicio" required class="form-control"></label>
            <label class="form-label">Fim (inclusive) <input type="date" name="data_fim" required class="form-control"></label>
            <input name="motivo" placeholder="Motivo da substituição" maxlength="500" required class="form-control">
            <button class="btn btn-admin" type="submit">Cadastrar substituição</button>
        </form>
        {% endif %}
        <div class="lista-rolagem" tabindex="0" role="region" aria-label="Substituições por período">
        <table><thead><tr><th>Titular</th><th>Substituto</th><th>Início</th><th>Fim</th><th>Motivo</th>{% if pode_editar_horarios() %}<th>Ações</th>{% endif %}</tr></thead><tbody>
        {% for s in substituicoes %}<tr><td>{{ s[1] }}</td><td>{{ s[2] }}</td><td>{{ s[3][8:10] }}/{{ s[3][5:7] }}/{{ s[3][:4] }}</td><td>{{ s[4][8:10] }}/{{ s[4][5:7] }}/{{ s[4][:4] }}</td><td>{{ s[5] }}</td>
        {% if pode_editar_horarios() %}<td><form action="/horarios/excluir_substituicao/{{ s[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-delete btn-sm" onclick="return confirm('Excluir esta substituição?')" aria-label="Excluir substituição">Excluir</button></form></td>{% endif %}</tr>
        {% else %}<tr><td colspan="6" class="text-muted">Nenhuma substituição cadastrada.</td></tr>{% endfor %}
        </tbody></table></div>

        <h4 style="margin-top: 15px;">Histórico de Trocas Cadastradas</h4>
        <div class="lista-rolagem" tabindex="0" role="region" aria-label="Lista com rolagem"><table>
            <thead>
                <tr>
                    <th>Data</th>
                    <th>Funcionário</th>
                    <th>Troca com / Ajuste</th>
                    <th>Motivo</th><th>Horários e registro</th>
                </tr>
            </thead>
            <tbody>
                {% for t in trocas %}
                <tr>
                    <td><strong>{{ t[1] }}</strong></td>
                    <td style="color: #c0392b;">{{ t[2] }}</td>
                    <td style="color: #27ae60;"><strong>{{ t[3] }}</strong></td>
                    <td>{{ t[4] or '-' }}</td><td>{{ t[5] or 'Registro anterior: sem aplicação automática à cobertura.' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table></div>
    </div>

    <!-- 6. GERENCIAMENTO DE CARGOS E JORNADAS -->
    {% if pode_editar_horarios() %}
    <div class="card" id="secao-cargos-jornadas">
        <div class="section-header">
            <h3>⚙️ Gerenciamento de Cargos e Tabela de Horários (CRUD)</h3>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
            
            <div>
                <h4>📋 Gestão de Cargos</h4>
                <table>
                    <thead>
                        <tr><th>Cargo</th><th class="col-acoes">Ação</th></tr>
                    </thead>
                    <tbody>
                        {% for c in cargos %}
                        <tr>
                            <td>{{ c[1] }}</td>
                            <td class="col-acoes">
                                <form action="/horarios/excluir_cargo/{{ c[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-delete btn-sm" onclick="return confirm('Excluir cargo?')" aria-label="Excluir cargo">Excluir</button></form>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                <form action="/horarios/novo_cargo" method="POST" class="d-flex gap-2" style="margin-top: 10px;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <input type="text" name="nome_cargo" placeholder="Novo Cargo" required class="form-control" style="flex: 1;">
                    <button type="submit" class="btn btn-admin">Adicionar</button>
                </form>
            </div>

            <div>
                <h4>⏰ Tabela de Horários / Jornadas</h4>
                <table>
                    <thead>
                        <tr><th>Descrição</th><th>Tipo</th><th class="col-acoes">Ação</th></tr>
                    </thead>
                    <tbody>
                        {% for j in jornadas %}
                        <tr>
                            <td>{{ j[1] }}</td>
                            <td><strong>{{ j[2] }}</strong></td>
                            <td class="col-acoes">
                                <form action="/horarios/excluir_jornada/{{ j[0] }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-delete btn-sm" onclick="return confirm('Excluir jornada?')" aria-label="Excluir jornada">Excluir</button></form>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                <form action="/horarios/nova_jornada" method="POST" class="form-grid" style="grid-template-columns: 1fr 1fr;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <input type="text" name="descricao" placeholder="Ex: 08:00 às 12:00 (Sábado)" required class="form-control">
                    <select name="tipo" class="form-select">
                        <option value="Semana">Semana</option>
                        <option value="Sabado">Sábado</option>
                    </select>
                    <input type="text" name="m_in" placeholder="Início Manhã (08:00)" required class="form-control">
                    <input type="text" name="m_out" placeholder="Fim Manhã (12:00)" required class="form-control">
                    <button type="submit" class="btn btn-admin" style="grid-column: span 2;">Adicionar Horário</button>
                </form>
            </div>

        </div>
    </div>
    {% endif %}

    <dialog id="foto-ampliada" aria-labelledby="foto-ampliada-nome">
        <form method="dialog"><button type="submit" class="btn" autofocus>Fechar ✕</button></form>
        <h3 id="foto-ampliada-nome"></h3>
        <img alt="">
    </dialog>
    <script>
        const fotoDialog = document.getElementById('foto-ampliada');
        document.querySelectorAll('.foto-cobertura').forEach(botao => {
            botao.addEventListener('click', () => {
                const origem = botao.querySelector('img');
                const ampliada = fotoDialog.querySelector('img');
                ampliada.src = origem.src;
                ampliada.alt = origem.alt;
                document.getElementById('foto-ampliada-nome').textContent = botao.title;
                fotoDialog.showModal();
            });
        });
        fotoDialog.addEventListener('click', event => {
            const rect = fotoDialog.getBoundingClientRect();
            if (event.target === fotoDialog && (event.clientX < rect.left || event.clientX > rect.right ||
                event.clientY < rect.top || event.clientY > rect.bottom)) fotoDialog.close();
        });
        document.addEventListener("DOMContentLoaded", function() {
            var scrollpos = localStorage.getItem('scrollpos');
            if (scrollpos && !window.location.hash) {
                window.scrollTo(0, scrollpos);
            }
        });

        window.onbeforeunload = function() {
            localStorage.setItem('scrollpos', window.scrollY);
        };

        function ordenarTabela(tabelaId, colunaIndex) {
            const tabela = document.getElementById(tabelaId);
            if (!tabela) return;
            
            const tbody = tabela.querySelector("tbody");
            const linhas = Array.from(tbody.querySelectorAll("tr"));
            const th = tabela.querySelectorAll("th")[colunaIndex];
            
            const direcaoAtual = th.getAttribute("data-ordem") || "asc";
            const novaDirecao = direcaoAtual === "asc" ? "desc" : "asc";

            tabela.querySelectorAll("th").forEach(header => {
                header.removeAttribute("data-ordem");
            });
            th.setAttribute("data-ordem", novaDirecao);

            linhas.sort((linhaA, linhaB) => {
                const celulaA = linhaA.children[colunaIndex].dataset.sort ?? linhaA.children[colunaIndex].innerText.trim();
                const celulaB = linhaB.children[colunaIndex].dataset.sort ?? linhaB.children[colunaIndex].innerText.trim();

                const comparacao = celulaA.localeCompare(celulaB, 'pt-BR', { numeric: true, sensitivity: 'base' });
                return novaDirecao === "asc" ? comparacao : -comparacao;
            });

            linhas.forEach(linha => tbody.appendChild(linha));
        }

        function validarApenasSabado(input) {
            if (!input.value) return true;
            const partes = input.value.split('-');
            const data = new Date(partes[0], partes[1] - 1, partes[2], 12, 0, 0);
            if (data.getDay() !== 6) {
                alert('⚠️ Atenção: A data selecionada não cai em um Sábado!');
                input.value = '';
                return false;
            }
            return true;
        }

        function validarSabadoForm(input) {
            return validarApenasSabado(input);
        }

        const isAdmin = {{ 'true' if pode_editar_horarios() else 'false' }};

        function allowDrop(ev) {
            if (isAdmin) ev.preventDefault();
        }

        function drag(ev) {
            if (isAdmin) {
                ev.dataTransfer.setData("text", ev.target.id);
            }
        }

        function drop(ev) {
            if (!isAdmin) return;
            ev.preventDefault();
            var data = ev.dataTransfer.getData("text");
            var card = document.getElementById(data);
            var colunaDestino = ev.currentTarget;
            
            if (colunaDestino.classList.contains('coluna-turno')) {
                
                var escalaId = card.getAttribute('data-id');
                var novoHorario = colunaDestino.getAttribute('data-horario');
                
                csrfFetch('/horarios/atualizar_horario_escala', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: escalaId, horario: novoHorario })
                }).then(res => res.json()).then(data => {
                    if (!data.success) {
                        alert(data.erro || 'Não foi possível mover o funcionário.');
                        return;
                    }
                    const destino = new URL(window.location.href);
                    destino.searchParams.set('data_cobertura', data.data);
                    destino.searchParams.set('data_filtro_sabado', data.data);
                    destino.hash = 'secao-escala-sabado';
                    window.location.assign(destino);
                }).catch(() => alert('Falha de conexão. Recarregue a página para conferir a escala.'));
            }
        }

        function exportarEscalaPDF() {
            var element = document.getElementById('area-impressao-escala');
            document.body.classList.add('is-exporting');

            var opt = {
              margin:       [0.3, 0.3, 0.3, 0.3],
              filename:     'escala_sabado.pdf',
              image:        { type: 'jpeg', quality: 0.98 },
              html2canvas:  { scale: 2, useCORS: true, logging: false },
              jsPDF:        { unit: 'in', format: 'a4', orientation: 'landscape' }
            };

            html2pdf().set(opt).from(element).save().then(function() {
                document.body.classList.remove('is-exporting');
            });
        }
    </script>
</div>
</body>
</html>
"""

# --- ROTAS PRINCIPAIS ---
@horarios_bp.route('/horarios')
def ver_horarios():
    busca_func_id = request.args.get('busca_funcionario', '')
    data_trocas = request.args.get('data_trocas', '')
    data_filtro_sabado = request.args.get('data_filtro_sabado', '')
    data_cobertura = request.args.get('data_cobertura')
    if not data_cobertura:
        data_cobertura = date.today().strftime('%Y-%m-%d')
    try:
        validar_data(data_cobertura)
        if data_trocas:
            validar_data(data_trocas)
    except ValueError:
        return 'Informe uma data válida.', 400
    modo_todos = request.args.get('modo', '') == 'todos'
    
    if not data_filtro_sabado and not modo_todos and not busca_func_id:
        data_filtro_sabado = obter_proximo_sabado()

    data_filtro_sabado_formatada = ""
    if data_filtro_sabado:
        try:
            dt = datetime.strptime(data_filtro_sabado, '%Y-%m-%d')
            data_filtro_sabado_formatada = dt.strftime('%d/%m/%Y')
        except ValueError:
            pass

    data_cobertura_formatada = ""
    if data_cobertura:
        try:
            dt_cob = datetime.strptime(data_cobertura, '%Y-%m-%d')
            data_cobertura_formatada = dt_cob.strftime('%d/%m/%Y')
        except ValueError:
            pass

    ausencia_inicio = request.args.get('ausencia_inicio', '').strip()
    ausencia_fim = request.args.get('ausencia_fim', '').strip()
    ausencia_motivo = request.args.get('ausencia_motivo', '').strip()
    filtro_ausencias = bool(ausencia_inicio or ausencia_fim or ausencia_motivo)
    try:
        for valor in (ausencia_inicio, ausencia_fim):
            if valor:
                validar_data(valor)
        if ausencia_inicio and ausencia_fim and ausencia_inicio > ausencia_fim:
            raise ValueError()
    except ValueError:
        return 'Informe um período válido.', 400
    limite_inicio = ausencia_inicio or ('0001-01-01' if filtro_ausencias else date.today().isoformat())
    limite_fim = ausencia_fim or ('9999-12-31' if filtro_ausencias else date.today().isoformat())

    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT f.id, f.nome, f.cargo_id, f.jornada_id, f.equipe_sabado, f.eh_apoiador_sabado, c.nome, j.descricao, f.prioridade, f.eh_sobreaviso, f.foto_url, f.ativo, f.nao_trabalha_sabado,
                   strftime('%d/%m', f.data_nascimento)
            FROM funcionarios f
            LEFT JOIN cargos c ON f.cargo_id = c.id
            LEFT JOIN jornadas j ON f.jornada_id = j.id
            ORDER BY f.ativo DESC, f.nome ASC
        """)
        funcionarios = cursor.fetchall()

        cursor.execute("""
            SELECT a.id, f.nome, a.motivo, 
                   strftime('%d/%m/%Y', a.data_inicio), 
                   strftime('%d/%m/%Y', a.data_fim),
                   a.hora_inicio, a.hora_fim
            FROM ausencias a
            JOIN funcionarios f ON f.id = a.funcionario_id
            WHERE a.data_fim >= ? AND a.data_inicio <= ? AND (? = '' OR a.motivo = ?)
            ORDER BY a.data_inicio DESC
        """, (limite_inicio, limite_fim, ausencia_motivo, ausencia_motivo))
        ausencias = cursor.fetchall()
        motivos_ausencias = [r[0] for r in conn.execute('SELECT DISTINCT motivo FROM ausencias ORDER BY motivo')]
        substituicoes = conn.execute('''SELECT s.id, t.nome, f.nome, s.data_inicio, s.data_fim, s.motivo
            FROM substituicoes_periodo s JOIN funcionarios t ON t.id = s.titular_id
            JOIN funcionarios f ON f.id = s.substituto_id ORDER BY s.data_inicio DESC''').fetchall()
        
        cursor.execute("SELECT id, nome FROM cargos ORDER BY nome")
        cargos = cursor.fetchall()

        cursor.execute("SELECT id, descricao, tipo FROM jornadas ORDER BY tipo, descricao")
        jornadas = cursor.fetchall()

        query_escala = """
            SELECT e.id, strftime('%d/%m/%Y', e.data_sabado), e.cor_equipe, f.nome, e.horario, e.observacao, f.equipe_sabado, f.foto_url, f.ativo
            FROM escala_sabado e
            JOIN funcionarios f ON f.id = e.funcionario_id
            WHERE 1=1
        """
        params_escala = []

        if busca_func_id:
            query_escala += " AND e.funcionario_id = ?"
            params_escala.append(busca_func_id)

        if data_filtro_sabado:
            query_escala += " AND e.data_sabado = ?"
            params_escala.append(data_filtro_sabado)

        query_escala += " ORDER BY e.data_sabado DESC, e.horario ASC"
        cursor.execute(query_escala, params_escala)
        escala_sabados = cursor.fetchall()

        cor_equipe_dia = "Verde"
        if escala_sabados:
            cor_equipe_dia = escala_sabados[0][2]

        query_trocas = """
            SELECT t.id, strftime('%d/%m/%Y', t.data_sabado), COALESCE(f1.nome, 'Funcionário removido'), COALESCE(f2.nome, 'Funcionário removido'), t.motivo, t.detalhes, t.aplicada
            FROM trocas_sabado t
            LEFT JOIN funcionarios f1 ON f1.id = t.funcionario_substituido_id
            LEFT JOIN funcionarios f2 ON f2.id = t.funcionario_substituto_id
            WHERE 1=1
        """
        params_trocas = []

        if busca_func_id:
            query_trocas += " AND (t.funcionario_substituido_id = ? OR t.funcionario_substituto_id = ?)"
            params_trocas.extend([busca_func_id, busca_func_id])

        if data_trocas:
            query_trocas += " AND t.data_sabado = ?"
            params_trocas.append(data_trocas)

        query_trocas += " ORDER BY t.data_sabado DESC, t.id DESC"
        cursor.execute(query_trocas, params_trocas)
        trocas = cursor.fetchall()

    cobertura = calcular_cobertura_diaria(data_referencia=data_cobertura)

    return render_template_string(
        HTML_INTERFACE, 
        funcionarios=funcionarios, 
        cargos=cargos, 
        jornadas=jornadas,
        escala_sabados=escala_sabados,
        cor_equipe_dia=cor_equipe_dia,
        trocas=trocas,
        cobertura=cobertura,
        ausencias=ausencias, motivos_ausencias=motivos_ausencias,
        ausencia_inicio=ausencia_inicio, ausencia_fim=ausencia_fim, ausencia_motivo=ausencia_motivo,
        filtro_ausencias=filtro_ausencias, substituicoes=substituicoes,
        busca_func_id=busca_func_id,
        data_filtro_sabado=data_filtro_sabado,
        data_filtro_sabado_formatada=data_filtro_sabado_formatada,
        data_cobertura=data_cobertura,
        data_cobertura_formatada=data_cobertura_formatada,
        modo_todos=modo_todos, data_trocas=data_trocas, funcionario={}
    )

@horarios_bp.route('/horarios/salvar_funcionario', methods=['POST'])
@login_required
def salvar_funcionario():
    nome = request.form.get('nome', '').strip()
    prioridade = request.form.get('prioridade', '')
    equipe = request.form.get('equipe_sabado', '')
    tipo = request.form.get('tipo_sabado', '')
    nascimento = request.form.get('data_nascimento', '').strip()
    if nascimento:
        try:
            if validar_data(nascimento) > date.today():
                raise ValueError
        except ValueError:
            return 'Informe uma data de nascimento válida, que não seja futura.', 400
    try:
        func_id = int(request.form['id']) if request.form.get('id') else None
        cargo = int(request.form.get('cargo_id', ''))
        jornada = int(request.form.get('jornada_id', ''))
        ativo = int(request.form.get('ativo', '1'))
    except ValueError:
        return 'Informe cargo, jornada e funcionário válidos.', 400
    if (not nome or len(nome) > 150 or prioridade not in {'P1', 'P2', 'P3', 'P4', 'P5'}
            or equipe not in EQUIPES or tipo not in TIPOS_SABADO or ativo not in (0, 1)
            or (func_id is not None and func_id <= 0)):
        return 'Revise os dados do funcionário.', 400
    with closing(get_db()) as conn, conn:
        if func_id and not conn.execute('SELECT 1 FROM funcionarios WHERE id = ?', (func_id,)).fetchone():
            return 'Funcionário não encontrado.', 404
        if not conn.execute('SELECT 1 FROM cargos WHERE id = ?', (cargo,)).fetchone():
            return 'Cargo inválido.', 400
        if not conn.execute("SELECT 1 FROM jornadas WHERE id = ? AND tipo = 'Semana'", (jornada,)).fetchone():
            return 'Selecione uma jornada da semana.', 400
        foto_url = None
        foto = request.files.get('foto')
        if foto and foto.filename:
            if not extensao_permitida(foto.filename):
                return 'Formato de foto inválido.', 400
            foto.seek(0, os.SEEK_END)
            tamanho = foto.tell()
            foto.seek(0)
            if tamanho > MAX_FOTO_BYTES:
                return 'Foto maior que o limite permitido.', 400
            nome_foto = f"{datetime.now():%Y%m%d%H%M%S%f}_{secure_filename(foto.filename)}"
            foto.save(os.path.join(UPLOAD_FOLDER, nome_foto))
            foto_url = f'/static/fotos/{nome_foto}'
        valores = (nome, prioridade, cargo, jornada, equipe, int(tipo == 'apoio'),
                   int(tipo == 'sobreaviso'), int(tipo == 'nenhum'), ativo, nascimento or None)
        if func_id:
            conn.execute('''UPDATE funcionarios SET nome=?, prioridade=?, cargo_id=?, jornada_id=?,
                equipe_sabado=?, eh_apoiador_sabado=?, eh_sobreaviso=?, nao_trabalha_sabado=?, ativo=?,
                data_nascimento=?, foto_url=COALESCE(?, foto_url) WHERE id=?''', (*valores, foto_url, func_id))
            acao = 'atualizar_funcionario'
        else:
            conn.execute('''INSERT INTO funcionarios (nome, prioridade, cargo_id, jornada_id,
                equipe_sabado, eh_apoiador_sabado, eh_sobreaviso, nao_trabalha_sabado, ativo, data_nascimento, foto_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (*valores, foto_url or ''))
            acao = 'criar_funcionario'
    
    # Auditoria: criação/atualização de funcionário (Opção 3)
    registrar_acao_admin(acao, {
        'funcionario_id': func_id,
        'nome': nome,
        'prioridade': prioridade,
        'equipe_sabado': equipe,
        'tipo_sabado': tipo,
        'ativo': ativo,
    })
    
    return redirect(url_for('horarios.ver_horarios') + '#secao-equipe')


@horarios_bp.route('/horarios/editar_funcionario/<int:id>')
@login_required
def editar_funcionario(id):
    with closing(get_db()) as conn:
        funcionario = conn.execute('SELECT * FROM funcionarios WHERE id = ?', (id,)).fetchone()
        if not funcionario:
            return 'Funcionário não encontrado.', 404
        cargos = conn.execute('SELECT id, nome FROM cargos ORDER BY nome').fetchall()
        jornadas = conn.execute("SELECT id, descricao, tipo FROM jornadas WHERE tipo = 'Semana' ORDER BY descricao").fetchall()
    return render_template('editar_funcionario.html', funcionario=dict(funcionario), cargos=cargos, jornadas=jornadas)

@horarios_bp.route('/horarios/gerar_sugestao_sabado', methods=['POST'])
@login_required
def gerar_sugestao_sabado():
    data_sabado = request.form.get('data_sabado')
    if not data_sabado:
        return redirect('/horarios#secao-escala-sabado')

    try:
        dt = datetime.strptime(data_sabado, '%Y-%m-%d')
        if dt.weekday() != 5:
            flash(f"A data selecionada ({dt.strftime('%d/%m/%Y')}) não é um Sábado!")
            return redirect('/horarios#secao-escala-sabado')
    except ValueError:
        flash("Data inválida informada!")
        return redirect('/horarios#secao-escala-sabado')

    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT funcionario_id FROM ausencias 
            WHERE ? BETWEEN data_inicio AND data_fim
        """, (data_sabado,))
        ausentes = [row[0] for row in cursor.fetchall()]

        if cursor.execute('SELECT 1 FROM trocas_sabado WHERE data_sabado = ? AND aplicada = 1', (data_sabado,)).fetchone():
            flash('Este sábado tem trocas ou ajustes registrados. Edite a escala para preservar essas alterações.')
            return redirect(url_for('horarios.ver_horarios', data_filtro_sabado=data_sabado))
        cursor.execute("DELETE FROM escala_sabado WHERE data_sabado = ?", (data_sabado,))

        cursor.execute("SELECT cor_equipe FROM escala_sabado ORDER BY id DESC LIMIT 1")
        ultimo = cursor.fetchone()
        cor_principal = 'Verde' if (ultimo and ultimo[0] == 'Amarela') else 'Amarela'
        cor_oposta = 'Amarela' if cor_principal == 'Verde' else 'Verde'

        turnos = [
            "07:30 - 11:30",
            "08:00 - 12:00",
            "08:00 - 12:00",
            "09:00 - 13:00",
            "10:00 - 14:00",
            "13:00 - 17:00",
            "14:00 - 18:00"
        ]

        cursor.execute("""
            SELECT f.id, f.nome 
            FROM funcionarios f
            LEFT JOIN cargos c ON f.cargo_id = c.id
            WHERE f.ativo = 1 AND f.nao_trabalha_sabado = 0
              AND f.equipe_sabado = ? 
              AND (c.nome LIKE '%Suporte%' OR f.cargo_id IS NULL)
              AND f.eh_apoiador_sabado = 0 
              AND f.eh_sobreaviso = 0
        """, (cor_principal,))
        tecnicos_principais = [t for t in cursor.fetchall() if t[0] not in ausentes]

        tecnicos_suporte = list(tecnicos_principais)
        if len(tecnicos_suporte) < len(turnos):
            cursor.execute("""
                SELECT f.id, f.nome 
                FROM funcionarios f
                LEFT JOIN cargos c ON f.cargo_id = c.id
                WHERE f.ativo = 1 AND f.nao_trabalha_sabado = 0
                  AND f.equipe_sabado = ? 
                  AND (c.nome LIKE '%Suporte%' OR f.cargo_id IS NULL)
                  AND f.eh_apoiador_sabado = 0 
                  AND f.eh_sobreaviso = 0
            """, (cor_oposta,))
            tecnicos_opostos = [t for t in cursor.fetchall() if t[0] not in ausentes]
            
            for tec in tecnicos_opostos:
                if len(tecnicos_suporte) < len(turnos):
                    tecnicos_suporte.append(tec)

        for idx, turno in enumerate(turnos):
            if idx < len(tecnicos_suporte):
                tec_id = tecnicos_suporte[idx][0]
                obs = 'Atendimento Rodízio' if idx < len(tecnicos_principais) else f'Atendimento Rodízio (Apoio Eq. {cor_oposta})'
                cursor.execute("""
                    INSERT INTO escala_sabado (data_sabado, cor_equipe, funcionario_id, horario, observacao)
                    VALUES (?, ?, ?, ?, ?)
                """, (data_sabado, cor_principal, tec_id, turno, obs))

        cursor.execute("""
            SELECT id FROM funcionarios 
            WHERE ativo = 1 AND nao_trabalha_sabado = 0 AND equipe_sabado = ? AND eh_apoiador_sabado = 1
        """, (cor_principal,))
        for ap in cursor.fetchall():
            if ap[0] not in ausentes:
                cursor.execute("""
                    INSERT INTO escala_sabado (data_sabado, cor_equipe, funcionario_id, horario, observacao)
                    VALUES (?, ?, ?, '08:00 - 12:00', 'Apoio Fixo (Dev/Com/Fin)')
                """, (data_sabado, cor_principal, ap[0]))

        cursor.execute("""
            SELECT id FROM funcionarios 
            WHERE ativo = 1 AND nao_trabalha_sabado = 0 AND equipe_sabado = ? AND eh_sobreaviso = 1
        """, (cor_principal,))
        for sb in cursor.fetchall():
            if sb[0] not in ausentes:
                cursor.execute("""
                    INSERT INTO escala_sabado (data_sabado, cor_equipe, funcionario_id, horario, observacao)
                    VALUES (?, ?, ?, 'Sobreaviso', 'Sobreaviso Oficial da Equipe')
                """, (data_sabado, cor_principal, sb[0]))
        conn.commit()

    return redirect(url_for('horarios.ver_horarios', data_filtro_sabado=data_sabado) + '#secao-escala-sabado')

@horarios_bp.route('/horarios/adicionar_item_escala', methods=['POST'])
@login_required
def adicionar_item_escala():
    data_sabado = request.form.get('data_sabado')
    funcionario_id = request.form.get('funcionario_id')
    horario = request.form.get('horario')
    tipo_sabado = request.form.get('tipo_sabado', 'rodizio')
    observacao = request.form.get('observacao', '')

    try:
        if validar_data(data_sabado).weekday() != 5:
            raise ValueError('Selecione um sábado.')
        funcionario_id = int(funcionario_id)
        if horario not in FAIXAS_SABADO or len(observacao) > 500:
            raise ValueError('Horário ou observação inválidos.')
        with closing(get_db()) as conn:
            validar_participante_sabado(conn, funcionario_id)
    except (ValueError, TypeError) as erro:
        return jsonify(erro=str(erro)), 400

    # Define a observação padrão caso não tenha sido preenchida manualmente
    if not observacao:
        if tipo_sabado == 'apoio':
            observacao = 'Apoio Fixo (Dev/Com/Fin)'
        elif tipo_sabado == 'sobreaviso':
            observacao = 'Sobreaviso Oficial da Equipe'
        else:
            observacao = 'Atendimento Rodízio'

    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("SELECT cor_equipe FROM escala_sabado WHERE data_sabado = ? LIMIT 1", (data_sabado,))
        row = cursor.fetchone()
        cor_equipe = row[0] if row else 'Verde'

        cursor.execute("""
            INSERT INTO escala_sabado (data_sabado, cor_equipe, funcionario_id, horario, observacao)
            VALUES (?, ?, ?, ?, ?)
        """, (data_sabado, cor_equipe, funcionario_id, horario, observacao))
        conn.commit()

    return redirect(url_for('horarios.ver_horarios', data_filtro_sabado=data_sabado) + '#secao-escala-sabado')

@horarios_bp.route('/horarios/editar_item_escala/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_item_escala(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        if request.method == 'POST':
            try:
                data_sabado = ajustar_escala(conn, id, int(request.form.get('funcionario_id', '')),
                    request.form.get('horario'), request.form.get('observacao', ''), session['user_id'])
            except ValueError as erro:
                return jsonify(erro=str(erro)), 400
            return redirect(url_for('horarios.ver_horarios', data_filtro_sabado=data_sabado,
                                    data_cobertura=data_sabado) + '#secao-escala-sabado')

        cursor.execute("SELECT id, data_sabado, funcionario_id, horario, observacao FROM escala_sabado WHERE id = ?", (id,))
        item = cursor.fetchone()
        if not item:
            return redirect('/horarios#secao-escala-sabado')
            
        cursor.execute("SELECT id, nome FROM funcionarios WHERE ativo = 1 AND nao_trabalha_sabado = 0 ORDER BY nome")
        funcs = cursor.fetchall()

    item_id, d_sabado, f_id, h_faixa, obs = item
    faixas = ["07:30 - 11:30", "08:00 - 12:00", "09:00 - 13:00", "10:00 - 14:00", "13:00 - 17:00", "14:00 - 18:00", "Sobreaviso"]

    opts_funcs = "".join([f'<option value="{f[0]}" {"selected" if f[0]==f_id else ""}>{escape(f[1])}</option>' for f in funcs])
    opts_faixas = "".join([f'<option value="{fx}" {"selected" if fx==h_faixa else ""}>{fx}</option>' for fx in faixas])

    return f"""
    <link rel="stylesheet" href="/static/ui.css">
    <script src="/static/ui.js" defer></script>
    <div style="max-width: 450px; margin: 50px auto; font-family: sans-serif; padding: 20px; border: 1px solid #ccc; border-radius: 8px; background: white;">
        <h3>✏️ Editar Item da Escala</h3>
        <form method="POST" style="display: grid; gap: 12px;">
            <input type="hidden" name="csrf_token" value="{csrf_token()}">
            <input type="hidden" name="data_sabado" value="{d_sabado}">
            <div>
                <label style="font-size: 0.85em;">Técnico / Colaborador:</label>
                <select name="funcionario_id" required style="width: 100%; padding: 8px;">
                    {opts_funcs}
                </select>
            </div>
            <div>
                <label style="font-size: 0.85em;">Horário / Turno:</label>
                <select name="horario" required style="width: 100%; padding: 8px;">
                    {opts_faixas}
                </select>
            </div>
            <div>
                <label style="font-size: 0.85em;">Observação:</label>
                <input type="text" name="observacao" value="{escape(obs or '')}" style="width: 100%; padding: 8px;">
            </div>
            <button type="submit" class="btn btn-primary">Salvar Alteração</button>
            <a href="/horarios?data_filtro_sabado={d_sabado}#secao-escala-sabado" class="btn btn-secondary" style="text-align: center;">Cancelar</a>
        </form>
    </div>
    """

@horarios_bp.route('/horarios/atualizar_horario_escala', methods=['POST'])
@login_required
def atualizar_horario_escala():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return jsonify(success=False, erro='Dados inválidos.'), 400
    try:
        escala_id = int(dados.get('id', ''))
        with closing(get_db()) as conn, conn:
            data = ajustar_escala(conn, escala_id, None, dados.get('horario'), None, session['user_id'])
    except (ValueError, TypeError) as erro:
        return jsonify(success=False, erro=str(erro)), 400
    return jsonify(success=True, data=data)

@horarios_bp.route('/horarios/salvar_ausencia', methods=['POST'])
@login_required
def salvar_ausencia():
    ausencia_id = request.form.get('id')
    funcionario_id = request.form['funcionario_id']
    motivo = request.form['motivo']
    data_inicio = request.form['data_inicio']
    data_fim = request.form['data_fim']
    hora_inicio = request.form.get('hora_inicio', '')
    hora_fim = request.form.get('hora_fim', '')

    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        if ausencia_id:
            cursor.execute("""
                UPDATE ausencias 
                SET funcionario_id=?, motivo=?, data_inicio=?, data_fim=?, hora_inicio=?, hora_fim=?
                WHERE id=?
            """, (funcionario_id, motivo, data_inicio, data_fim, hora_inicio, hora_fim, ausencia_id))
        else:
            cursor.execute("""
                INSERT INTO ausencias (funcionario_id, motivo, data_inicio, data_fim, hora_inicio, hora_fim)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (funcionario_id, motivo, data_inicio, data_fim, hora_inicio, hora_fim))
        conn.commit()

    return redirect('/horarios#secao-ausencias')

@horarios_bp.route('/horarios/editar_ausencia/<int:id>')
@login_required
def editar_ausencia(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, funcionario_id, motivo, data_inicio, data_fim, hora_inicio, hora_fim 
            FROM ausencias WHERE id = ?
        """, (id,))
        aus = cursor.fetchone()
        cursor.execute("SELECT id, nome FROM funcionarios WHERE ativo = 1 ORDER BY nome")
        funcs = cursor.fetchall()

    if not aus:
        return redirect('/horarios#secao-ausencias')

    a_id, f_id, motivo, d_in, d_fim, h_in, h_fim = aus
    opts_funcs = "".join([f'<option value="{f[0]}" {"selected" if f[0]==f_id else ""}>{escape(f[1])}</option>' for f in funcs])

    return f"""
    <link rel="stylesheet" href="/static/ui.css">
    <script src="/static/ui.js" defer></script>
    <div style="max-width: 500px; margin: 40px auto; font-family: sans-serif; padding: 20px; border: 1px solid #ccc; border-radius: 8px; background: white;">
        <h3>✏️ Editar Ausência / Licença</h3>
        <form action="/horarios/salvar_ausencia" method="POST" style="display: grid; gap: 12px;">
            <input type="hidden" name="csrf_token" value="{csrf_token()}">
            <input type="hidden" name="id" value="{a_id}">
            
            <div>
                <label style="font-size: 0.85em;">Técnico:</label>
                <select name="funcionario_id" required style="width: 100%; padding: 8px;">
                    {opts_funcs}
                </select>
            </div>

            <div>
                <label style="font-size: 0.85em;">Motivo:</label>
                <input type="text" name="motivo" value="{escape(motivo)}" required style="width: 100%; padding: 8px;">
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                <div>
                    <label style="font-size: 0.85em;">Data Início:</label>
                    <input type="date" name="data_inicio" value="{d_in}" required style="width: 100%; padding: 8px;">
                </div>
                <div>
                    <label style="font-size: 0.85em;">Hora Início (Opcional):</label>
                    <input type="time" name="hora_inicio" value="{h_in or ''}" style="width: 100%; padding: 8px;">
                </div>
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                <div>
                    <label style="font-size: 0.85em;">Data Fim:</label>
                    <input type="date" name="data_fim" value="{d_fim}" required style="width: 100%; padding: 8px;">
                </div>
                <div>
                    <label style="font-size: 0.85em;">Hora Fim (Opcional):</label>
                    <input type="time" name="hora_fim" value="{h_fim or ''}" style="width: 100%; padding: 8px;">
                </div>
            </div>

            <button type="submit" class="btn btn-success">Salvar Ausência</button>
            <a href="/horarios#secao-ausencias" class="btn btn-secondary" style="text-align: center;">Cancelar</a>
        </form>
    </div>
    """

@horarios_bp.route('/horarios/excluir_ausencia/<int:id>', methods=['POST'])
@login_required
def excluir_ausencia(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        # Busca info da ausência para auditoria
        ausencia = conn.execute("SELECT funcionario_id, motivo, data_inicio, data_fim FROM ausencias WHERE id = ?", (id,)).fetchone()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ausencias WHERE id = ?", (id,))
        conn.commit()
    
    # Auditoria: exclusão de ausência (Opção 3)
    if ausencia:
        registrar_acao_admin('excluir_ausencia', {
            'ausencia_id': id,
            'funcionario_id': ausencia['funcionario_id'],
            'motivo': ausencia['motivo'],
            'data_inicio': ausencia['data_inicio'],
            'data_fim': ausencia['data_fim'],
        })
    
    return redirect('/horarios#secao-ausencias')

@horarios_bp.route('/horarios/excluir_dia_inteiro/<data_sabado>', methods=['POST'])
@login_required
def excluir_dia_inteiro(data_sabado):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        # Conta quantas escalas serão excluídas para auditoria
        count = conn.execute("SELECT COUNT(*) FROM escala_sabado WHERE data_sabado = ?", (data_sabado,)).fetchone()[0]
        cursor = conn.cursor()
        cursor.execute("DELETE FROM escala_sabado WHERE data_sabado = ?", (data_sabado,))
        conn.commit()
    
    # Auditoria: exclusão de dia inteiro de escala (Opção 3)
    registrar_acao_admin('excluir_dia_inteiro_escala', {
        'data_sabado': data_sabado,
        'escalas_excluidas': count,
    })
    
    return redirect('/horarios#secao-escala-sabado')

@horarios_bp.route('/horarios/novo_cargo', methods=['POST'])
@login_required
def novo_cargo():
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO cargos (nome) VALUES (?)", (request.form['nome_cargo'],))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
    return redirect('/horarios#secao-cargos-jornadas')

@horarios_bp.route('/horarios/excluir_cargo/<int:id>', methods=['POST'])
@login_required
def excluir_cargo(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        # Busca info do cargo para auditoria
        cargo = conn.execute("SELECT nome FROM cargos WHERE id = ?", (id,)).fetchone()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cargos WHERE id = ?", (id,))
        conn.commit()
    
    # Auditoria: exclusão de cargo (Opção 3)
    if cargo:
        registrar_acao_admin('excluir_cargo', {
            'cargo_id': id,
            'nome': cargo['nome'],
        })
    
    return redirect('/horarios#secao-cargos-jornadas')

@horarios_bp.route('/horarios/nova_jornada', methods=['POST'])
@login_required
def nova_jornada():
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO jornadas (descricao, tipo, manha_inicio, manha_fim)
            VALUES (?, ?, ?, ?)
        """, (request.form['descricao'], request.form['tipo'], request.form['m_in'], request.form['m_out']))
        conn.commit()
    return redirect('/horarios#secao-cargos-jornadas')

@horarios_bp.route('/horarios/excluir_jornada/<int:id>', methods=['POST'])
@login_required
def excluir_jornada(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        # Busca info da jornada para auditoria
        jornada = conn.execute("SELECT descricao, tipo FROM jornadas WHERE id = ?", (id,)).fetchone()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM jornadas WHERE id = ?", (id,))
        conn.commit()
    
    # Auditoria: exclusão de jornada (Opção 3)
    if jornada:
        registrar_acao_admin('excluir_jornada', {
            'jornada_id': id,
            'descricao': jornada['descricao'],
            'tipo': jornada['tipo'],
        })
    
    return redirect('/horarios#secao-cargos-jornadas')

@horarios_bp.route('/horarios/registrar_troca', methods=['POST'])
@login_required
def registrar_troca():
    data = request.form.get('data_sabado', '')
    motivo = request.form.get('motivo', '').strip()
    try:
        titular = int(request.form.get('substituido_id', ''))
        substituto = int(request.form.get('substituto_id', ''))
        if len(motivo) > 500:
            raise ValueError('O motivo deve ter até 500 caracteres.')
        with closing(get_db()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            aplicar_troca(conn, data, titular, substituto, motivo, session['user_id'])
    except ValueError as erro:
        return jsonify(erro=str(erro)), 400
    
    # Auditoria: registro de troca (Opção 3)
    registrar_acao_admin('registrar_troca', {
        'data_sabado': data,
        'titular_id': titular,
        'substituto_id': substituto,
        'motivo': motivo,
    })
    
    return redirect(url_for('horarios.ver_horarios', data_cobertura=data, data_trocas=data,
                            data_filtro_sabado=data if validar_data(data).weekday() == 5 else '') + '#secao-trocas')

@horarios_bp.route('/horarios/excluir_funcionario/<int:id>', methods=['POST'])
@login_required
def excluir_funcionario(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        # Habilita constraints de chave estrangeira
        conn.execute("PRAGMA foreign_keys = ON")
        
        # Busca info do funcionário para auditoria
        funcionario = conn.execute("SELECT nome FROM funcionarios WHERE id = ?", (id,)).fetchone()
        
        # Remove registros relacionados antes de excluir o funcionário
        cursor = conn.cursor()
        cursor.execute("DELETE FROM escala_sabado WHERE funcionario_id = ?", (id,))
        cursor.execute("DELETE FROM ausencias WHERE funcionario_id = ?", (id,))
        cursor.execute("DELETE FROM funcionarios WHERE id = ?", (id,))
        conn.commit()
    
    # Auditoria: exclusão de funcionário (Opção 3)
    if funcionario:
        registrar_acao_admin('excluir_funcionario', {
            'funcionario_id': id,
            'nome': funcionario[0],
        })
    
    return redirect('/horarios#secao-equipe')

@horarios_bp.route('/horarios/excluir_escala_sabado/<int:id>', methods=['POST'])
@login_required
def excluir_escala_sabado(id):
    with closing(sqlite3.connect(DB_NAME)) as conn, conn:
        # Busca info da escala para auditoria
        escala = conn.execute("SELECT data_sabado, funcionario_id, horario FROM escala_sabado WHERE id = ?", (id,)).fetchone()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM escala_sabado WHERE id = ?", (id,))
        conn.commit()
    
    # Auditoria: exclusão de escala de sábado (Opção 3)
    if escala:
        registrar_acao_admin('excluir_escala_sabado', {
            'escala_id': id,
            'data_sabado': escala['data_sabado'],
            'funcionario_id': escala['funcionario_id'],
            'horario': escala['horario'],
        })
    
    return redirect('/horarios#secao-escala-sabado')

@horarios_bp.route('/horarios/login', methods=['GET', 'POST'])
def login():
    # FIX: login por senha única fixa removido. Agora usa o mesmo login de usuário do painel de bancos.
    return redirect(url_for('bancos.admin_login', next='/horarios'))

@horarios_bp.route('/horarios/logout', methods=['POST'])
def logout():
    user_id = session.get('user_id')
    user_nome = session.get('user_nome')
    session.clear()
    if user_id and user_nome:
        from seguranca import registrar_logout
        registrar_logout(user_id, user_nome)
    return redirect(url_for('horarios.ver_horarios'))


@horarios_bp.route('/horarios/substituicao_periodo', methods=['POST'])
@login_required
def salvar_substituicao_periodo():
    try:
        titular = int(request.form.get('titular_id', ''))
        substituto = int(request.form.get('substituto_id', ''))
    except ValueError:
        return 'Selecione dois funcionários válidos.', 400
    try:
        with closing(get_db()) as conn, conn:
            registrar_substituicao(conn, titular, substituto,
                request.form.get('data_inicio', ''), request.form.get('data_fim', ''),
                request.form.get('motivo', '').strip(), session['user_id'])
    except ValueError as erro:
        return str(erro), 400
    
    # Auditoria: substituição de período (Opção 3)
    registrar_acao_admin('salvar_substituicao_periodo', {
        'titular_id': titular,
        'substituto_id': substituto,
        'data_inicio': request.form.get('data_inicio', ''),
        'data_fim': request.form.get('data_fim', ''),
        'motivo': request.form.get('motivo', '').strip(),
    })
    
    return redirect('/horarios#secao-trocas')


@horarios_bp.route('/horarios/excluir_substituicao/<int:id>', methods=['POST'])
@login_required
def excluir_substituicao_periodo(id):
    with closing(get_db()) as conn, conn:
        # Busca info da substituição para auditoria
        substituicao = conn.execute("SELECT titular_id, substituto_id, data_inicio, data_fim, motivo FROM substituicoes_periodo WHERE id = ?", (id,)).fetchone()
        conn.execute('DELETE FROM substituicoes_periodo WHERE id = ?', (id,))
    
    # Auditoria: exclusão de substituição de período (Opção 3)
    if substituicao:
        registrar_acao_admin('excluir_substituicao_periodo', {
            'substituicao_id': id,
            'titular_id': substituicao['titular_id'],
            'substituto_id': substituicao['substituto_id'],
            'data_inicio': substituicao['data_inicio'],
            'data_fim': substituicao['data_fim'],
            'motivo': substituicao['motivo'],
        })
    
    return redirect('/horarios#secao-trocas')
