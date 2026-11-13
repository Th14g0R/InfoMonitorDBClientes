"""Módulo gerador e serializador de listas Cobian Backup 11 (Gravity) .lst.

Gera arquivos no formato UTF-16LE com BOM, quebras CRLF, envelope {11// e //11} por tarefa,
gramática estrita de propriedades 'Chave=Valor' sem espaços ao redor do igual, codificação IEEE 754 de 64 bits
(Delphi TDateTime) para agendamentos e sanitização estrita contra injeção de linhas e comandos.
"""
import base64
import datetime
import re
import struct
import uuid

# Base Delphi TDateTime (1899-12-30 00:00:00)
DELPHI_EPOCH = datetime.date(1899, 12, 30)

# Limites estruturais
MAX_TAREFAS_POR_LISTA = 10
MAX_LEN_NOME = 80
MAX_LEN_CAMINHO = 260
MAX_LEN_COMENTARIO = 120
MAX_LEN_GENERICO = 260

# Caracteres de controle estendidos: \x00-\x1f, DEL (\x7f), NEL (\x85), Line Separator (\u2028), Paragraph Separator (\u2029)
CONTROL_CHARS_PATTERN = re.compile(r'[\x00-\x1f\x7f\x85\u2028\u2029]')

# Validação estrita de Base64 para hashes de senha do Cobian
BASE64_STRICT_PATTERN = re.compile(r'^[A-Za-z0-9+/]+={0,2}$')

# Allowlist de tipos de eventos permitidos no Cobian 11
PRE_EVENTOS_PERMITIDOS = {'execute', 'stop_service', 'nenhum', ''}
POS_EVENTOS_PERMITIDOS = {'start_service', 'nenhum', ''}

# Padrão de hostname / IP simplificado e seguro (sem caracteres perigosos)
HOST_PATTERN = re.compile(r'^[a-zA-Z0-9.-]+$')


def validar_base64_estrito(valor: str) -> str:
    """
    Valida se uma string é Base64 válida e canônica:
    - String não vazia
    - Comprimento múltiplo de 4
    - Apenas caracteres do alfabeto Base64 estrito (A-Z, a-z, 0-9, +, /, com padding =)
    - base64.b64decode(valor.encode('ascii'), validate=True)
    - Re-encoding canônico idêntico ao valor original
    """
    if valor is None or not isinstance(valor, str):
        raise TypeError(f"Hash de senha deve ser string, recebido {type(valor).__name__}.")

    valor_limpo = valor.strip()
    if not valor_limpo:
        raise ValueError("Hash de senha não pode ser vazio.")

    if len(valor_limpo) % 4 != 0:
        raise ValueError(f"Hash de senha inválido: comprimento {len(valor_limpo)} não é múltiplo de 4.")

    if not BASE64_STRICT_PATTERN.match(valor_limpo):
        raise ValueError("Hash de senha contém caracteres inválidos para Base64.")

    try:
        dados_bin = base64.b64decode(valor_limpo.encode('ascii'), validate=True)
    except Exception as e:
        raise ValueError("Hash de senha inválido na decodificação Base64 estrita.") from e

    reencoded = base64.b64encode(dados_bin).decode('ascii')
    if reencoded != valor_limpo:
        raise ValueError("Hash de senha inválido: Base64 não canônico.")

    return valor_limpo


def sanitizar_campo_cobian(
    valor: str,
    max_len: int = MAX_LEN_GENERICO,
    permitir_virgula: bool = False
) -> str:
    """
    Sanitiza uma string para inclusão segura em arquivos .lst Cobian 11.
    Exige tipo str estrito (sem coerção silenciosa).
    Rejeita quebras de linha (CR/LF), caracteres de controle ASCII e estendidos (DEL, NEL, U+2028, U+2029),
    aspas duplas (para evitar escape de blocos) e vírgulas quando proibidas (para evitar injeção CSV).
    """
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise TypeError(f"Campo deve ser string, recebido {type(valor).__name__}.")

    if CONTROL_CHARS_PATTERN.search(valor):
        raise ValueError("Caracteres de controle, quebra de linha ou separadores de parágrafo não permitidos.")

    if '"' in valor:
        raise ValueError('Aspas duplas não permitidas no campo.')

    if not permitir_virgula and ',' in valor:
        raise ValueError('Vírgula não permitida neste campo.')

    if len(valor) > max_len:
        raise ValueError(f"Campo excede o tamanho máximo permitido de {max_len} caracteres.")

    return valor.strip()


