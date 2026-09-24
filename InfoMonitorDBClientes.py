from dotenv import load_dotenv
import os

# FIX: carregar o .env ANTES de importar bancos/horarios, pois esses módulos
# leem variáveis de ambiente (ex: SERVIDORES_CONFIG) assim que são importados.
load_dotenv()

from flask import Flask, redirect, url_for, jsonify, send_from_directory
from bancos import bancos_bp
from horarios import horarios_bp
from seguranca import aplicar_config_flask

app = Flask(__name__)
aplicar_config_flask(app)

app.register_blueprint(bancos_bp)
app.register_blueprint(horarios_bp)

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
