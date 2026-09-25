[CmdletBinding()]
param(
    [ValidateSet('', 'install', 'update', 'apply_prepared', 'configure', 'configure_apply', 'status', 'restart', 'stop', 'uninstall', 'register', 'acl')]
    [string]$Action = '',
    [string]$Target = '',
    [string]$Staging = '',
    [string]$ExpectedCommit = '',
    [string]$ExpectedMetadataHash = '',
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$RepoUrl = 'https://github.com/Th14g0R/InfoMonitorDBClientes.git'
$Branch = 'main'
$ServiceName = 'InfoMonitorDBClientes'
$DefaultDir = 'C:\InfoMonitorDBClientes'
$ScriptPath = $MyInvocation.MyCommand.Path
$installerMutex = $null

function Enter-InstallerMutex([string]$Folder) {
    $created = $false
    $mutex = New-Object Threading.Mutex($true, 'Global\InfoMonitorDBClientes-Installer', [ref]$created)
    if (-not $created) { $mutex.Dispose(); throw 'Outro instalador para este destino ja esta em execucao.' }
    return $mutex
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-DetectedInstallDirectory {
    $parametersPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    try {
        $configured = (Get-ItemProperty -LiteralPath $parametersPath -Name AppDirectory -ErrorAction Stop).AppDirectory
        if ($configured) {
            $full = [IO.Path]::GetFullPath([string]$configured).TrimEnd('\', '/')
            if (Test-Path -LiteralPath (Join-Path $full 'InfoMonitorDBClientes.py') -PathType Leaf) { return $full }
        }
    } catch {}
    return $null
}

function Invoke-Elevated([string]$Operation, [string]$Folder) {
    if ($Elevated) {
        Invoke-ServiceOperation $Operation $Folder
        return
    }
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $ScriptPath),
        '-Action', $Operation, '-Target', ('"{0}"' -f $Folder), '-Elevated')
    $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList $arguments
    if ($process.ExitCode -ne 0) { throw "A operacao administrativa '$Operation' falhou." }
}

function Invoke-ElevatedPrepared([string]$Folder, [string]$Prepared, [string]$Commit, [string]$MetadataHash) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $ScriptPath),
        '-Action', 'apply_prepared', '-Target', ('"{0}"' -f $Folder), '-Staging', ('"{0}"' -f $Prepared),
        '-ExpectedCommit', $Commit, '-ExpectedMetadataHash', $MetadataHash, '-Elevated')
    $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList $arguments
    if ($process.ExitCode -ne 0) { throw 'A aplicacao elevada do pacote preparado falhou.' }
}

function Assert-Administrator {
    if (-not (Test-Administrator)) { throw 'Esta operacao de servico requer permissao de Administrador.' }
}