def validar_porta_cobian(porta: int | str) -> int:
    """Valida se a porta FTP é um inteiro na faixa válida 1..65535."""
    if isinstance(porta, bool):
        raise TypeError("Porta FTP não pode ser booleano.")
    if isinstance(porta, str):
        porta_str = porta.strip()
        if not porta_str.isdigit():
            raise ValueError(f"Porta FTP inválida: '{porta}'. Deve conter apenas dígitos numéricos.")
        porta_int = int(porta_str)
    elif isinstance(porta, int):
        porta_int = porta
    else:
        raise TypeError(f"Porta FTP deve ser inteiro ou string numérica, recebido {type(porta).__name__}.")

    if not (1 <= porta_int <= 65535):
        raise ValueError(f"Porta FTP fora da faixa válida (1..65535): {porta_int}.")
    return porta_int


def validar_host_cobian(host: str) -> str:
    """Valida se o hostname/IP de destino FTP possui formato válido e sem caracteres de controle."""
    host_limpo = sanitizar_campo_cobian(host, max_len=120, permitir_virgula=False)
    if not host_limpo:
        raise ValueError("Host FTP é obrigatório.")
    if not HOST_PATTERN.match(host_limpo):
        raise ValueError(f"Host FTP contém caracteres inválidos: '{host_limpo}'.")
    return host_limpo


def codificar_horario_cobian(horario_hhmm: str, base_date: datetime.date | None = None) -> str:
    """
    Codifica um horário 'HH:MM' em uma string binária de 64 bits ('0' e '1') correspondente
    ao float IEEE 754 de 64 bits em big-endian do Delphi TDateTime.
    Não utiliza números mágicos artificiais; gera horários exatos ao segundo.
    Permite injetar base_date para reproducibilidade de testes (default: data atual).
    """
    if not isinstance(horario_hhmm, str):
        raise TypeError(f"Horário inválido: esperado string HH:MM, recebido {type(horario_hhmm).__name__}.")

    m = re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', horario_hhmm.strip())
    if not m:
        raise ValueError(f"Horário inválido: '{horario_hhmm}'. Formato esperado: HH:MM.")

    hora = int(m.group(1))
    minuto = int(m.group(2))

    data_base = base_date or datetime.date.today()
    dias = (data_base - DELPHI_EPOCH).days
    fracao_dia = (hora * 3600.0 + minuto * 60.0) / 86400.0
    valor_float = float(dias) + fracao_dia

    packed = struct.pack('>d', valor_float)
    return ''.join(f'{byte:08b}' for byte in packed)


def decodificar_horario_cobian(bin_str: str) -> datetime.datetime:
    """
    Decodifica uma string binária de 64 bits ('0' e '1') em um objeto datetime.datetime
    baseado na época Delphi TDateTime (1899-12-30).
    """
    if not isinstance(bin_str, str) or len(bin_str) != 64 or not set(bin_str).issubset({'0', '1'}):
        raise ValueError("String binária inválida para TDateTime Cobian (esperado 64 bits '0' ou '1').")

    bytes_float = bytes(int(bin_str[i:i+8], 2) for i in range(0, 64, 8))
    valor_float = struct.unpack('>d', bytes_float)[0]

    base_dt = datetime.datetime.combine(DELPHI_EPOCH, datetime.time.min)
    dt = base_dt + datetime.timedelta(days=valor_float)
    return dt


