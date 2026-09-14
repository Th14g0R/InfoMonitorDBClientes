[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repoUrl = 'https://github.com/Th14g0R/InfoMonitorDBClientes.git'
$branch = 'main'
$defaultDir = 'C:\Tomcat 9.0\webapps\InfoMonitorDBClientes'
$defaultService = 'InfoMonitorDBClientes'
$entryPoint = 'InfoMonitorDBClientes.py'
$serviceToRecover = $null

function Write-Title([string]$Text) {
    Write-Host "`n============================================================================" -ForegroundColor Cyan
    Write-Host " $Text" -ForegroundColor Cyan
    Write-Host "============================================================================" -ForegroundColor Cyan
}

function Read-YesNo([string]$Question, [bool]$DefaultYes = $true) {
    $suffix = if ($DefaultYes) { '[S/n]' } else { '[s/N]' }
    $answer = (Read-Host "$Question $suffix").Trim()
    if (-not $answer) { return $DefaultYes }
    return $answer -match '^(s|sim|y|yes)$'
}

function Select-Folder([string]$Initial, [bool]$AllowNew) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Selecione a pasta do InfoMonitorDBClientes'
    $dialog.ShowNewFolderButton = $AllowNew
    if (Test-Path -LiteralPath $Initial -PathType Container) { $dialog.SelectedPath = $Initial }
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dialog.SelectedPath }
    return $null
}

function Get-SafeTarget([string]$Path) {
    if (-not $Path) { throw 'Nenhuma pasta foi selecionada.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\', '/')
    if ($full -eq $root -or $full -match '^(?i:C:\Windows|C:\Program Files(?: \(x86\))?)$') {
        throw "Pasta de destino insegura: $full"
    }
    return $full
}

function Get-AppService([string]$Target) {
    $escaped = [Regex]::Escape($Target)
    return Get-CimInstance Win32_Service -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $defaultService -or $_.DisplayName -eq $defaultService -or $_.PathName -match $escaped } |
        Select-Object -First 1
}

function Get-LocalVersion([string]$Target) {
    $marker = Join-Path $Target '.infomonitor-version'
    if (Test-Path -LiteralPath $marker -PathType Leaf) {
        return (Get-Content -LiteralPath $marker -Raw).Trim()
    }
    if (Test-Path -LiteralPath (Join-Path $Target '.git') -PathType Container) {
        $value = & git -C $Target rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0) { return ($value | Select-Object -First 1).Trim() }
    }
    return $null
}

function Get-RemoteVersion {
    $line = & git ls-remote $repoUrl "refs/heads/$branch" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $line) { throw 'Nao foi possivel consultar a versao no GitHub.' }
    return (($line | Select-Object -First 1) -split '\s+')[0]
}

