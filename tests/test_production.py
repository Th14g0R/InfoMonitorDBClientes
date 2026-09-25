import os
import unittest
from pathlib import Path
from unittest.mock import patch

import production


ROOT = Path(__file__).resolve().parents[1]


class ProductionConfigTests(unittest.TestCase):
    def test_configuracao_valida(self):
        with patch.dict(os.environ, {
            'BIND_HOST': '127.0.0.1', 'BIND_PORT': '9123', 'WEB_THREADS': '6'
        }, clear=False):
            self.assertEqual(production.configuracao_servidor(), {
                'host': '127.0.0.1', 'port': 9123, 'threads': 6
            })

    def test_rejeita_porta_threads_e_host_invalidos(self):
        casos = (
            {'BIND_HOST': '127.0.0.1', 'BIND_PORT': '0', 'WEB_THREADS': '4'},
            {'BIND_HOST': '127.0.0.1', 'BIND_PORT': '8888', 'WEB_THREADS': '65'},
            {'BIND_HOST': 'http://localhost', 'BIND_PORT': '8888', 'WEB_THREADS': '4'},
        )
        for valores in casos:
            with self.subTest(valores=valores), patch.dict(os.environ, valores, clear=False):
                with self.assertRaises(SystemExit):
                    production.configuracao_servidor()

    def test_instaladores_usam_runner_lock_e_diretorio_absoluto(self):
        unix = (ROOT / 'scripts' / 'servico_unix.sh').read_text(encoding='utf-8')
        windows = (ROOT / 'scripts' / 'instalar_atualizar.ps1').read_text(encoding='utf-8')
        bat = (ROOT / 'INSTALAR_OU_ATUALIZAR.bat').read_text(encoding='utf-8')
        shell = (ROOT / 'INSTALAR_OU_ATUALIZAR.sh').read_text(encoding='utf-8')
        command = (ROOT / 'INSTALAR_OU_ATUALIZAR.command').read_text(encoding='utf-8')
        self.assertIn('requirements.lock', unix)
        self.assertNotIn('lock="$PROJECT_DIR/requirements.txt"', unix)
        self.assertIn('WorkingDirectory', unix)
        self.assertIn('/production.py', unix)
        self.assertIn('NoNewPrivileges=true', unix)
        self.assertIn('health_check', unix)
        self.assertIn('trap rollback_update ERR', unix)
        self.assertIn('.venv.next', unix)
        self.assertIn('.venv.previous', unix)
        self.assertIn('infomonitor-service-installer.lock', unix)
        self.assertIn('INSTALL_LOCK="/tmp/infomonitor-service-installer.lock"', unix)
        self.assertNotRegex(unix, r'INSTALL_LOCK=.*TMPDIR')
        self.assertIn('lock global inseguro ou corrompido', unix)
        self.assertIn('/healthz', unix)
        self.assertIn('env_value()', unix)
        self.assertNotIn('from dotenv import dotenv_values', unix)
        self.assertIn('infomonitor-smoke.XXXXXX', unix)
        self.assertIn('execute como usuario normal', unix)
        self.assertIn('RUNTIME_SWAP_STATE=previous_moved', unix)
        self.assertIn('set -Eeuo pipefail', unix)
        self.assertNotIn('reset --hard', unix)
        self.assertIn('read-tree "$old_commit"', unix)
        self.assertIn('ReadWritePaths="$escaped_data"', unix)
        self.assertIn('manifest.created', unix)
        self.assertIn('colisao com caminho local nao gerenciado', unix)
        self.assertIn('Diretorio de instalacao/atualizacao: %s', unix)
        self.assertIn('Opcao para $PROJECT_DIR', unix)
        self.assertIn('Diretorio do instalador:', bat)
        self.assertIn('Diretorio do instalador: %s', shell)
        self.assertIn('Diretorio do instalador: %s', command)
        self.assertIn('requirements.lock', windows)
        self.assertIn('AppDirectory', windows)
        self.assertIn('production.py', windows)
        self.assertNotIn('& $Python -m pip install', windows)
        self.assertIn("'NT AUTHORITY\\LocalService'", windows)
        self.assertIn('.infomonitor-manifest', windows)
        self.assertIn('Test-HttpHealth', windows)
        self.assertIn('.venv.next', windows)
        self.assertIn('.venv.previous', windows)
        self.assertIn('Threading.Mutex', windows)
        self.assertIn('Global\\InfoMonitorDBClientes-', windows)
        self.assertIn('Resolve-ManifestPath', windows)
        self.assertIn('Get-DataDirectory', windows)
        self.assertIn("Servico ainda existe apos a remocao", windows)
        self.assertIn("$script:RuntimeSwapState = 'PreviousMoved'", windows)
        self.assertIn('function Apply-ConfiguredEnvironment', windows)
        self.assertIn('Initialize-DataDirectory $Folder', windows)
        self.assertIn("Invoke-ServiceOperation 'acl' $Folder", windows)
        self.assertIn('$createdPaths', windows)
        self.assertIn('Colisao com caminho local nao gerenciado', windows)
        self.assertIn('function Get-DetectedInstallDirectory', windows)
        self.assertIn('Instalacao detectada no servico:', windows)
        self.assertIn('Pasta que sera instalada/atualizada', windows)
        self.assertNotIn('checkout de desenvolvimento nao pode ser usado', windows)
        self.assertNotIn("Join-Path $Folder 'nssm.exe'", windows)
        manifest_start = windows.index('function Get-InstalledManifest')
        manifest_end = windows.index('function Backup-Code', manifest_start)
        legacy_manifest = windows[manifest_start:manifest_end]
        self.assertIn("'.infomonitor-version'", legacy_manifest)
        self.assertIn("Join-Path $Folder '.git'", legacy_manifest)
        self.assertIn('& git -C $Folder ls-files', legacy_manifest)
        self.assertIn('ls-tree -r --name-only $oldSha', legacy_manifest)
        version_marker = legacy_manifest.index("$versionFile = Join-Path $Folder '.infomonitor-version'")
        self.assertNotIn('ls-files', legacy_manifest[version_marker:])
        self.assertIn('backup manual e migre com uma instalacao limpa', legacy_manifest)
        elevated_start = windows.rindex('if ($Elevated)')
        elevated_end = windows.index('\ntry {\n    if (-not $Target)', elevated_start)
        self.assertIn('Enter-InstallerMutex $elevatedTarget', windows[elevated_start:elevated_end])
        self.assertIn('$installerMutex.ReleaseMutex()', windows)
        self.assertNotIn('$script:installerMutex', windows)
        self.assertEqual(windows.count('Enter-InstallerMutex'), 2)
        self.assertIn("'install' { Prepare-And-Apply $Target }", windows)
        self.assertIn("Invoke-Elevated 'configure_apply' $Target", windows)
        self.assertIn('Build-CandidateRuntime $source $prepared $wheels', windows)
        self.assertIn('Invoke-ElevatedPrepared $Folder $prepared $commit $metadataHash', windows)
        self.assertIn('ExpectedMetadataHash', windows)
        self.assertIn('targetWasFresh', windows)
        self.assertIn('oldManifestExisted', windows)
        self.assertIn('oldManifestSha256', windows)
        self.assertIn('oldVersionExisted', windows)
        self.assertIn("throw 'installation changed; prepare again'", windows)
        self.assertIn('function Capture-InstalledBase', windows)
        capture_start = windows.index('function Capture-InstalledBase')
        capture_end = windows.index('function Get-InstalledManifest', capture_start)
        capture_slice = windows[capture_start:capture_end]
        self.assertLess(capture_slice.index('$before = Read-InstalledFingerprint'),
                        capture_slice.index('$ownedManifest = @(Get-InstalledManifest'))
        self.assertLess(capture_slice.index('$ownedManifest = @(Get-InstalledManifest'),
                        capture_slice.index('$after = Read-InstalledFingerprint'))
        self.assertIn("throw 'installation changed during preparation; retry'", capture_slice)
        prepare_start = windows.index('function Prepare-And-Apply')
        prepare_end = windows.index('function Apply-PreparedRelease', prepare_start)
        prepare_slice = windows[prepare_start:prepare_end]
        self.assertIn('$installedBase = Capture-InstalledBase $source $Folder', prepare_slice)
        self.assertIn('$oldManifest = @($installedBase.ownedManifest)', prepare_slice)
        self.assertIn('targetWasFresh=$installedBase.fresh', prepare_slice)
        self.assertNotIn('targetWasFresh=(-not (Test-Path', prepare_slice)
        apply_start = windows.index('function Apply-PreparedRelease')
        apply_end = windows.index('\nif ($Elevated)', apply_start)
        apply_slice = windows[apply_start:apply_end]
        cas_position = apply_slice.index("throw 'installation changed; prepare again'")
        self.assertLess(cas_position, apply_slice.index('$service = Get-Service'))
        self.assertLess(cas_position, apply_slice.index("Invoke-ServiceOperation 'stop'"))
        self.assertIn('Get-Content $currentVersionPath -Raw', apply_slice)
        self.assertIn('-cne [string]$metadata.oldVersion', apply_slice)
        self.assertIn('Get-FileHash $currentManifestPath -Algorithm SHA256', apply_slice)
        elevated_slice = windows[elevated_start:elevated_end].lower()
        for forbidden in (' git ', 'pip ', 'python.exe', 'import infomonitordbclientes', 'build-candidateruntime'):
            self.assertNotIn(forbidden, elevated_slice)

    def test_windows_prepared_base_cas_rejeita_v1_quando_alvo_virou_v3(self):
        def installation_changed(prepared, current):
            return (
                prepared['fresh'] != current['fresh']
                or prepared['manifest_exists'] != current['manifest_exists']
                or prepared['version_exists'] != current['version_exists']
                or (current['manifest_exists'] and
                    prepared['manifest_hash'].casefold() != current['manifest_hash'].casefold())
                or (current['version_exists'] and
                    prepared['version'] != current['version'])
            )

        prepared_v1 = {
            'fresh': False, 'manifest_exists': True, 'manifest_hash': 'a' * 64,
            'version_exists': True, 'version': 'v1\n',
        }
        current_v3 = dict(prepared_v1, version='v3\n')
        self.assertTrue(installation_changed(prepared_v1, current_v3))
        self.assertFalse(installation_changed(prepared_v1, dict(prepared_v1)))

        # A derivacao de propriedade entre estas leituras nao pode combinar
        # arquivos de V1 com metadados de V3.
        before_v1 = dict(prepared_v1)
        after_v3 = dict(prepared_v1, version='v3\n', manifest_hash='b' * 64)
        self.assertTrue(installation_changed(before_v1, after_v3))

        fresh = {
            'fresh': True, 'manifest_exists': False, 'manifest_hash': None,
            'version_exists': False, 'version': None,
        }
        newly_installed = dict(prepared_v1)
        self.assertTrue(installation_changed(fresh, newly_installed))

    def test_dados_operacionais_estao_ignorados(self):
        ignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
        for regra in ('.env', '*.db', 'known_hosts', 'static/fotos/*', '*.log'):
            self.assertIn(regra, ignore)
        self.assertIn('data/', ignore)


if __name__ == '__main__':
    unittest.main()