def _formatar_destino_ftp(host: str, porta: int | str, usuario: str, diretorio_inicial: str, hash_senha: str) -> str:
    """
    Formata o bloco de destino remoto FTP (Destination) do Cobian 11 Gravity
    com parâmetros delimitados por aspas quadruplicadas.
    """
    host_validado = validar_host_cobian(host)
    porta_validada = validar_porta_cobian(porta)
    usuario_validado = sanitizar_campo_cobian(usuario, max_len=80, permitir_virgula=False)
    if not usuario_validado:
        raise ValueError("Usuário FTP é obrigatório.")

    diretorio_inicial = sanitizar_campo_cobian(diretorio_inicial or '/', max_len=120, permitir_virgula=False)
    if hash_senha:
        hash_senha = sanitizar_campo_cobian(hash_senha, max_len=300, permitir_virgula=False)
        hash_senha = validar_base64_estrito(hash_senha)
    else:
        hash_senha = ''

    destino = (
        f'"2,""""""FTP Host={host_validado}""""'
        f',""""FTP Port={porta_validada}""""'
        f',""""FTP User name={usuario_validado}""""'
        f',""""FTP Password={hash_senha}""""'
        f',""""FTP Initial directory={diretorio_inicial}""""'
        f',""""FTP Passive=true""""'
        f',""""FTP Passive host=true""""'
        f',""""FTP Proxy=""""""""Proxy type=0"""""""",""""""""Proxy host="""""""",""""""""Proxy password="""""""",""""""""Proxy user name="""""""",""""""""Proxy port=1080"""""""",""""""""Proxy login=false"""""""",""""""""Proxy use IPv6=false""""""""""""'
        f',""""FTP SSL=""""""""Use SSL=0"""""""",""""""""SSL Method=3"""""""",""""""""SSL Authentication=4"""""""",""""""""SSL Data protect=0"""""""",""""""""SSL Mode=3"""""""",""""""""SSL Verify depth=0"""""""",""""""""SSL Path through=false"""""""",""""""""SSL Use CCC=false"""""""",""""""""SSL Verify peer=false"""""""",""""""""SSL Verify fail=false"""""""",""""""""SSL Verify client once=false"""""""",""""""""SSL Certificate file="""""""",""""""""SSL Cipher list="""""""",""""""""SSL DH parameter list="""""""",""""""""SSL Key file="""""""",""""""""SSL Root file="""""""",""""""""SSL Verify dir=""""""""""""'
        f',""""FTP Data port=0""""'
        f',""""FTP Min port=0""""'
        f',""""FTP Max port=0""""'
        f',""""FTP External ip=""""'
        f',""""FTP Use MLIS=true""""'
        f',""""FTP Use HOST=true""""'
        f',""""FTP Use extension data port=false""""'
        f',""""FTP Use compression=false""""'
        f',""""FTP Try NAT fast track=false""""'
        f',""""FTP Force UTF8=false""""'
        f',""""FTP List hidden files=false""""'
        f',""""FTP Listing parameters=-la""""'
        f',""""FTP Connect time out=20000""""'
        f',""""FTP Transfer time out=0""""'
        f',""""FTP Read timeout=60000""""'
        f',""""FTP Listen timeout=10000""""'
        f',""""FTP Keep alive=true""""'
        f',""""FTP Use IPv6=false""""'
        f',""""FTP Keep alive time=120000""""'
        f',""""FTP Keep alive interval=10000""""'
        f',""""FTP Account=""""'
        f',""""FTP Server host=""""'
        f',""""FTP files="""""""'
    )
    return destino


