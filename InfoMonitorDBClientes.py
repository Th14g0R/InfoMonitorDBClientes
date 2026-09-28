from dotenv import load_dotenv
import os

# FIX: carregar o .env ANTES de importar bancos/horarios, pois esses módulos
# leem variáveis de ambiente (ex: SERVIDORES_CONFIG) assim que são importados.
load_dotenv()

from flask import Flask, redirect, url_for, jsonify, send_from_directory, render_template_string, request
from bancos import bancos_bp
from horarios import horarios_bp
from seguranca import aplicar_config_flask

app = Flask(__name__)
aplicar_config_flask(app)

app.register_blueprint(bancos_bp)
app.register_blueprint(horarios_bp)

@app.errorhandler(500)
def erro_interno(e):
    if request.is_json or request.path.startswith('/api/'):
        return jsonify(sucesso=False, erro="Erro interno do servidor. A equipe técnica foi notificada."), 500
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <title>Erro de Processamento - InfoMonitor</title>
        <link rel="stylesheet" href="/static/ui.css">
        <style>
            .erro-box { max-width: 580px; margin: 80px auto; padding: 32px; text-align: center; background: var(--cor-superficie, #fff); border-radius: 12px; border: 1px solid var(--cor-borda, #ddd); box-shadow: 0 4px 16px rgba(0,0,0,0.08); font-family: inherit; }
            .erro-box h1 { font-size: 1.8rem; color: #dc3545; margin-bottom: 12px; }
            .erro-box p { font-size: 0.95rem; color: var(--cor-texto, #333); margin-bottom: 24px; line-height: 1.5; }
            .erro-acoes { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
        </style>
    </head>
    <body>
        <div class="erro-box">
            <h1>⚠️ Ocorreu um erro no procedimento</h1>
            <p>Não foi possível concluir a ação solicitada no momento.<br>Nenhuma alteração indevida foi salva no banco de dados.</p>
            <div class="erro-acoes">
                <a href="javascript:history.back()" class="btn btn-secondary">⬅️ Voltar</a>
                <a href="/horarios" class="btn btn-primary">📅 Painel de Horários</a>
                <a href="/" class="btn btn-bancos">🖥️ Monitor de Bancos</a>
            </div>
        </div>
    </body>
    </html>
    """), 500

@app.errorhandler(404)
def nao_encontrado(e):
    if request.is_json or request.path.startswith('/api/'):
        return jsonify(sucesso=False, erro="Recurso não encontrado."), 404
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <title>Página Não Encontrada - InfoMonitor</title>
        <link rel="stylesheet" href="/static/ui.css">
        <style>
            .erro-box { max-width: 540px; margin: 80px auto; padding: 32px; text-align: center; background: var(--cor-superficie, #fff); border-radius: 12px; border: 1px solid var(--cor-borda, #ddd); box-shadow: 0 4px 16px rgba(0,0,0,0.08); font-family: inherit; }
            .erro-box h1 { font-size: 1.8rem; color: #ffc107; margin-bottom: 12px; }
            .erro-box p { font-size: 0.95rem; color: var(--cor-texto, #333); margin-bottom: 24px; }
        </style>
    </head>
    <body>
        <div class="erro-box">
            <h1>🔍 Página Não Encontrada (404)</h1>
            <p>O endereço solicitado não existe ou foi removido.</p>
            <a href="/horarios" class="btn btn-primary">Ir para Início</a>
        </div>
    </body>
    </html>
    """), 404

@app.get('/healthz')
def healthz():
    """Sonda barata: nao consulta SSH, backups nem bancos remotos."""
    return jsonify(status='ok'), 200

@app.get('/static/fotos/<path:nome>')
def foto_dados(nome):
    data_dir = os.getenv('DATA_DIR', '').strip()
    if not data_dir:
        return app.send_static_file(f'fotos/{nome}')
    return send_from_directory(os.path.join(data_dir, 'fotos'), nome)

# ADICIONE ESTA ROTA PARA EVITAR ERRO AO ACESSAR A RAIZ
@app.route('/')
def index():
    return redirect(url_for('horarios.ver_horarios'))

if __name__ == '__main__':
    host = os.getenv('BIND_HOST', '0.0.0.0')
    porta = int(os.getenv('BIND_PORT', '8888'))
    # BIND_HOST=0.0.0.0 no .env se precisar expor na rede interna
    app.run(host=host, port=porta, debug=False)