function Resolve-SafeTarget([string]$Path, [bool]$ForInstall) {
    if (-not $Path) { throw 'Pasta de destino vazia.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\', '/')
    $protected = @($env:windir, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData,
        $env:SystemRoot, (Join-Path $env:SystemRoot 'System32')) | Where-Object { $_ } |
        ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\', '/') }
    if ($full -eq $root) { throw 'A raiz do disco nao e um destino permitido.' }
    foreach ($item in $protected) {
        if ($full.Equals($item, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($item + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Destino protegido pelo sistema: $full"
        }
    }
    $cursor = [IO.Path]::GetPathRoot($full)
    foreach ($segment in $full.Substring($cursor.Length).Split(@('\'), [StringSplitOptions]::RemoveEmptyEntries)) {
        $cursor = Join-Path $cursor $segment
        if (Test-Path -LiteralPath $cursor) {
            $attributes = (Get-Item -LiteralPath $cursor -Force).Attributes
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Destino contem junction/reparse point: $cursor" }
        }
    }
    if ($ForInstall -and (Test-Path $full)) {
        $entries = @(Get-ChildItem -LiteralPath $full -Force)
        $installed = (Test-Path (Join-Path $full '.infomonitor-manifest')) -or
            (Test-Path (Join-Path $full 'InfoMonitorDBClientes.py'))
        if ($entries.Count -gt 0 -and -not $installed) {
            throw 'A pasta nao esta vazia e nao possui manifesto de instalacao.'
        }
    }
    return $full
}

function Resolve-ManifestPath([string]$Folder, [string]$Relative) {
    if (-not $Relative -or [IO.Path]::IsPathRooted($Relative)) { throw "Entrada de manifesto invalida: $Relative" }
    $normalized = $Relative.Replace('/', '\')
    if ($normalized.Split('\') -contains '..') { throw "Travessia no manifesto: $Relative" }
    $root = [IO.Path]::GetFullPath($Folder).TrimEnd('\') + '\'
    $candidate = [IO.Path]::GetFullPath((Join-Path $Folder $normalized))
    if (-not $candidate.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "Entrada fora do destino: $Relative" }
    return $candidate
}

function Get-DataDirectory([string]$Folder) {
    $configured = 'data'
    $envPath = Join-Path $Folder '.env'
    if (Test-Path $envPath) {
        $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match '^DATA_DIR=' } | Select-Object -Last 1
        if ($line) { $configured = (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'") }
    }
    $candidate = if ([IO.Path]::IsPathRooted($configured)) { $configured } else { Join-Path $Folder $configured }
    $resolved = Resolve-SafeTarget $candidate $false
    if ($resolved.Equals([IO.Path]::GetFullPath($Folder).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'DATA_DIR nao pode ser a raiz do codigo no Windows.'
    }
    return $resolved
}

function Find-Nssm([string]$Folder) {
    $path = Join-Path $env:ProgramFiles 'nssm\win64\nssm.exe'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $owner = (Get-Acl -LiteralPath $path).Owner
    if ($owner -notmatch '(?i)(Administrators|Administradores|SYSTEM|TrustedInstaller)$') {
        throw "nssm.exe nao pertence a uma identidade administrativa confiavel: $owner"
    }
    $expected = $env:NSSM_SHA256
    if (-not $expected -or $expected -notmatch '^[0-9a-fA-F]{64}$') { throw 'Defina NSSM_SHA256 com o hash SHA-256 oficial esperado.' }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if (-not $actual.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) { throw 'O SHA-256 do nssm.exe nao corresponde a NSSM_SHA256.' }
    return $path
}

function Invoke-Nssm([string]$Nssm, [string[]]$Arguments) {
    & $Nssm @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "NSSM falhou: $($Arguments -join ' ')" }
}

function Invoke-Icacls([string[]]$Arguments) {
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Falha ao aplicar ACL: $($Arguments -join ' ')" }
}

function Set-AppAcl([string]$Folder) {
    Assert-Administrator
    $installer = "${env:USERDOMAIN}\${env:USERNAME}"
    Invoke-Icacls @($Folder, '/inheritance:r', '/grant:r', "$installer`:(OI)(CI)M", '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-19:(OI)(CI)RX')
    $data = Get-DataDirectory $Folder
    if (Test-Path $data) { Invoke-Icacls @($data, '/grant:r', '*S-1-5-19:(OI)(CI)M') }
    foreach ($relative in @('.env', 'known_hosts', '.infomonitor-manifest', '.infomonitor-version')) {
        $sensitive = Join-Path $Folder $relative
        if (Test-Path $sensitive) {
            Invoke-Icacls @($sensitive, '/inheritance:r', '/grant:r', "$installer`:M", '*S-1-5-32-544:F', '*S-1-5-18:F', '*S-1-5-19:R')
        }
    }
    $manifest = Join-Path $Folder '.infomonitor-manifest'
    if (Test-Path $manifest) {
        foreach ($relative in Get-Content -LiteralPath $manifest) {
            $codeFile = Resolve-ManifestPath $Folder $relative
            if (Test-Path $codeFile -PathType Leaf) {
                Invoke-Icacls @($codeFile, '/inheritance:r', '/grant:r', "$installer`:M", '*S-1-5-32-544:F', '*S-1-5-18:F', '*S-1-5-19:R')
            }
        }
    }
}

function Register-Service([string]$Folder) {
    Assert-Administrator
    $nssm = Find-Nssm $Folder
    if (-not $nssm) { throw 'NSSM nao encontrado. Instale-o pelo site oficial e coloque nssm.exe no PATH ou na pasta do projeto.' }
    $python = Join-Path $Folder '.venv\Scripts\python.exe'
    $runner = Join-Path $Folder 'production.py'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $runner)) {
        throw 'Ambiente virtual ou production.py nao encontrado.'
    }
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) { Invoke-Nssm $nssm @('install', $ServiceName, $python, $runner) }
    Invoke-Nssm $nssm @('set', $ServiceName, 'Application', $python)
    $runnerArgument = '"' + $runner + '"'
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppParameters', $runnerArgument)
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppDirectory', $Folder)
    Invoke-Nssm $nssm @('set', $ServiceName, 'ObjectName', 'NT AUTHORITY\LocalService')
    Invoke-Nssm $nssm @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START')
    $dataDir = Get-DataDirectory $Folder
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppStdout', (Join-Path $dataDir 'logs\servico-saida.log'))
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppStderr', (Join-Path $dataDir 'logs\servico-erro.log'))
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppRotateFiles', '1')
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppRotateBytes', '10485760')
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppExit', 'Default', 'Restart')
    Invoke-Nssm $nssm @('set', $ServiceName, 'AppEnvironmentExtra', 'PYTHONDONTWRITEBYTECODE=1')
    Set-AppAcl $Folder
    if ((Get-Service -Name $ServiceName).Status -eq 'Running') { Restart-Service $ServiceName -Force }
    else { Start-Service $ServiceName }
    $running = Get-Service -Name $ServiceName
    $running.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
    $running.Refresh()
    if ($running.Status -ne 'Running') { throw 'O servico nao atingiu o estado Running.' }
}

function Invoke-ServiceOperation([string]$Operation, [string]$Folder) {
    Assert-Administrator
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    switch ($Operation) {
        'register' { Register-Service $Folder }
        'acl' { Set-AppAcl $Folder }
        'restart' {
            if (-not $service) { throw 'Servico nao instalado.' }
            Restart-Service $ServiceName -Force
            $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
        }
        'stop' {
            if ($service -and $service.Status -ne 'Stopped') {
                Stop-Service $ServiceName -Force
                $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(20))
            }
        }
        'uninstall' {
            if ($service -and $service.Status -ne 'Stopped') {
                Stop-Service $ServiceName -Force
                $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(20))
                $service.Refresh()
                if ($service.Status -ne 'Stopped') { throw 'Servico nao parou; remocao abortada.' }
            }
            $nssm = Find-Nssm $Folder
            if ($service -and $nssm) { Invoke-Nssm $nssm @('remove', $ServiceName, 'confirm') }
            elseif ($service) {
                & sc.exe delete $ServiceName | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'sc.exe nao conseguiu remover o servico.' }
            }
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { break }
                Start-Sleep -Milliseconds 500
            }
            if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { throw 'Servico ainda existe apos a remocao.' }
        }
    }
}

function Ensure-Environment([string]$Folder) {
    $envPath = Join-Path $Folder '.env'
    if (Test-Path -LiteralPath $envPath) {
        if (-not (Select-String -LiteralPath $envPath -Pattern '^DATA_DIR=' -Quiet)) { Add-Content -LiteralPath $envPath -Value "`nDATA_DIR=data" }
        Initialize-DataDirectory $Folder
        return
    }
    $example = Join-Path $Folder '.env.example'
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $secret = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    $content = (Get-Content -LiteralPath $example -Raw) -replace '(?m)^SECRET_KEY=.*$', "SECRET_KEY=$secret"
    [IO.File]::WriteAllText($envPath, $content, (New-Object Text.UTF8Encoding($false)))
    Write-Host '.env criado com chave aleatoria; revise-o antes de produzir.' -ForegroundColor Yellow
    Initialize-DataDirectory $Folder
}

function Initialize-DataDirectory([string]$Folder) {
    $data = Get-DataDirectory $Folder
    New-Item -ItemType Directory -Force $data, (Join-Path $data 'fotos'), (Join-Path $data 'logs') | Out-Null
    foreach ($name in @('sistema.db', 'historico_bancos.db')) {
        $old = Join-Path $Folder $name; $new = Join-Path $data $name
        if ((Test-Path $old -PathType Leaf) -and -not (Test-Path $new)) { Copy-Item -LiteralPath $old -Destination $new }
    }
    $oldPhotos = Join-Path $Folder 'static\fotos'
    if (Test-Path $oldPhotos) {
        Get-ChildItem -LiteralPath $oldPhotos -File | Where-Object { $_.Name -notin @('site.webmanifest', '.gitkeep') } | ForEach-Object {
            $destination = Join-Path (Join-Path $data 'fotos') $_.Name
            if (-not (Test-Path $destination)) { Copy-Item -LiteralPath $_.FullName -Destination $destination }
        }
    }
}

function Ensure-Runtime([string]$Folder, [string]$WheelDir = '') {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Python 3 nao encontrado.' }
    & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 ou superior e obrigatorio.' }
    $venvPython = Join-Path $Folder '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { & $pythonCommand.Source -m venv (Join-Path $Folder '.venv') }
    $lock = Join-Path $Folder 'requirements.lock'
    if (-not (Test-Path -LiteralPath $lock)) { throw 'requirements.lock obrigatorio e nao encontrado.' }
    if ($WheelDir) { & $venvPython -m pip install --disable-pip-version-check --no-index --find-links $WheelDir -r $lock }
    else { & $venvPython -m pip install --disable-pip-version-check -r $lock }
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependencias no ambiente virtual.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'As dependencias instaladas sao inconsistentes.' }
    New-Item -ItemType Directory -Force (Join-Path $Folder 'logs') | Out-Null
}

function Build-CandidateRuntime([string]$Source, [string]$Folder, [string]$WheelDir) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    $next = Join-Path $Folder '.venv.next'
    if (Test-Path $next) { Remove-Item -LiteralPath $next -Recurse -Force }
    & $pythonCommand.Source -m venv $next
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar ambiente candidato.' }
    $candidatePython = Join-Path $next 'Scripts\python.exe'
    & $candidatePython -m pip install --disable-pip-version-check --no-index --find-links $WheelDir -r (Join-Path $Source 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar ambiente candidato.' }
    & $candidatePython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Ambiente candidato inconsistente.' }
    Push-Location $Source
    try { & $candidatePython -c "import os,sys; os.environ.update(SECRET_KEY='0123456789abcdef0123456789abcdef', SERVIDORES_CONFIG='{}', DATA_DIR=sys.argv[1]); import production; production.configuracao_servidor(); import InfoMonitorDBClientes" (Join-Path $Source 'smoke-data') }
    finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw 'Smoke import do ambiente candidato falhou.' }
}

function Swap-CandidateRuntime([string]$Folder) {
    $current = Join-Path $Folder '.venv'; $next = Join-Path $Folder '.venv.next'; $previous = Join-Path $Folder '.venv.previous'
    $script:RuntimeSwapState = 'None'
    if (Test-Path $previous) {
        if (Test-Path $current) { Remove-Item -LiteralPath $previous -Recurse -Force }
        else { Move-Item -LiteralPath $previous -Destination $current }
    }
    if (Test-Path $current) {
        Move-Item -LiteralPath $current -Destination $previous
        $script:RuntimeSwapState = 'PreviousMoved'
    } else { $script:RuntimeSwapState = 'FreshPending' }
    Move-Item -LiteralPath $next -Destination $current
    if ($script:RuntimeSwapState -eq 'PreviousMoved') { $script:RuntimeSwapState = 'CompleteWithPrevious' }
    else { $script:RuntimeSwapState = 'CompleteFresh' }
}

function Restore-PreviousRuntime([string]$Folder) {
    $current = Join-Path $Folder '.venv'; $next = Join-Path $Folder '.venv.next'; $previous = Join-Path $Folder '.venv.previous'
    switch ($script:RuntimeSwapState) {
        'PreviousMoved' { if (Test-Path $current) { throw 'Estado parcial de runtime inconsistente.' }; Move-Item $previous $current }
        'CompleteWithPrevious' { if (Test-Path $current) { Remove-Item $current -Recurse -Force }; Move-Item $previous $current }
        'FreshPending' { Remove-Item $next -Recurse -Force -ErrorAction SilentlyContinue }
        'CompleteFresh' { Remove-Item $current -Recurse -Force -ErrorAction SilentlyContinue }
        'None' { Remove-Item $next -Recurse -Force -ErrorAction SilentlyContinue }
        default { throw 'Estado de troca de runtime desconhecido.' }
    }
    $script:RuntimeSwapState = 'None'
}

function Copy-TrackedFiles([string]$Source, [string]$Folder) {
    New-Item -ItemType Directory -Force $Folder | Out-Null
    $tracked = & git -C $Source ls-files
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao listar arquivos versionados.' }
    foreach ($relative in $tracked) {
        if (Test-ProtectedRelative $relative) { continue }
        $destination = Resolve-ManifestPath $Folder $relative
        New-Item -ItemType Directory -Force (Split-Path $destination -Parent) | Out-Null
        Copy-Item -LiteralPath (Join-Path $Source $relative) -Destination $destination -Force
    }
}

function Test-ProtectedRelative([string]$Relative) {
    $normalized = $Relative.Replace('\', '/').TrimStart('/')
    $first = ($normalized -split '/')[0]
    $leaf = [IO.Path]::GetFileName($normalized)
    $extension = [IO.Path]::GetExtension($normalized)
    return $first -in @('.venv', '.venv.next', '.venv.previous', 'data', 'logs', 'backups') -or
        ($normalized.StartsWith('static/fotos/') -and $leaf -notin @('site.webmanifest', '.gitkeep')) -or
        $leaf -in @('.env', 'known_hosts', 'nssm.exe', '.infomonitor-version', '.infomonitor-manifest') -or
        $extension -in @('.db', '.sqlite', '.sqlite3', '.fdb', '.fbk', '.pem', '.key', '.pfx', '.log')
}

function Read-InstalledFingerprint([string]$Folder) {
    $appPath = Join-Path $Folder 'InfoMonitorDBClientes.py'
    $manifestPath = Join-Path $Folder '.infomonitor-manifest'
    $versionPath = Join-Path $Folder '.infomonitor-version'
    $manifestExists = Test-Path -LiteralPath $manifestPath -PathType Leaf
    $versionExists = Test-Path -LiteralPath $versionPath -PathType Leaf
    return [pscustomobject]@{
        fresh=(-not (Test-Path -LiteralPath $appPath -PathType Leaf))
        manifestExists=$manifestExists
        manifestSha256=$(if ($manifestExists) { (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash } else { $null })
        versionExists=$versionExists
        version=$(if ($versionExists) { Get-Content -LiteralPath $versionPath -Raw } else { $null })
    }
}

function Test-InstalledFingerprintEqual($Left, $Right) {
    return ([bool]$Left.fresh -eq [bool]$Right.fresh) -and
        ([bool]$Left.manifestExists -eq [bool]$Right.manifestExists) -and
        ([bool]$Left.versionExists -eq [bool]$Right.versionExists) -and
        ((-not $Left.manifestExists) -or ([string]$Left.manifestSha256).Equals(
            [string]$Right.manifestSha256, [StringComparison]::OrdinalIgnoreCase)) -and
        ((-not $Left.versionExists) -or ([string]$Left.version -ceq [string]$Right.version))
}

function Capture-InstalledBase([string]$Source, [string]$Folder) {
    $before = Read-InstalledFingerprint $Folder
    $ownedManifest = @(Get-InstalledManifest $Source $Folder)
    $after = Read-InstalledFingerprint $Folder
    if (-not (Test-InstalledFingerprintEqual $before $after)) {
        throw 'installation changed during preparation; retry'
    }
    return [pscustomobject]@{
        fresh=$before.fresh
        manifestExists=$before.manifestExists
        manifestSha256=$before.manifestSha256
        versionExists=$before.versionExists
        version=$before.version
        ownedManifest=@($ownedManifest)
    }
}

function Get-InstalledManifest([string]$Source, [string]$Folder) {
    $manifest = Join-Path $Folder '.infomonitor-manifest'
    if (Test-Path $manifest) {
        $entries = @(Get-Content -LiteralPath $manifest | Where-Object { $_ -and -not (Test-ProtectedRelative $_) })
        foreach ($entry in $entries) { Resolve-ManifestPath $Folder $entry | Out-Null }
        return $entries
    }
    if (-not (Test-Path (Join-Path $Folder 'InfoMonitorDBClientes.py') -PathType Leaf)) { return @() }
    if (Test-Path (Join-Path $Folder '.git') -PathType Container) {
        $tracked = @(& git -C $Folder ls-files)
        if ($LASTEXITCODE -ne 0 -or $tracked.Count -eq 0) {
            throw 'O checkout Git local nao pode ser lido para preparar a atualizacao.'
        }
        Write-Host 'Checkout Git local detectado; os arquivos atuais serao atualizados e registrados no manifesto da instalacao.' -ForegroundColor Yellow
        return @($tracked | Where-Object {
            -not (Test-ProtectedRelative $_) -and (Test-Path (Resolve-ManifestPath $Folder $_) -PathType Leaf)
        })
    }
    $versionFile = Join-Path $Folder '.infomonitor-version'
    if (-not (Test-Path $versionFile -PathType Leaf)) {
        throw 'Instalacao legada sem manifesto/versao. Faca backup manual e migre com uma instalacao limpa.'
    }
    $oldSha = (Get-Content -LiteralPath $versionFile -Raw).Trim()
    if ($oldSha -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Marcador de versao legado invalido. Faca backup manual e migre com uma instalacao limpa.'
    }
    & git -C $Source cat-file -e "$oldSha^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        & git -C $Source fetch --quiet --depth 1 origin $oldSha
        if ($LASTEXITCODE -ne 0) { throw 'Commit da versao instalada nao foi encontrado; faca backup manual e migracao limpa.' }
        & git -C $Source cat-file -e "$oldSha^{commit}" 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Commit legado baixado nao e valido; atualizacao abortada.' }
    }
    $tracked = & git -C $Source ls-tree -r --name-only $oldSha
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao ler manifesto do commit legado.' }
    return @($tracked | Where-Object {
        -not (Test-ProtectedRelative $_) -and (Test-Path (Resolve-ManifestPath $Folder $_) -PathType Leaf)
    })
}

function Backup-Code([string]$Source, [string]$Folder, [string[]]$OldManifest) {
    if (-not (Test-Path -LiteralPath (Join-Path $Folder 'InfoMonitorDBClientes.py'))) { return }
    $backupDir = Join-Path (Split-Path $Folder -Parent) 'InfoMonitorDBClientes-backups'
    New-Item -ItemType Directory -Force $backupDir | Out-Null
    $stage = Join-Path ([IO.Path]::GetTempPath()) ("infomonitor-code-{0}" -f [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $stage | Out-Null
    try {
        foreach ($relative in $OldManifest) {
            $installedFile = Resolve-ManifestPath $Folder $relative
            if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf)) { continue }
            if (Test-ProtectedRelative $relative) { continue }
            $backupFile = Join-Path $stage $relative
            New-Item -ItemType Directory -Force (Split-Path $backupFile -Parent) | Out-Null
            Copy-Item -LiteralPath $installedFile -Destination $backupFile -Force
        }
        [IO.File]::WriteAllLines((Join-Path $stage 'MANIFESTO-INSTALADO.txt'), $OldManifest, [Text.Encoding]::UTF8)
        $zip = Join-Path $backupDir ("codigo-{0}.zip" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip
        Write-Host "Backup somente do codigo: $zip"
        return $zip
    } finally { Remove-Item -LiteralPath $stage -Recurse -Force }
}

function Apply-Release([string]$Source, [string]$Folder, [string[]]$OldManifest) {
    $newManifest = @(& git -C $Source ls-files | Where-Object { -not (Test-ProtectedRelative $_) })
    if ($LASTEXITCODE -ne 0 -or $newManifest.Count -eq 0) { throw 'Manifesto novo invalido.' }
    Copy-TrackedFiles $Source $Folder
    foreach ($relative in $OldManifest) {
        if ($relative -notin $newManifest -and -not (Test-ProtectedRelative $relative)) {
            $obsolete = Resolve-ManifestPath $Folder $relative
            if (Test-Path $obsolete -PathType Leaf) { Remove-Item -LiteralPath $obsolete -Force }
        }
    }
    $manifest = Join-Path $Folder '.infomonitor-manifest'
    $temporary = "$manifest.tmp"
    [IO.File]::WriteAllLines($temporary, $newManifest, [Text.Encoding]::UTF8)
    Move-Item -LiteralPath $temporary -Destination $manifest -Force
    return $newManifest
}

function Test-HttpHealth([string]$Folder) {
    $values = @{}
    Get-Content (Join-Path $Folder '.env') | ForEach-Object {
        if ($_ -match '^([A-Z_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
    }
    $hostName = if ($values.BIND_HOST) { $values.BIND_HOST } else { '127.0.0.1' }
    if ($hostName -in @('0.0.0.0', '::')) { $hostName = '127.0.0.1' }
    $port = if ($values.BIND_PORT) { [int]$values.BIND_PORT } else { 8888 }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri "http://${hostName}:$port/healthz" -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { return }
        } catch { Start-Sleep -Seconds 1 }
    }
    throw "Health check falhou em http://${hostName}:$port/"
}

function Apply-ConfiguredEnvironment([string]$Folder) {
    Get-DataDirectory $Folder | Out-Null
    Initialize-DataDirectory $Folder
    Invoke-ServiceOperation 'acl' $Folder
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Invoke-ServiceOperation 'restart' $Folder
        Test-HttpHealth $Folder
    }
}

function Install-Or-Update([string]$Folder) {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) { throw 'Git for Windows nao encontrado.' }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ("infomonitor-{0}" -f [Guid]::NewGuid().ToString('N'))
    $wheels = Join-Path ([IO.Path]::GetTempPath()) ("infomonitor-wheels-{0}" -f [Guid]::NewGuid().ToString('N'))
    $wasRunning = $false
    $stopped = $false
    $backup = $null
    $oldManifest = @()
    $candidateManifest = @()
    $createdPaths = @()
    $script:RuntimeSwapState = 'None'
    $mutated = $false
    $freshInstall = $false
    $oldVersion = $null
    $oldManifestExisted = $false
    try {
        & git clone --quiet --depth 1 --branch $Branch $RepoUrl $temp
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao baixar o repositorio.' }
        if (-not (Test-Path (Join-Path $temp 'requirements.lock'))) { throw 'Download sem requirements.lock.' }
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Python 3 nao encontrado.' }
        & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 ou superior e obrigatorio.' }
        New-Item -ItemType Directory $wheels | Out-Null
        & $pythonCommand.Source -m pip download --disable-pip-version-check -r (Join-Path $temp 'requirements.lock') -d $wheels
        if ($LASTEXITCODE -ne 0) { throw 'Falha no preflight das dependencias.' }
        New-Item -ItemType Directory -Force $Folder | Out-Null
        Build-CandidateRuntime $temp $Folder $wheels
        $commit = (& git -C $temp rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Commit baixado invalido.' }
        $oldManifest = @(Get-InstalledManifest $temp $Folder)
        $oldManifestExisted = Test-Path (Join-Path $Folder '.infomonitor-manifest')
        if (Test-Path (Join-Path $Folder '.infomonitor-version')) { $oldVersion = Get-Content (Join-Path $Folder '.infomonitor-version') -Raw }
        $candidateManifest = @(& git -C $temp ls-files | Where-Object { -not (Test-ProtectedRelative $_) })
        if ($LASTEXITCODE -ne 0 -or $candidateManifest.Count -eq 0) { throw 'Manifesto candidato invalido.' }
        $createdPaths = @($candidateManifest | Where-Object { $_ -notin $oldManifest })
        foreach ($relative in $createdPaths) {
            $collision = Resolve-ManifestPath $Folder $relative
            if (Test-Path -LiteralPath $collision) { throw "Colisao com caminho local nao gerenciado: $relative" }
        }
        $backup = Backup-Code $temp $Folder $oldManifest
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        $freshInstall = -not $service
        $wasRunning = $service -and $service.Status -eq 'Running'
        if ($wasRunning) { Invoke-Elevated 'stop' $Folder; $stopped = $true }
        $mutated = $true
        $newManifest = @(Apply-Release $temp $Folder $oldManifest)
        Ensure-Environment $Folder
        Swap-CandidateRuntime $Folder
        [IO.File]::WriteAllText((Join-Path $Folder '.infomonitor-version'), $commit, [Text.Encoding]::ASCII)
        if ($freshInstall -or $wasRunning) {
            Invoke-Elevated 'register' $Folder
            Test-HttpHealth $Folder
        } else { Invoke-Elevated 'acl' $Folder }
        $stopped = $false
        Remove-Item -LiteralPath (Join-Path $Folder '.venv.previous') -Recurse -Force -ErrorAction SilentlyContinue
    } catch {
        $failure = $_
        if ($mutated) {
            try {
                if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { Invoke-Elevated 'stop' $Folder }
                foreach ($relative in $createdPaths) {
                    if (-not (Test-ProtectedRelative $relative)) {
                        $added = Resolve-ManifestPath $Folder $relative
                        if (Test-Path $added -PathType Leaf) { Remove-Item -LiteralPath $added -Force }
                    }
                }
                if ($backup -and (Test-Path $backup)) { Expand-Archive -LiteralPath $backup -DestinationPath $Folder -Force }
                Remove-Item -LiteralPath (Join-Path $Folder 'MANIFESTO-INSTALADO.txt') -Force -ErrorAction SilentlyContinue
                if ($oldManifestExisted) { [IO.File]::WriteAllLines((Join-Path $Folder '.infomonitor-manifest'), $oldManifest, [Text.Encoding]::UTF8) }
                else { Remove-Item (Join-Path $Folder '.infomonitor-manifest') -Force -ErrorAction SilentlyContinue }
                if ($null -ne $oldVersion) { [IO.File]::WriteAllText((Join-Path $Folder '.infomonitor-version'), $oldVersion, [Text.Encoding]::ASCII) }
                else { Remove-Item (Join-Path $Folder '.infomonitor-version') -Force -ErrorAction SilentlyContinue }
                Restore-PreviousRuntime $Folder
                if ($wasRunning) { Invoke-Elevated 'register' $Folder }
                elseif ($freshInstall) { Invoke-Elevated 'uninstall' $Folder }
            } catch { Write-Host "Rollback incompleto: $($_.Exception.Message)" -ForegroundColor Red }
        }
        throw $failure
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
        if (Test-Path -LiteralPath $wheels) { Remove-Item -LiteralPath $wheels -Recurse -Force }
        if (Test-Path -LiteralPath (Join-Path $Folder '.venv.next')) { Remove-Item -LiteralPath (Join-Path $Folder '.venv.next') -Recurse -Force }
    }
}

function Assert-RequestedMode([string]$Folder, [string]$Mode) {
    $installed = Test-Path -LiteralPath (Join-Path $Folder 'InfoMonitorDBClientes.py') -PathType Leaf
    if ($Mode -eq 'install' -and $installed) {
        throw 'Uma instalacao existente foi encontrada neste destino. Escolha a opcao Atualizar.'
    }
    if ($Mode -eq 'update' -and -not $installed) {
        throw 'Nenhuma instalacao existente foi encontrada neste destino. Escolha a opcao Instalar.'
    }
}

function Prepare-And-Apply([string]$Folder, [ValidateSet('install', 'update')][string]$Mode) {
    Assert-RequestedMode $Folder $Mode
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $git -or -not $pythonCommand) { throw 'Git for Windows e Python 3 sao obrigatorios.' }
    if ($Mode -eq 'update') {
        Write-Host "Atualizacao selecionada para: $Folder" -ForegroundColor Cyan
        Write-Host 'Baixando o main, comparando arquivos e validando dependencias antes de alterar a instalacao.'
    } else {
        Write-Host "Nova instalacao selecionada para: $Folder" -ForegroundColor Cyan
    }
    $prepared = Join-Path ([IO.Path]::GetTempPath()) ("infomonitor-prepared-{0}" -f [Guid]::NewGuid().ToString('N'))
    $source = Join-Path $prepared 'source'; $wheels = Join-Path $prepared 'wheels'
    try {
        New-Item -ItemType Directory -Force $prepared, $wheels | Out-Null
        & git clone --quiet --depth 1 --branch $Branch $RepoUrl $source
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao baixar o repositorio.' }
        $commit = (& git -C $source rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Commit preparado invalido.' }
        & $pythonCommand.Source -m pip download --disable-pip-version-check -r (Join-Path $source 'requirements.lock') -d $wheels
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao baixar dependencias.' }
        Build-CandidateRuntime $source $prepared $wheels
        $runtimeZip = Join-Path $prepared 'runtime.zip'
        Compress-Archive -Path (Join-Path $prepared '.venv.next\*') -DestinationPath $runtimeZip
        Remove-Item (Join-Path $prepared '.venv.next') -Recurse -Force
        $installedBase = Capture-InstalledBase $source $Folder
        $oldManifest = @($installedBase.ownedManifest)
        $candidateManifest = @(& git -C $source ls-files | Where-Object { -not (Test-ProtectedRelative $_) })
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao preparar manifesto candidato.' }
        $createdPaths = @($candidateManifest | Where-Object { $_ -notin $oldManifest })
        foreach ($relative in $createdPaths) {
            if (Test-Path -LiteralPath (Resolve-ManifestPath $Folder $relative)) { throw "Colisao com caminho local nao gerenciado: $relative" }
        }
        $hashes = @()
        foreach ($relative in $candidateManifest) {
            $hashes += [pscustomobject]@{ path=$relative; sha256=(Get-FileHash (Join-Path $source $relative) -Algorithm SHA256).Hash }
        }
        $metadata = [pscustomobject]@{
            commit=$commit; mode=$Mode; oldManifest=$oldManifest; candidateManifest=$candidateManifest; createdPaths=$createdPaths
            targetWasFresh=$installedBase.fresh
            oldManifestExisted=$installedBase.manifestExists
            oldManifestSha256=$installedBase.manifestSha256
            oldVersionExisted=$installedBase.versionExists
            oldVersion=$installedBase.version
            runtimeSha256=(Get-FileHash $runtimeZip -Algorithm SHA256).Hash; hashes=$hashes
        }
        $metadataPath = Join-Path $prepared 'metadata.json'
        $metadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
        $metadataHash = (Get-FileHash $metadataPath -Algorithm SHA256).Hash
        Invoke-ElevatedPrepared $Folder $prepared $commit $metadataHash
    } finally {
        if (Test-Path $prepared) { Remove-Item $prepared -Recurse -Force }
    }
}

function Apply-PreparedRelease([string]$Folder, [string]$Prepared, [string]$Commit) {
    $preparedFull = Resolve-SafeTarget $Prepared $false
    if (-not (Test-Path $preparedFull -PathType Container) -or
        ((Get-Item $preparedFull -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Staging preparado invalido.' }
    $metadataPath = Join-Path $preparedFull 'metadata.json'
    $actualMetadataHash = (Get-FileHash $metadataPath -Algorithm SHA256).Hash
    if ($ExpectedMetadataHash -notmatch '^[0-9a-fA-F]{64}$' -or
        -not $actualMetadataHash.Equals($ExpectedMetadataHash, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Hash dos metadados preparados divergente.'
    }
    $metadata = Get-Content $metadataPath -Raw | ConvertFrom-Json
    if ($Commit -notmatch '^[0-9a-f]{40}$' -or $metadata.commit -ne $Commit) { throw 'Commit preparado divergente.' }
    if ($metadata.mode -notin @('install', 'update')) { throw 'Modo preparado invalido.' }
    if ($metadata.mode -eq 'install' -and -not [bool]$metadata.targetWasFresh) { throw 'Instalacao preparada sobre destino existente.' }
    if ($metadata.mode -eq 'update' -and [bool]$metadata.targetWasFresh) { throw 'Atualizacao preparada sem instalacao existente.' }
    $source = Join-Path $preparedFull 'source'; $runtimeZip = Join-Path $preparedFull 'runtime.zip'
    if ((Get-FileHash $runtimeZip -Algorithm SHA256).Hash -ne $metadata.runtimeSha256) { throw 'Hash do runtime preparado divergente.' }
    foreach ($item in $metadata.hashes) {
        $file = Resolve-ManifestPath $source ([string]$item.path)
        if ((Get-FileHash $file -Algorithm SHA256).Hash -ne $item.sha256) { throw "Hash divergente: $($item.path)" }
    }
    $currentFresh = -not (Test-Path (Join-Path $Folder 'InfoMonitorDBClientes.py') -PathType Leaf)
    $currentManifestPath = Join-Path $Folder '.infomonitor-manifest'
    $currentVersionPath = Join-Path $Folder '.infomonitor-version'
    $currentManifestExists = Test-Path $currentManifestPath -PathType Leaf
    $currentVersionExists = Test-Path $currentVersionPath -PathType Leaf
    $baseChanged = ([bool]$metadata.targetWasFresh -ne $currentFresh) -or
        ([bool]$metadata.oldManifestExisted -ne $currentManifestExists) -or
        ([bool]$metadata.oldVersionExisted -ne $currentVersionExists)
    if (-not $baseChanged -and $currentManifestExists) {
        $currentManifestHash = (Get-FileHash $currentManifestPath -Algorithm SHA256).Hash
        $baseChanged = -not $currentManifestHash.Equals(
            [string]$metadata.oldManifestSha256, [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $baseChanged -and $currentVersionExists) {
        $baseChanged = (Get-Content $currentVersionPath -Raw) -cne [string]$metadata.oldVersion
    }
    if ($baseChanged) { throw 'installation changed; prepare again' }
    $oldManifest = @($metadata.oldManifest); $candidateManifest = @($metadata.candidateManifest); $createdPaths = @($metadata.createdPaths)
    foreach ($relative in $createdPaths) {
        if (Test-Path -LiteralPath (Resolve-ManifestPath $Folder $relative)) { throw "Colisao elevada com caminho nao gerenciado: $relative" }
    }
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    $freshInstall = -not $service; $wasRunning = $service -and $service.Status -eq 'Running'
    $backup = Backup-Code $source $Folder $oldManifest
    $script:RuntimeSwapState = 'None'; $mutated = $false
    try {
        if ($wasRunning) { Invoke-ServiceOperation 'stop' $Folder }
        $mutated = $true
        foreach ($relative in $candidateManifest) {
            $destination = Resolve-ManifestPath $Folder $relative
            New-Item -ItemType Directory -Force (Split-Path $destination -Parent) | Out-Null
            Copy-Item -LiteralPath (Resolve-ManifestPath $source $relative) -Destination $destination -Force
        }
        foreach ($relative in $oldManifest) {
            if ($relative -notin $candidateManifest -and -not (Test-ProtectedRelative $relative)) {
                Remove-Item (Resolve-ManifestPath $Folder $relative) -Force -ErrorAction SilentlyContinue
            }
        }
        [IO.File]::WriteAllLines((Join-Path $Folder '.infomonitor-manifest'), $candidateManifest, [Text.Encoding]::UTF8)
        Ensure-Environment $Folder
        $next = Join-Path $Folder '.venv.next'; if (Test-Path $next) { Remove-Item $next -Recurse -Force }
        New-Item -ItemType Directory $next | Out-Null
        Expand-Archive $runtimeZip -DestinationPath $next
        Swap-CandidateRuntime $Folder
        [IO.File]::WriteAllText((Join-Path $Folder '.infomonitor-version'), $Commit, [Text.Encoding]::ASCII)
        if ($freshInstall -or $wasRunning) { Invoke-ServiceOperation 'register' $Folder; Test-HttpHealth $Folder }
        else { Invoke-ServiceOperation 'acl' $Folder }
        Remove-Item (Join-Path $Folder '.venv.previous') -Recurse -Force -ErrorAction SilentlyContinue
    } catch {
        if ($mutated) {
            if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { Invoke-ServiceOperation 'stop' $Folder }
            foreach ($relative in $createdPaths) { Remove-Item (Resolve-ManifestPath $Folder $relative) -Force -ErrorAction SilentlyContinue }
            if ($backup -and (Test-Path $backup)) { Expand-Archive $backup -DestinationPath $Folder -Force }
            Remove-Item (Join-Path $Folder 'MANIFESTO-INSTALADO.txt') -Force -ErrorAction SilentlyContinue
            if ($metadata.oldManifestExisted) { [IO.File]::WriteAllLines((Join-Path $Folder '.infomonitor-manifest'), $oldManifest, [Text.Encoding]::UTF8) }
            else { Remove-Item (Join-Path $Folder '.infomonitor-manifest') -Force -ErrorAction SilentlyContinue }
            if ($null -ne $metadata.oldVersion) { [IO.File]::WriteAllText((Join-Path $Folder '.infomonitor-version'), [string]$metadata.oldVersion, [Text.Encoding]::ASCII) }
            else { Remove-Item (Join-Path $Folder '.infomonitor-version') -Force -ErrorAction SilentlyContinue }
            Restore-PreviousRuntime $Folder
            if ($wasRunning) { Invoke-ServiceOperation 'register' $Folder } elseif ($freshInstall) { Invoke-ServiceOperation 'uninstall' $Folder }
        }
        throw
    }
}

if ($Elevated) {
    if ($Action -notin @('apply_prepared', 'configure_apply', 'register', 'restart', 'stop', 'uninstall', 'acl')) {
        throw 'Operacao elevada invalida.'
    }
    $elevatedTarget = Resolve-SafeTarget $Target ($Action -eq 'apply_prepared')
    $installerMutex = Enter-InstallerMutex $elevatedTarget
    try {
        if ($Action -eq 'apply_prepared') { Apply-PreparedRelease $elevatedTarget $Staging $ExpectedCommit }
        elseif ($Action -eq 'configure_apply') { Apply-ConfiguredEnvironment $elevatedTarget }
        else { Invoke-ServiceOperation $Action $elevatedTarget }
    } finally {
        try { $installerMutex.ReleaseMutex() } catch {}
        $installerMutex.Dispose()
    }
    exit 0
}

try {
    if (-not $Target) {
        $local = Split-Path (Split-Path $ScriptPath -Parent) -Parent
        $detected = Get-DetectedInstallDirectory
        $localProject = Test-Path -LiteralPath (Join-Path $local 'InfoMonitorDBClientes.py') -PathType Leaf
        $suggestion = if ($detected) { $detected } elseif ($localProject) { $local } else { $DefaultDir }
        Write-Host "`nDiretorio do instalador: $local" -ForegroundColor Cyan
        if ($detected) { Write-Host "Instalacao detectada no servico: $detected" -ForegroundColor Green }
        elseif ($localProject) { Write-Host 'Projeto local detectado; ele pode ser atualizado no proprio local.' -ForegroundColor Green }
        else { Write-Host "Nenhuma instalacao existente foi detectada; sugestao: $DefaultDir" -ForegroundColor Yellow }
        $entered = Read-Host "Pasta que sera instalada/atualizada (Enter usa: $suggestion)"
        $Target = if ($entered) { $entered } else { $suggestion }
    }
    $Target = Resolve-SafeTarget $Target $false
    Write-Host "Destino selecionado: $Target" -ForegroundColor Cyan
    $targetHasApp = Test-Path -LiteralPath (Join-Path $Target 'InfoMonitorDBClientes.py') -PathType Leaf
    Write-Host $(if ($targetHasApp) { 'Estado do destino: instalacao existente (use Atualizar).' } else { 'Estado do destino: pasta nova (use Instalar).' }) -ForegroundColor Yellow
    if (-not $Action) {
        Write-Host "`n[1] Instalar novo      [2] Atualizar existente  [3] Configurar .env"
        Write-Host '[4] Status             [5] Reiniciar            [6] Parar'
        Write-Host '[7] Remover servico    [0] Sair'
        $choice = Read-Host 'Opcao'
        $Action = @{'1'='install';'2'='update';'3'='configure';'4'='status';'5'='restart';'6'='stop';'7'='uninstall';'0'=''}[$choice]
        if ($null -eq $Action) { throw 'Opcao invalida.' }
        if (-not $Action) { exit 0 }
    }
    $Target = Resolve-SafeTarget $Target ($Action -eq 'install')
    switch ($Action) {
        'install' { Prepare-And-Apply $Target 'install' }
        'update' { Prepare-And-Apply $Target 'update' }
        'configure' {
            Ensure-Environment $Target
            Start-Process notepad.exe -ArgumentList @((Join-Path $Target '.env')) -Wait
            Invoke-Elevated 'configure_apply' $Target
        }
        'status' {
            $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($service) { $service | Format-List Name, Status, StartType }
            else { Write-Host 'Servico nao instalado.' }
            Write-Host "Logs: $(Join-Path (Get-DataDirectory $Target) 'logs')"
        }
        'restart' { Invoke-Elevated 'restart' $Target }
        'stop' { Invoke-Elevated 'stop' $Target }
        'uninstall' {
            Invoke-Elevated 'uninstall' $Target
            Write-Host 'Servico removido. .env, bancos, known_hosts, fotos e logs foram preservados.'
        }
    }
} catch {
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($installerMutex) {
        try { $installerMutex.ReleaseMutex() } catch {}
        $installerMutex.Dispose()
    }
}