def montar_tarefa_cobian(
    params: dict,
    guid: str | None = None,
    base_date: datetime.date | None = None
) -> str:
    """
    Monta o bloco completo de uma única tarefa Cobian 11, incluindo envelope {11// e //11},
    com formatação estrita de propriedades 'Chave=Valor' (sem espaços, formato real Cobian 11).
    Valida tipos estritos (sem coerção silenciosa) e campos obrigatórios.
    """
    if not isinstance(params, dict):
        raise TypeError("Parâmetros da tarefa devem ser fornecidos como um dicionário.")

    if guid is None:
        raw_guid = str(uuid.uuid4()).upper()
    else:
        if not isinstance(guid, str):
            raise TypeError("GUID deve ser string.")
        raw_guid = guid.strip().strip('{}').upper()
        # Validação do formato GUID
        if not re.match(r'^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$', raw_guid):
            raise ValueError(f"GUID inválido: '{guid}'.")

    # Nome da tarefa (obrigatório)
    nome_raw = params.get('nome')
    if not nome_raw or not isinstance(nome_raw, str) or not nome_raw.strip():
        raise ValueError("Nome da tarefa é obrigatório.")
    nome = sanitizar_campo_cobian(nome_raw, max_len=MAX_LEN_NOME, permitir_virgula=False)

    # Ativo (booleano estrito)
    ativo_val = params.get('ativo', True)
    if type(ativo_val) is not bool:
        raise TypeError(f"Campo 'ativo' deve ser booleano estrito, recebido {type(ativo_val).__name__}.")
    ativo_str = 'true' if ativo_val else 'false'

    # Origem (obrigatória)
    origem_raw = params.get('origem')
    if not origem_raw or not isinstance(origem_raw, str) or not origem_raw.strip():
        raise ValueError("Origem da tarefa é obrigatória.")
    origem = sanitizar_campo_cobian(origem_raw, max_len=MAX_LEN_CAMINHO, permitir_virgula=False)

    # Horário
    horario = params.get('horario', '13:00')
    data_base = base_date or datetime.date.today()
    data_hora_bin = codificar_horario_cobian(horario, base_date=data_base)

    dias_base = (data_base - DELPHI_EPOCH).days
    timer_from_bin = ''.join(f'{b:08b}' for b in struct.pack('>d', float(dias_base)))
    timer_to_bin = ''.join(f'{b:08b}' for b in struct.pack('>d', float(dias_base) + 86399.0 / 86400.0))

    comentario_raw = params.get('comentario', '')
    comentario = sanitizar_campo_cobian(comentario_raw, max_len=MAX_LEN_COMENTARIO, permitir_virgula=True)

    # Pré-eventos com allowlist e tipos estritos
    pre_tipo_raw = params.get('pre_evento_tipo', 'nenhum')
    if not isinstance(pre_tipo_raw, str):
        raise TypeError("pre_evento_tipo deve ser string.")
    pre_tipo = pre_tipo_raw.strip().lower()
    if pre_tipo not in PRE_EVENTOS_PERMITIDOS:
        raise ValueError(f"pre_evento_tipo inválido: '{pre_tipo}'. Permitidos: {PRE_EVENTOS_PERMITIDOS}")

    pre_cam_raw = params.get('pre_evento_caminho', '')
    pre_cam = sanitizar_campo_cobian(pre_cam_raw, max_len=MAX_LEN_CAMINHO, permitir_virgula=False)

    pausa_val = params.get('pre_evento_pausa', 0)
    if isinstance(pausa_val, bool) or not isinstance(pausa_val, int):
        raise TypeError(f"pre_evento_pausa deve ser int, recebido {type(pausa_val).__name__}.")
    if not (0 <= pausa_val <= 86400):
        raise ValueError(f"pre_evento_pausa fora da faixa permitida (0..86400): {pausa_val}.")

    if pre_tipo in ('execute', 'stop_service'):
        if not pre_cam:
            raise ValueError(f"Caminho do comando/serviço é obrigatório quando pre_evento_tipo é '{pre_tipo}'.")
        if pausa_val > 0:
            pre_eventos = f'"{pre_tipo.upper()},{pre_cam},","PAUSE,{pausa_val},"'
        else:
            pre_eventos = f'"{pre_tipo.upper()},{pre_cam},"'
    else:
        pre_eventos = ''

    # Pós-eventos com allowlist e tipos estritos
    pos_tipo_raw = params.get('pos_evento_tipo', 'nenhum')
    if not isinstance(pos_tipo_raw, str):
        raise TypeError("pos_evento_tipo deve ser string.")
    pos_tipo = pos_tipo_raw.strip().lower()
    if pos_tipo not in POS_EVENTOS_PERMITIDOS:
        raise ValueError(f"pos_evento_tipo inválido: '{pos_tipo}'. Permitidos: {POS_EVENTOS_PERMITIDOS}")

    pos_cam_raw = params.get('pos_evento_caminho', '')
    pos_cam = sanitizar_campo_cobian(pos_cam_raw, max_len=MAX_LEN_CAMINHO, permitir_virgula=False)

    if pos_tipo == 'start_service':
        if not pos_cam:
            raise ValueError("Nome do serviço é obrigatório quando pos_evento_tipo é 'start_service'.")
        pos_eventos = f'"START_SERVICE,{pos_cam},"'
    else:
        pos_eventos = ''

    # Destino FTP global
    destino = _formatar_destino_ftp(
        host=params.get('host', ''),
        porta=params.get('porta', '8821'),
        usuario=params.get('usuario', ''),
        diretorio_inicial=params.get('diretorio_inicial', '/'),
        hash_senha=params.get('hash_senha', ''),
    )

    linhas = [
        "{11//",
        f"Id={{{raw_guid}}}",
        f"Name={nome}",
        "Group=IMS",
        f"Enabled={ativo_str}",
        "Include subdirectories=true",
        "Create separated backups=false",
        "Use attributes=true",
        "Use VSC=true",
        "Backup type=0",
        "Priority=0",
        f'Source="0,{origem}"',
        f"Destination={destino}",
        "Schedule type=1",
        "Select days of the week=false",
        "Order of Day of the week=1",
        "Day of the week=1",
        f"Date/Time={data_hora_bin}",
        "Day of the month=1",
        "Month=1",
        "Timer=180",
        f"Timer from={timer_from_bin}",
        f"Timer to={timer_to_bin}",
        "Full copies=0",
        "Differental copies=0",
        "One full every=0",
        "Use fixed day=false",
        "Fixed day=1",
        "Compression=1",
        "Compress individually=false",
        "Split=0",
        "Custom size=4300000000",
        f"Comment={comentario}",
        # Passphrase e Run as Password permanecem intencionalmente vazios (Encryption=0 e Impersonate=false)
        "Encryption=0",
        "Passphrase=",
        "Exclusions=",
        "Inclusions=",
        f"Pre backup events={pre_eventos}",
        f"Post backup events={pos_eventos}",
        "Abort if pre-event fails=false",
        "Abort if post-event fails=false",
        "Mirror=false",
        "Absolute paths=false",
        "Always create top directory=false",
        "Clear archive attribute=true",
        "Include backup type=true",
        "Delete empty directories=false",
        "Impersonate=false",
        "Abort if impersonation fails=false",
        "Run as User name=",
        "Run as Domain=.",
        "Run as Password=",
        "//11}",
    ]
    return "\r\n".join(linhas) + "\r\n"


