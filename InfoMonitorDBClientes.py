from dotenv import load_dotenv
import os

# FIX: carregar o .env ANTES de importar bancos/horarios, pois esses módulos
# leem variáveis de ambiente (ex: SERVIDORES_CONFIG) assim que são importados.
load_dotenv()

from flask import Flask, redirect, url_for
from bancos import bancos_bp
from horarios import horarios_bp
from seguranca import aplicar_config_flask

app = Flask(__name__)
aplicar_config_flask(app)

app.register_blueprint(bancos_bp)
app.register_blueprint(horarios_bp)

# ADICIONE ESTA ROTA PARA EVITAR ERRO AO ACESSAR A RAIZ
@app.route('/')
def index():
    return redirect(url_for('horarios.ver_horarios'))

if __name__ == '__main__':
    host = os.getenv('BIND_HOST', '0.0.0.0')
    porta = int(os.getenv('BIND_PORT', '8888'))
    # BIND_HOST=0.0.0.0 no .env se precisar expor na rede interna
    app.run(host=host, port=porta, debug=False)
