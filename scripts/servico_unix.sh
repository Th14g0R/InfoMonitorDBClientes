#!/usr/bin/env bash
set -Eeuo pipefail

PLATFORM=${1:-}
ACTION=${2:-}
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
SERVICE_NAME=infomonitor
MAC_LABEL=com.infobrasil.infomonitor
BRANCH=main
LOG_DIR="$PROJECT_DIR/logs"
INSTALL_LOCK="/tmp/infomonitor-service-installer.lock"
TRANSACTION_ROLLBACK=
RUNTIME_SWAP_STATE=

fail() { printf 'ERRO: %s\n' "$*" >&2; exit 1; }
note() { printf '\n%s\n' "$*"; }

case "$PLATFORM" in macos|linux) ;; *) fail "plataforma invalida" ;; esac
[ "${EUID:-$(id -u)}" -ne 0 ] || fail "execute como usuario normal; sudo sera solicitado somente ao controlar o servico"
if printf '%s' "$PROJECT_DIR" | LC_ALL=C grep -q '[[:cntrl:]]'; then fail "o caminho do projeto contem caractere de controle"; fi
case "$PROJECT_DIR" in /|/System|/Library|/usr|/etc|/var|/bin|/sbin) fail "diretorio de projeto inseguro: $PROJECT_DIR";; esac
if [ "$PLATFORM" = linux ] && [[ "$PROJECT_DIR" == *%* ]]; then fail "o caminho do projeto nao pode conter % no systemd"; fi
if ! mkdir "$INSTALL_LOCK" 2>/dev/null; then
    if [ -L "$INSTALL_LOCK" ] || [ ! -d "$INSTALL_LOCK" ]; then fail "lock global inseguro ou corrompido: $INSTALL_LOCK"; fi
    fail "outro instalador ja esta em execucao"
fi
cleanup_lock() { rmdir "$INSTALL_LOCK" 2>/dev/null || true; }
trap cleanup_lock EXIT
on_signal() {
    trap - INT TERM
    if [ -n "$TRANSACTION_ROLLBACK" ]; then "$TRANSACTION_ROLLBACK" || true; fi
    cleanup_lock
    exit 130
}
trap on_signal INT TERM

env_value() {
    local key=$1 file=$2 line value
    line=$(awk -v key="$key" 'index($0,key "=")==1 {v=substr($0,length(key)+2)} END{print v}' "$file")
    value=${line%$'\r'}
    case "$value" in \"*\") value=${value#\"}; value=${value%\"};; \'*\') value=${value#\'}; value=${value%\'};; esac
    printf '%s' "$value"
}

python_command() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || fail "Python 3.10 ou superior e obrigatorio"
        command -v python3
    else fail "Python 3 nao encontrado"; fi
}

ensure_runtime() {
    local python lock secret
    python=$(python_command)
    lock="$PROJECT_DIR/requirements.lock"
    [ -f "$lock" ] || fail "requirements.lock obrigatorio e nao encontrado"
    mkdir -p "$LOG_DIR"
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
        secret=$($python -c 'import secrets; print(secrets.token_hex(32))')
        sed "s/^SECRET_KEY=.*/SECRET_KEY=$secret/" "$PROJECT_DIR/.env" > "$PROJECT_DIR/.env.tmp"
        mv "$PROJECT_DIR/.env.tmp" "$PROJECT_DIR/.env"
        printf '.env criado. Revise servidores, e-mail, backup e bind antes de produzir.\n'
    fi
    chmod 600 "$PROJECT_DIR/.env"
    ensure_data_dir
    build_candidate_runtime "$lock"
}

install_service_runtime() {
    local had_service=0 was_active=0
    if [ "$PLATFORM" = macos ]; then [ -f "/Library/LaunchDaemons/$MAC_LABEL.plist" ] && had_service=1
    else systemctl cat "$SERVICE_NAME.service" >/dev/null 2>&1 && had_service=1; fi
    if service_is_active; then was_active=1; fi
    rollback_install() {
        trap - ERR
        stop_service >/dev/null 2>&1 || true
        restore_previous_runtime
        if [ "$was_active" -eq 1 ]; then restart_service >/dev/null 2>&1 || true
        elif [ "$had_service" -eq 0 ]; then uninstall_service >/dev/null 2>&1 || true; fi
        fail "instalacao falhou; runtime anterior restaurado"
    }
    TRANSACTION_ROLLBACK=rollback_install
    trap rollback_install ERR
    if [ "$was_active" -eq 1 ]; then stop_service; fi
    ensure_runtime
    swap_candidate_runtime
    register_service
    health_check
    rm -rf "$PROJECT_DIR/.venv.previous"
    TRANSACTION_ROLLBACK=
    trap - ERR
}