def gerar_arquivo_cobian_lst(dados_lista: dict, base_date: datetime.date | None = None) -> bytes:
    """
    Gera o conteúdo binário de um arquivo .lst completo para o Cobian Backup 11.
    Codificação UTF-16LE com BOM (\xff\xfe) e quebras CRLF (\r\n).
    Não aceita overrides de destino (host, porta, usuario, etc.) dentro das tarefas.
    """
    if not isinstance(dados_lista, dict):
        raise TypeError("Dados da lista devem ser fornecidos como um dicionário.")

    tarefas = dados_lista.get('tarefas')
    if not tarefas or not isinstance(tarefas, list):
        raise ValueError("A lista deve conter ao menos uma tarefa válida.")

    if len(tarefas) > MAX_TAREFAS_POR_LISTA:
        raise ValueError(f"A lista não pode conter mais de {MAX_TAREFAS_POR_LISTA} tarefas.")

    host_global = dados_lista.get('host', '')
    porta_global = dados_lista.get('porta', '8821')
    usuario_global = dados_lista.get('usuario', '')
    dir_global = dados_lista.get('diretorio_inicial', '/')
    hash_global = dados_lista.get('hash_senha', '')

    # Valida parâmetros de conexão globais
    validar_host_cobian(host_global)
    validar_porta_cobian(porta_global)
    if not usuario_global:
        raise ValueError("Usuário FTP global é obrigatório.")

    CAMPOS_PROIBIDOS_NA_TAREFA = {'host', 'porta', 'usuario', 'diretorio_inicial', 'hash_senha'}

    blocos_tarefas = []
    for t in tarefas:
        if not isinstance(t, dict):
            raise TypeError("Cada tarefa deve ser um dicionário.")

        # Rejeita overrides de destino por tarefa
        chaves_proibidas = set(t.keys()) & CAMPOS_PROIBIDOS_NA_TAREFA
        if chaves_proibidas:
            raise ValueError(f"Parâmetros de destino não são permitidos dentro da tarefa: {chaves_proibidas}. O destino FTP é global.")

        params = dict(t)
        params['host'] = host_global
        params['porta'] = porta_global
        params['usuario'] = usuario_global
        params['diretorio_inicial'] = dir_global
        params['hash_senha'] = hash_global

        bloco = montar_tarefa_cobian(params, base_date=base_date)
        blocos_tarefas.append(bloco)

    texto_total = "".join(blocos_tarefas)
    return b'\xff\xfe' + texto_total.encode('utf-16le')