function New-Backup([string]$Target) {
    $backupDir = Join-Path (Split-Path -Parent $Target) 'InfoMonitorDBClientes-backups'
    [IO.Directory]::CreateDirectory($backupDir) | Out-Null
    $zipPath = Join-Path $backupDir ("InfoMonitorDBClientes-{0}.zip" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
    $excluded = @('.git', '.venv', '__pycache__', '.pytest_cache', '.test-security-browser')
    try {
        Get-ChildItem -LiteralPath $Target -File -Recurse -Force | ForEach-Object {
            $relative = $_.FullName.Substring($Target.Length).TrimStart('\', '/')
            $first = ($relative -split '[\\/]')[0]
            if ($excluded -notcontains $first -and $_.Extension -notin @('.pyc', '.log')) {
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $zip, $_.FullName, $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
            }
        }
    } finally { $zip.Dispose() }
    if (-not (Test-Path -LiteralPath $zipPath) -or (Get-Item -LiteralPath $zipPath).Length -eq 0) {
        throw 'O backup de seguranca nao foi criado.'
    }
    return $zipPath
}

function Get-Nssm([string]$Target, $Service) {
    $candidates = @(
        (Join-Path $Target 'nssm.exe'),
        (Join-Path $PSScriptRoot 'nssm.exe'),
        (Join-Path (Split-Path -Parent $PSScriptRoot) 'nssm.exe')
    )
    if ($Service -and ($Service.PathName -match '(?i)^\s*"([^"]*nssm\.exe)"' -or
        $Service.PathName -match '(?i)^\s*(.*?nssm\.exe)\s')) {
        $candidates = @($Matches[1]) + $candidates
    }
    $command = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    return $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}

function Copy-RepositoryFiles([string]$Source, [string]$Target) {
    [IO.Directory]::CreateDirectory($Target) | Out-Null
    $tracked = (& git -C $Source ls-files) | Where-Object { $_ }
    if ($LASTEXITCODE -ne 0 -or -not $tracked) { throw 'Nao foi possivel obter a lista segura de arquivos do Git.' }
    foreach ($relative in $tracked) {
        $extension = [IO.Path]::GetExtension($relative)
        $leaf = [IO.Path]::GetFileName($relative)
        if ($leaf -in @('.env', 'known_hosts', 'nssm.exe') -or
            $extension -in @('.db', '.sqlite', '.sqlite3', '.fdb', '.fbk', '.pem', '.key', '.p12', '.pfx')) { continue }
        $sourceFile = Join-Path $Source $relative
        $targetFile = Join-Path $Target $relative
        [IO.Directory]::CreateDirectory((Split-Path -Parent $targetFile)) | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $targetFile -Force
    }
}

function Ensure-Environment([string]$Target) {
    $envPath = Join-Path $Target '.env'
    if (Test-Path -LiteralPath $envPath) { return }
    $example = Join-Path $Target '.env.example'
    if (-not (Test-Path -LiteralPath $example)) { throw '.env.example nao foi encontrado.' }
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $secret = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    $content = (Get-Content -LiteralPath $example -Raw) -replace '(?m)^SECRET_KEY=.*$', "SECRET_KEY=$secret"
    [IO.File]::WriteAllText($envPath, $content, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Arquivo .env criado com SECRET_KEY aleatoria: $envPath" -ForegroundColor Yellow
    Write-Host 'Revise as configuracoes antes de usar o sistema em producao.' -ForegroundColor Yellow
    Start-Process notepad.exe -ArgumentList @($envPath) -Wait
}

function Ensure-Venv([string]$Target, [string]$Python) {
    $venvPython = Join-Path $Target '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { & $Python -m venv (Join-Path $Target '.venv') }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) { throw 'Falha ao criar o ambiente virtual Python.' }
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $Target 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar as dependencias Python.' }
    return $venvPython
}

function Configure-Service([string]$Target, [string]$Python, $CurrentService) {
    $nssm = Get-Nssm $Target $CurrentService
    if (-not $nssm) {
        Write-Host 'NSSM nao encontrado. Os arquivos foram instalados, mas o servico nao foi criado.' -ForegroundColor Yellow
        Write-Host 'Coloque nssm.exe ao lado do BAT ou na pasta instalada e execute novamente.' -ForegroundColor Yellow
        if ($CurrentService) { Start-Service -Name $CurrentService.Name }
        return $null
    }
    $serviceName = if ($CurrentService) { $CurrentService.Name } else { $defaultService }
    if (-not $CurrentService) {
        & $nssm install $serviceName $Python (Join-Path $Target $entryPoint)
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao registrar o servico com NSSM.' }
    }
    & $nssm set $serviceName Application $Python | Out-Null
    & $nssm set $serviceName AppParameters (Join-Path $Target $entryPoint) | Out-Null
    & $nssm set $serviceName AppDirectory $Target | Out-Null
    & $nssm set $serviceName Start SERVICE_AUTO_START | Out-Null
    & $nssm set $serviceName AppStdout (Join-Path $Target 'servico-saida.log') | Out-Null
    & $nssm set $serviceName AppStderr (Join-Path $Target 'servico-erro.log') | Out-Null
    Start-Service -Name $serviceName
    return Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
}

try {
    Write-Title 'InfoMonitorDBClientes - instalacao, verificacao e atualizacao'
    Write-Host '[1] Verificar instalacao e oferecer atualizacao'
    Write-Host '[2] Instalar em uma nova pasta'
    Write-Host '[3] Apenas verificar versao e servico'
    Write-Host '[0] Sair'
    $choice = (Read-Host 'Escolha uma opcao [1]').Trim()
    if (-not $choice) { $choice = '1' }
    if ($choice -eq '0') { exit 0 }
    if ($choice -notin @('1', '2', '3')) { throw 'Opcao invalida.' }

    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) { throw 'Git nao encontrado. Instale o Git for Windows e execute novamente.' }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Python nao encontrado. Instale Python 3 de 64 bits e execute novamente.' }
    & $pythonCommand.Source --version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'O comando python.exe existe, mas nao aponta para uma instalacao valida do Python.' }

    $scriptProjectDir = Split-Path -Parent $PSScriptRoot
    $detectedDir = $null
    if (Test-Path -LiteralPath (Join-Path $defaultDir $entryPoint) -PathType Leaf) {
        $detectedDir = $defaultDir
    } elseif ((Test-Path -LiteralPath (Join-Path $scriptProjectDir $entryPoint) -PathType Leaf) -and
              ((Test-Path -LiteralPath (Join-Path $scriptProjectDir '.env') -PathType Leaf) -or
               (Test-Path -LiteralPath (Join-Path $scriptProjectDir '.infomonitor-version') -PathType Leaf))) {
        $detectedDir = $scriptProjectDir
    }

    if ($choice -ne '2' -and $detectedDir) {
        Write-Host "`nInstalacao encontrada em: $detectedDir" -ForegroundColor Green
        Write-Host '[ENTER/S] Confirmar  [G] Selecionar outra pasta  [M] Digitar outro caminho'
        $folderChoice = (Read-Host 'Deseja verificar esta instalacao? [S]').Trim()
        if (-not $folderChoice -or $folderChoice -match '^(?i:s|sim)$') { $target = $detectedDir }
        elseif ($folderChoice -match '^(?i:g)$') { $target = Select-Folder $detectedDir $false }
        elseif ($folderChoice -match '^(?i:m)$') { $target = Read-Host 'Caminho completo da instalacao' }
        else { throw 'Opcao de pasta invalida.' }
    } else {
        if ($choice -ne '2') {
            Write-Host "`nNenhuma instalacao foi encontrada no caminho sugerido." -ForegroundColor Yellow
            Write-Host 'Selecione onde deseja instalar o sistema.' -ForegroundColor Yellow
        }
        Write-Host "`nPasta sugerida: $defaultDir"
        Write-Host '[ENTER] Usar a pasta sugerida  [G] Selecionar graficamente  [M] Digitar caminho'
        $folderChoice = (Read-Host 'Opcao').Trim()
        $target = $defaultDir
        if ($folderChoice -match '^(?i:g)$') { $target = Select-Folder $defaultDir $true }
        elseif ($folderChoice -match '^(?i:m)$') { $target = Read-Host 'Caminho completo para instalacao' }
        elseif ($folderChoice) { throw 'Opcao de pasta invalida.' }
    }
    $target = Get-SafeTarget $target
    $installed = Test-Path -LiteralPath (Join-Path $target $entryPoint) -PathType Leaf
    $service = Get-AppService $target
    $localVersion = if ($installed) { Get-LocalVersion $target } else { $null }
    $remoteVersion = Get-RemoteVersion

    Write-Title 'Resultado da verificacao'
    Write-Host "Pasta: $target"
    Write-Host ("Instalacao: " + $(if ($installed) { 'encontrada' } else { 'nao encontrada' }))
    Write-Host ("Versao local: " + $(if ($localVersion) { $localVersion.Substring(0, [Math]::Min(12, $localVersion.Length)) } else { 'nao identificada (instalacao anterior)' }))
    Write-Host "Versao GitHub: $($remoteVersion.Substring(0, 12))"
    Write-Host ("Servico: " + $(if ($service) { "$($service.Name) - $($service.State)" } else { 'nao encontrado' }))

    if ($choice -eq '3') { exit 0 }
    if ($installed -and $localVersion -eq $remoteVersion) {
        if (-not (Read-YesNo 'A versao local ja e a mais atual. Deseja reinstalar os arquivos?' $false)) { exit 0 }
    } elseif ($installed) {
        if (-not (Read-YesNo 'Deseja atualizar a versao local para a versao do GitHub?' $true)) {
            Write-Host 'Versao local mantida sem alteracoes.' -ForegroundColor Green
            exit 0
        }
    } elseif (-not (Read-YesNo "Deseja instalar em '$target'?" $true)) { exit 0 }

    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("InfoMonitorDBClientes-{0}" -f [Guid]::NewGuid().ToString('N'))
    try {
        Write-Title 'Preparando arquivos'
        & git clone --quiet --depth 1 --branch $branch $repoUrl $tempRoot
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao baixar o repositorio do GitHub.' }
        $downloadedVersion = (& git -C $tempRoot rev-parse HEAD).Trim()
        if ($downloadedVersion -ne $remoteVersion) { throw 'A versao baixada nao corresponde a versao consultada.' }

        if ($service -and $service.State -ne 'Stopped') {
            $serviceToRecover = $service.Name
            Stop-Service -Name $service.Name -Force
            $service = Get-AppService $target
        }
        $backup = $null
        if ($installed) {
            $backup = New-Backup $target
            Write-Host "Backup criado: $backup" -ForegroundColor Green
        }
        Copy-RepositoryFiles $tempRoot $target
        Ensure-Environment $target
        $venvPython = Ensure-Venv $target $pythonCommand.Source
        [IO.File]::WriteAllText((Join-Path $target '.infomonitor-version'), $downloadedVersion, [Text.Encoding]::ASCII)

        $configuredService = Configure-Service $target $venvPython $service
        if ($configuredService) {
            $serviceToRecover = $null
            Start-Sleep -Seconds 2
            $configuredService = Get-AppService $target
            Write-Host "Servico $($configuredService.Name): $($configuredService.State)" -ForegroundColor Green
        }

        $port = 8888
        $portLine = Get-Content -LiteralPath (Join-Path $target '.env') | Where-Object { $_ -match '^BIND_PORT=\d+$' } | Select-Object -Last 1
        if ($portLine) { $port = [int](($portLine -split '=', 2)[1]) }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
            Write-Host "Site respondendo em http://127.0.0.1:$port/ (HTTP $($response.StatusCode))." -ForegroundColor Green
        } catch {
            Write-Host "O site ainda nao respondeu na porta $port. Consulte servico-erro.log e o .env." -ForegroundColor Yellow
        }
        Write-Title 'Operacao concluida'
        Write-Host "Versao instalada: $($downloadedVersion.Substring(0, 12))"
        Write-Host "Pasta: $target"
        if ($backup) { Write-Host "Backup anterior: $backup" }
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
    }
} catch {
    if ($serviceToRecover) {
        try { Start-Service -Name $serviceToRecover -ErrorAction Stop } catch {
            Write-Host "Nao foi possivel reiniciar o servico $serviceToRecover automaticamente." -ForegroundColor Red
        }
    }
    Write-Host "`nERRO: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