ensure_data_dir() {
    if ! grep -q '^DATA_DIR=' "$PROJECT_DIR/.env"; then printf '\nDATA_DIR=data\n' >> "$PROJECT_DIR/.env"; fi
    local configured data_dir db
    configured=$(env_value DATA_DIR "$PROJECT_DIR/.env"); configured=${configured:-data}
    case "$configured" in /*) data_dir=$configured;; *) data_dir="$PROJECT_DIR/$configured";; esac
    mkdir -p "$data_dir/fotos" "$data_dir/logs"
    for db in sistema.db historico_bancos.db; do
        if [ -f "$PROJECT_DIR/$db" ] && [ ! -e "$data_dir/$db" ]; then cp -p "$PROJECT_DIR/$db" "$data_dir/$db"; fi
    done
    if [ -d "$PROJECT_DIR/static/fotos" ]; then
        find "$PROJECT_DIR/static/fotos" -type f ! -name site.webmanifest ! -name .gitkeep -exec cp -p -n {} "$data_dir/fotos/" \;
    fi
}

resolved_data_dir() {
    local configured candidate
    configured=$(env_value DATA_DIR "$PROJECT_DIR/.env"); configured=${configured:-data}
    case "$configured" in /*) candidate=$configured;; *) candidate="$PROJECT_DIR/$configured";; esac
    [ -d "$candidate" ] || fail "DATA_DIR nao existe: $candidate"
    candidate=$(CDPATH= cd -- "$candidate" && pwd -P)
    if printf '%s' "$candidate" | LC_ALL=C grep -q '[[:cntrl:]]'; then fail "DATA_DIR contem caractere de controle"; fi
    if [ "$PLATFORM" = linux ] && [[ "$candidate" == *%* ]]; then fail "DATA_DIR nao pode conter % no systemd"; fi
    printf '%s' "$candidate"
}

build_candidate_runtime() {
    local lock=$1 wheel_dir=${2:-} source_dir=${3:-$PROJECT_DIR}
    rm -rf "$PROJECT_DIR/.venv.next"
    "$(python_command)" -m venv "$PROJECT_DIR/.venv.next"
    if [ -n "$wheel_dir" ]; then
        "$PROJECT_DIR/.venv.next/bin/python" -m pip install --disable-pip-version-check --no-index --find-links "$wheel_dir" --requirement "$lock"
    else
        "$PROJECT_DIR/.venv.next/bin/python" -m pip install --disable-pip-version-check --requirement "$lock"
    fi
    "$PROJECT_DIR/.venv.next/bin/python" -m pip check
    local smoke_dir
    smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/infomonitor-smoke.XXXXXX")
    if ! (cd "$source_dir" && SECRET_KEY=0123456789abcdef0123456789abcdef SERVIDORES_CONFIG='{}' DATA_DIR="$smoke_dir" "$PROJECT_DIR/.venv.next/bin/python" -c 'import production; production.configuracao_servidor(); import InfoMonitorDBClientes'); then
        rm -rf "$smoke_dir"
        return 1
    fi
    rm -rf "$smoke_dir"
}

swap_candidate_runtime() {
    RUNTIME_SWAP_STATE=none
    if [ -d "$PROJECT_DIR/.venv.previous" ]; then
        if [ -d "$PROJECT_DIR/.venv" ]; then rm -rf "$PROJECT_DIR/.venv.previous"
        else mv "$PROJECT_DIR/.venv.previous" "$PROJECT_DIR/.venv"; fi
    fi
    if [ -d "$PROJECT_DIR/.venv" ]; then
        mv "$PROJECT_DIR/.venv" "$PROJECT_DIR/.venv.previous"
        RUNTIME_SWAP_STATE=previous_moved
    else RUNTIME_SWAP_STATE=fresh_pending; fi
    mv "$PROJECT_DIR/.venv.next" "$PROJECT_DIR/.venv"
    if [ "$RUNTIME_SWAP_STATE" = previous_moved ]; then RUNTIME_SWAP_STATE=complete_with_previous
    else RUNTIME_SWAP_STATE=complete_fresh; fi
}

restore_previous_runtime() {
    case "$RUNTIME_SWAP_STATE" in
        previous_moved) [ ! -d "$PROJECT_DIR/.venv" ] || return 1; mv "$PROJECT_DIR/.venv.previous" "$PROJECT_DIR/.venv";;
        complete_with_previous) rm -rf "$PROJECT_DIR/.venv"; mv "$PROJECT_DIR/.venv.previous" "$PROJECT_DIR/.venv";;
        fresh_pending) rm -rf "$PROJECT_DIR/.venv.next";;
        complete_fresh) rm -rf "$PROJECT_DIR/.venv";;
        none|'') rm -rf "$PROJECT_DIR/.venv.next";;
        *) return 1;;
    esac
    RUNTIME_SWAP_STATE=none
}

backup_code() {
    command -v git >/dev/null 2>&1 || fail "Git nao encontrado"
    git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "a pasta nao e um checkout Git"
    local backup_dir stamp
    backup_dir="$(dirname "$PROJECT_DIR")/InfoMonitorDBClientes-backups"
    stamp=$(date +%Y%m%d-%H%M%S)
    mkdir -p "$backup_dir"
    # git archive contem somente o codigo versionado: nunca .env, bancos, fotos ou logs.
    CODE_BACKUP="$backup_dir/codigo-$stamp.tar.gz"
    git -C "$PROJECT_DIR" archive --format=tar.gz -o "$CODE_BACKUP" HEAD
    printf 'Backup somente do codigo: %s\n' "$CODE_BACKUP"
}

xml_escape() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g; s/'"'"'/\&apos;/g'; }

register_macos() {
    local plist temp user project python stdout stderr
    plist="/Library/LaunchDaemons/$MAC_LABEL.plist"
    temp=$(mktemp "${TMPDIR:-/tmp}/infomonitor-plist.XXXXXX")
    user=$(id -un)
    project=$(xml_escape "$PROJECT_DIR")
    python=$(xml_escape "$PROJECT_DIR/.venv/bin/python")
    stdout=$(xml_escape "$LOG_DIR/servico-saida.log")
    stderr=$(xml_escape "$LOG_DIR/servico-erro.log")
    cat > "$temp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$MAC_LABEL</string>
  <key>UserName</key><string>$(xml_escape "$user")</string>
  <key>WorkingDirectory</key><string>$project</string>
  <key>ProgramArguments</key><array><string>$python</string><string>$project/production.py</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$stdout</string>
  <key>StandardErrorPath</key><string>$stderr</string>
</dict></plist>
EOF
    plutil -lint "$temp"
    note "O macOS pedira permissao apenas para registrar/reiniciar o servico."
    sudo install -o root -g wheel -m 644 "$temp" "$plist"
    rm -f "$temp"
    sudo launchctl bootout system "$plist" >/dev/null 2>&1 || true
    sudo launchctl bootstrap system "$plist"
    sudo launchctl enable "system/$MAC_LABEL"
    sudo launchctl kickstart -k "system/$MAC_LABEL"
}

register_linux() {
    local unit temp user group escaped_project escaped_python data_dir escaped_data
    unit="/etc/systemd/system/$SERVICE_NAME.service"
    temp=$(mktemp "${TMPDIR:-/tmp}/infomonitor-unit.XXXXXX")
    user=$(id -un); group=$(id -gn)
    escaped_project=${PROJECT_DIR//\\/\\\\}; escaped_project=${escaped_project//\"/\\\"}
    escaped_python="$escaped_project/.venv/bin/python"
    data_dir=$(resolved_data_dir)
    escaped_data=${data_dir//\\/\\\\}; escaped_data=${escaped_data//\"/\\\"}
    cat > "$temp" <<EOF
[Unit]
Description=InfoMonitorDBClientes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
Group=$group
WorkingDirectory="$escaped_project"
ExecStart="$escaped_python" "$escaped_project/production.py"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths="$escaped_project"
ReadWritePaths="$escaped_data"

[Install]
WantedBy=multi-user.target
EOF
    note "O Linux pedira permissao apenas para registrar/reiniciar o servico."
    sudo install -o root -g root -m 644 "$temp" "$unit"
    rm -f "$temp"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$SERVICE_NAME.service"
}

register_service() { if [ "$PLATFORM" = macos ]; then register_macos; else register_linux; fi; }

stop_service() {
    if [ "$PLATFORM" = macos ]; then
        if sudo launchctl print "system/$MAC_LABEL" >/dev/null 2>&1; then
            sudo launchctl bootout system "/Library/LaunchDaemons/$MAC_LABEL.plist"
            ! sudo launchctl print "system/$MAC_LABEL" >/dev/null 2>&1 || fail "launchd ainda esta ativo"
        fi
    else
        sudo systemctl stop "$SERVICE_NAME.service"
        ! systemctl is-active --quiet "$SERVICE_NAME.service" || fail "systemd ainda esta ativo"
    fi
}

restart_service() {
    if [ "$PLATFORM" = macos ]; then
        if sudo launchctl print "system/$MAC_LABEL" >/dev/null 2>&1; then
            sudo launchctl kickstart -k "system/$MAC_LABEL"
        else
            [ -f "/Library/LaunchDaemons/$MAC_LABEL.plist" ] || fail "servico launchd nao instalado"
            sudo launchctl bootstrap system "/Library/LaunchDaemons/$MAC_LABEL.plist"
        fi
    else sudo systemctl restart "$SERVICE_NAME.service"; fi
}

status_service() {
    if [ "$PLATFORM" = macos ]; then
        sudo launchctl print "system/$MAC_LABEL"
    else systemctl status "$SERVICE_NAME.service" --no-pager; fi
}

uninstall_service() {
    note "Somente o servico sera removido; configuracao, bancos, fotos e logs permanecerao."
    if [ "$PLATFORM" = macos ]; then
        stop_service
        sudo rm -f "/Library/LaunchDaemons/$MAC_LABEL.plist"
        [ ! -e "/Library/LaunchDaemons/$MAC_LABEL.plist" ] || fail "plist nao foi removido"
    else
        stop_service
        sudo systemctl disable "$SERVICE_NAME.service"
        sudo rm -f "/etc/systemd/system/$SERVICE_NAME.service"
        sudo systemctl daemon-reload
        ! systemctl cat "$SERVICE_NAME.service" >/dev/null 2>&1 || fail "unidade systemd ainda existe"
    fi
}

update_code() {
    command -v git >/dev/null 2>&1 || fail "Git nao encontrado"
    [ -z "$(git -C "$PROJECT_DIR" status --porcelain --untracked-files=no)" ] || fail "ha alteracoes versionadas locais; revise-as antes de atualizar"
    [ -f "$PROJECT_DIR/requirements.lock" ] || fail "requirements.lock obrigatorio e nao encontrado"
    local old_commit wheel_dir was_active=0 old_manifest new_manifest created_manifest
    old_commit=$(git -C "$PROJECT_DIR" rev-parse HEAD)
    git -C "$PROJECT_DIR" fetch --quiet origin "$BRANCH"
    git -C "$PROJECT_DIR" merge-base --is-ancestor "$old_commit" FETCH_HEAD || fail "atualizacao nao e fast-forward"
    wheel_dir=$(mktemp -d "${TMPDIR:-/tmp}/infomonitor-wheels.XXXXXX")
    old_manifest="$wheel_dir/manifest.old"; new_manifest="$wheel_dir/manifest.new"; created_manifest="$wheel_dir/manifest.created"
    git -C "$PROJECT_DIR" ls-tree -r --name-only "$old_commit" | LC_ALL=C sort > "$old_manifest"
    git -C "$PROJECT_DIR" ls-tree -r --name-only FETCH_HEAD | LC_ALL=C sort > "$new_manifest"
    comm -13 "$old_manifest" "$new_manifest" > "$created_manifest"
    while IFS= read -r relative; do
        if [ -n "$relative" ] && { [ -e "$PROJECT_DIR/$relative" ] || [ -L "$PROJECT_DIR/$relative" ]; }; then
            rm -rf "$wheel_dir"
            fail "colisao com caminho local nao gerenciado: $relative"
        fi
    done < "$created_manifest"
    mkdir "$wheel_dir/source"
    git -C "$PROJECT_DIR" archive FETCH_HEAD | tar -x -C "$wheel_dir/source"
    cp "$wheel_dir/source/requirements.lock" "$wheel_dir/requirements.lock"
    "$(python_command)" -m pip download --disable-pip-version-check -r "$wheel_dir/requirements.lock" -d "$wheel_dir"
    build_candidate_runtime "$wheel_dir/requirements.lock" "$wheel_dir" "$wheel_dir/source"
    backup_code
    if service_is_active; then was_active=1; fi
    rollback_update() {
        trap - ERR
        while IFS= read -r relative; do [ -n "$relative" ] && rm -f "$PROJECT_DIR/$relative"; done < "$created_manifest"
        tar -xzf "$CODE_BACKUP" -C "$PROJECT_DIR"
        git -C "$PROJECT_DIR" update-ref HEAD "$old_commit"
        git -C "$PROJECT_DIR" read-tree "$old_commit"
        restore_previous_runtime
        if [ "$was_active" -eq 1 ]; then restart_service >/dev/null 2>&1 || true; fi
        rm -rf "$wheel_dir"
        fail "atualizacao falhou; codigo anterior restaurado"
    }
    TRANSACTION_ROLLBACK=rollback_update
    trap rollback_update ERR
    if [ "$was_active" -eq 1 ]; then stop_service; fi
    ensure_data_dir
    git -C "$PROJECT_DIR" merge --ff-only FETCH_HEAD
    swap_candidate_runtime
    if [ "$was_active" -eq 1 ]; then register_service; health_check; fi
    rm -rf "$wheel_dir"
    rm -rf "$PROJECT_DIR/.venv.previous"
    TRANSACTION_ROLLBACK=
    trap - ERR
}

service_is_active() {
    if [ "$PLATFORM" = macos ]; then sudo launchctl print "system/$MAC_LABEL" >/dev/null 2>&1
    else systemctl is-active --quiet "$SERVICE_NAME.service"; fi
}

health_check() {
    local port host attempt
    host=$(env_value BIND_HOST "$PROJECT_DIR/.env"); host=${host:-127.0.0.1}
    port=$(env_value BIND_PORT "$PROJECT_DIR/.env"); port=${port:-8888}
    case "$host" in 0.0.0.0|::) host=127.0.0.1;; esac
    for attempt in {1..20}; do
        if curl --fail --silent --show-error --max-time 3 "http://$host:$port/healthz" >/dev/null; then return 0; fi
        sleep 1
    done
    printf 'ERRO: servico nao respondeu ao health check em %s:%s\n' "$host" "$port" >&2
    return 1
}

show_logs() {
    mkdir -p "$LOG_DIR"
    if [ "$PLATFORM" = linux ] && command -v journalctl >/dev/null 2>&1; then
        journalctl -u "$SERVICE_NAME.service" -n 100 --no-pager
    else tail -n 100 "$LOG_DIR"/servico-*.log 2>/dev/null || printf 'Ainda nao ha logs.\n'; fi
}

if [ -z "$ACTION" ]; then
    printf '\nInfoMonitorDBClientes (%s)\n1) instalar/configurar servico\n2) atualizar codigo e reiniciar\n3) editar configuracao e reiniciar\n4) status\n5) reiniciar\n6) parar\n7) logs\n8) remover servico\n0) sair\n' "$PLATFORM"
    read -r -p 'Opcao: ' choice
    case "$choice" in 1) ACTION=install;; 2) ACTION=update;; 3) ACTION=configure;; 4) ACTION=status;; 5) ACTION=restart;; 6) ACTION=stop;; 7) ACTION=logs;; 8) ACTION=uninstall;; 0) exit 0;; *) fail "opcao invalida";; esac
fi

case "$ACTION" in
    install) install_service_runtime;;
    update) update_code;;
    configure) [ -f "$PROJECT_DIR/.env" ] || fail ".env ausente; execute instalar primeiro"; "${EDITOR:-vi}" "$PROJECT_DIR/.env"; ensure_data_dir; restart_service; health_check;;
    status) status_service;; restart) restart_service;; stop) stop_service;;
    logs) show_logs;; uninstall) uninstall_service;;
    *) fail "acao invalida: $ACTION";;
esac