def obter_preset_fbk(alias: str, caminho_fbk: str | None = None, caminho_exe: str | None = None, pausa: int = 1800) -> list[dict]:
    """Retorna o preset padrão InfoBackup (FBK) com 2 tarefas diárias (13h e 23h)."""
    alias_limpo = sanitizar_campo_cobian(alias or '', max_len=MAX_LEN_NOME, permitir_virgula=False)
    if not alias_limpo:
        raise ValueError("Alias do cliente é obrigatório para o preset FBK.")

    caminho_fbk = caminho_fbk or rf"C:\Infobrasil\infoBackup\backup\{alias_limpo}\backup.fbk"
    caminho_exe = caminho_exe or r"C:\Infobrasil\infoBackup\infoBackup.exe"

    return [
        {
            'nome': f"IMS-{alias_limpo}-13h",
            'ativo': True,
            'origem': caminho_fbk,
            'horario': "13:00",
            'tipo_origem': "fbk",
            'pre_evento_tipo': "execute",
            'pre_evento_caminho': caminho_exe,
            'pre_evento_pausa': pausa,
            'pos_evento_tipo': "nenhum",
            'pos_evento_caminho': "",
            'comentario': alias_limpo,
        },
        {
            'nome': f"IMS-{alias_limpo}-23h",
            'ativo': True,
            'origem': caminho_fbk,
            'horario': "23:00",
            'tipo_origem': "fbk",
            'pre_evento_tipo': "execute",
            'pre_evento_caminho': caminho_exe,
            'pre_evento_pausa': pausa,
            'pos_evento_tipo': "nenhum",
            'pos_evento_caminho': "",
            'comentario': alias_limpo,
        },
    ]


def obter_preset_fdb(alias: str, caminho_fdb: str | None = None, servico: str = 'FirebirdServerDefaultInstance', pausa: int = 60) -> list[dict]:
    """Retorna o preset Banco Direto (FDB) com 1 tarefa diária (23h) com parada e reinício do serviço."""
    alias_limpo = sanitizar_campo_cobian(alias or '', max_len=MAX_LEN_NOME, permitir_virgula=False)
    if not alias_limpo:
        raise ValueError("Alias do cliente é obrigatório para o preset FDB.")

    caminho_fdb = caminho_fdb or r"C:\Infobrasil\Dados\DADOS.FDB"
    servico_limpo = sanitizar_campo_cobian(servico, max_len=MAX_LEN_CAMINHO, permitir_virgula=False)

    return [
        {
            'nome': f"FDB-{alias_limpo}-23h",
            'ativo': True,
            'origem': caminho_fdb,
            'horario': "23:00",
            'tipo_origem': "fdb",
            'pre_evento_tipo': "stop_service",
            'pre_evento_caminho': servico_limpo,
            'pre_evento_pausa': pausa,
            'pos_evento_tipo': "start_service",
            'pos_evento_caminho': servico_limpo,
            'comentario': alias_limpo,
        }
    ]


