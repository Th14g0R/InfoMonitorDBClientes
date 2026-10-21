"""Ponto de entrada WSGI de producao (um processo Waitress)."""

import ipaddress
import os
import re
import sys

from dotenv import load_dotenv


def _inteiro_config(nome, padrao, minimo, maximo):
    valor = os.getenv(nome, str(padrao)).strip()
    try:
        numero = int(valor)
    except ValueError as exc:
        raise SystemExit(f"{nome} deve ser um numero inteiro.") from exc
    if not minimo <= numero <= maximo:
        raise SystemExit(f"{nome} deve estar entre {minimo} e {maximo}.")
    return numero


def _host_config():
    bind_env = os.getenv("BIND_HOST")
    host = bind_env.strip() if bind_env else "127.0.0.1"
    if not host or len(host) > 253 or any(c.isspace() for c in host):
        raise SystemExit("BIND_HOST deve ser um IP ou nome de host valido.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(
            r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?",
            host,
        ):
            raise SystemExit("BIND_HOST deve ser um IP ou nome de host valido.")
    if not bind_env:
        sys.stderr.write(
            "\n" + "=" * 78 + "\n"
            " [AVISO DE CONFIGURAÇÃO - BIND_HOST]\n"
            " BIND_HOST não está definido no arquivo .env.\n"
            f" Usando padrão seguro local: {host} (o sistema NÃO responderá fora desta máquina).\n"
            " Se precisar acessar de outras máquinas na rede interna, configure no seu .env:\n"
            "   BIND_HOST=0.0.0.0\n"
            + "=" * 78 + "\n\n"
        )
    else:
        sys.stderr.write(f"[INFO] Servidor Waitress iniciando com BIND_HOST={host}\n")
    return host


def configuracao_servidor():
    """Carrega e valida somente opcoes aceitas pelo servidor HTTP."""
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 ou superior e obrigatorio.")
    load_dotenv()
    return {
        "host": _host_config(),
        "port": _inteiro_config("BIND_PORT", 8888, 1, 65535),
        "threads": _inteiro_config("WEB_THREADS", 4, 1, 64),
    }


def main():
    config = configuracao_servidor()
    from waitress import serve
    from InfoMonitorDBClientes import app

    serve(app, **config)


if __name__ == "__main__":
    main()