def extrair_hash_senha_compativel(conteudo_lst: str, host: str, porta: int | str, usuario: str) -> str:
    """
    Extrai o hash da senha de uma lista Cobian pré-existente exigindo correspondência
    estrita da tripla (host, porta, usuário) em TODAS as tarefas com destino FTP.
    Valida e analisa exaustivamente todas as ocorrências de Destination, FTP Host, FTP Port,
    FTP User name e FTP Password em cada tarefa.
    Recusa terminantemente:
    - Tarefas sem destino FTP válido
    - Múltiplas linhas 'Destination =' na mesma tarefa
    - Parâmetros duplicados ou ambíguos na mesma Destination (ex: múltiplos FTP Host=)
    - Divergência de host, porta ou usuário
    - Hashes vazios ou que não sejam Base64 canônico estrito
    - Múltiplos hashes divergentes entre tarefas
    """
    if not conteudo_lst or not isinstance(conteudo_lst, str):
        raise ValueError("Conteúdo da lista de referência inválido ou vazio.")

    host_esperado = validar_host_cobian(host)
    porta_esperada = str(validar_porta_cobian(porta))
    usuario_esperado = sanitizar_campo_cobian(usuario, max_len=80, permitir_virgula=False)

    # Divide o arquivo em tarefas baseadas no envelope {11// ... //11}
    blocos_tarefas = re.findall(r'\{11//\s*(.*?)\s*//11\}', conteudo_lst, flags=re.DOTALL)
    if not blocos_tarefas:
        # Fallback para tarefas delimitadas por { ... }
        blocos_tarefas = [ln for ln in conteudo_lst.split('{') if '}' in ln]

    if not blocos_tarefas:
        raise ValueError("Nenhuma tarefa Cobian encontrada na lista de referência.")

    hashes_encontrados = set()

    for idx, bloco in enumerate(blocos_tarefas, 1):
        # 1. Localiza TODAS as linhas Destination na tarefa (bloqueia linhas Destination duplicadas)
        linhas_dest = re.findall(r'^\s*Destination\s*=\s*(.+)$', bloco, flags=re.MULTILINE)
        if not linhas_dest:
            raise ValueError(f"A Tarefa {idx} da lista de referência não possui destino configurado.")
        if len(linhas_dest) > 1:
            raise ValueError(f"A Tarefa {idx} da lista de referência contém múltiplas linhas de destino (Destination duplicado/ambíguo).")

        dest_linha = linhas_dest[0].strip()

        # 2. Toda tarefa deve possuir destino FTP válido (começa com "2, e contém FTP Host=)
        if not dest_linha.startswith('"2,') or 'FTP Host=' not in dest_linha:
            raise ValueError(f"A Tarefa {idx} da lista de referência não possui destino FTP válido.")

        # 3. Findall de TODOS os parâmetros na linha Destination para detectar repetições/ambiguidades
        all_hosts = re.findall(r'FTP Host=([^",]+)', dest_linha)
        all_ports = re.findall(r'FTP Port=([^",]+)', dest_linha)
        all_users = re.findall(r'FTP User name=([^",]+)', dest_linha)
        all_pwds = re.findall(r'FTP Password=([^",]*)', dest_linha)

        if len(all_hosts) != 1:
            raise ValueError(f"A Tarefa {idx} da lista de referência possui parâmetros 'FTP Host' duplicados ou ambíguos no destino.")
        if len(all_ports) != 1:
            raise ValueError(f"A Tarefa {idx} da lista de referência possui parâmetros 'FTP Port' duplicados ou ambíguos no destino.")
        if len(all_users) != 1:
            raise ValueError(f"A Tarefa {idx} da lista de referência possui parâmetros 'FTP User name' duplicados ou ambíguos no destino.")
        if len(all_pwds) != 1:
            raise ValueError(f"A Tarefa {idx} da lista de referência possui parâmetros 'FTP Password' duplicados ou ambíguos no destino.")

        d_host = all_hosts[0].strip()
        d_port = all_ports[0].strip()
        d_user = all_users[0].strip()
        d_pwd_raw = all_pwds[0].strip()

        if d_host != host_esperado:
            raise ValueError(f"Host divergente na lista de referência (Tarefa {idx}): '{d_host}' != '{host_esperado}'.")

        if d_port != porta_esperada:
            raise ValueError(f"Porta divergente na lista de referência (Tarefa {idx}): '{d_port}' != '{porta_esperada}'.")

        if d_user != usuario_esperado:
            raise ValueError(f"Usuário divergente na lista de referência (Tarefa {idx}): '{d_user}' != '{usuario_esperado}'.")

        if not d_pwd_raw:
            raise ValueError("A versão de referência não possui hash de senha gravado.")

        # Validação estrita de Base64 com decode e verificação canônica
        d_pwd = validar_base64_estrito(d_pwd_raw)
        hashes_encontrados.add(d_pwd)

    if len(hashes_encontrados) > 1:
        raise ValueError("A lista de referência contém múltiplos hashes de senha divergentes entre as tarefas.")

    return list(hashes_encontrados)[0]
