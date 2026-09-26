[CmdletBinding()]
param(
    [ValidateSet("Tts", "Video")]
    [string]$Purpose,
    [string]$Distribution,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$RepositoryUrl = "https://github.com/vonvonhero/narration-video-gen.git"
$RepositoryDirectory = "narration-video-gen"
$DockerDesktopUrl = "https://docs.docker.com/desktop/setup/install/windows-install/"
$DockerDesktopTermsUrl = "https://www.docker.com/legal/docker-subscription-service-agreement/"
$DockerDesktopPackageId = "Docker.DockerDesktop"
$NvidiaDriverUrl = "https://www.nvidia.com/Download/index.aspx"
$WslInstallUrl = "https://learn.microsoft.com/windows/wsl/install"
$SetupStateDirectory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "NarrationVideoGen"
$PurposeStatePath = Join-Path $SetupStateDirectory "setup-purpose.txt"
$InstalledLauncherPath = Join-Path $SetupStateDirectory "setup.cmd"
$InstalledSetupScriptPath = Join-Path $SetupStateDirectory "scripts\setup-windows.ps1"
$InstalledCleanupLauncherPath = Join-Path $SetupStateDirectory "cleanup.cmd"
$InstalledCleanupScriptPath = Join-Path $SetupStateDirectory "scripts\cleanup-windows.ps1"
$DesktopShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Narration Video Gen.lnk"
$SourceLauncherPath = Join-Path (Split-Path -Parent $PSScriptRoot) "setup.cmd"
$SourceCleanupLauncherPath = Join-Path (Split-Path -Parent $PSScriptRoot) "cleanup.cmd"
$SourceCleanupScriptPath = Join-Path $PSScriptRoot "cleanup-windows.ps1"
$WslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$DockerSettingsPath = Join-Path $env:APPDATA "Docker\settings-store.json"
$VideoWslMemoryGiB = 20
$VideoWslSwapGiB = 32
$script:RestartDockerAfterWslShutdown = $false
$UiLanguage = if ([Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq "ja") {
    "ja"
} else {
    "en"
}

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
    # Redirected consoles on older Windows can reject encoding changes.
}

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "== $Title" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "[--] $Message" -ForegroundColor Yellow
}

function Write-Ng([string]$Message) {
    Write-Host "[NG] $Message" -ForegroundColor Red
}

function Confirm-Action([string]$Message) {
    $answer = Read-Host "$Message [y/N]"
    return $answer -match '^(?i:y|yes)$'
}

function Confirm-ActionDefaultYes([string]$Message) {
    $answer = Read-Host "$Message [Y/n]"
    return -not $answer -or $answer -match '^(?i:y|yes)$'
}

function Write-DesktopShortcut {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($DesktopShortcutPath)
    $shortcut.TargetPath = $InstalledLauncherPath
    $shortcut.WorkingDirectory = $SetupStateDirectory
    $shortcut.Description = "動画・音声作成を開始します"
    $shortcut.Save()
}

function Install-DesktopShortcut {
    try {
        $installedScriptDirectory = Split-Path -Parent $InstalledSetupScriptPath
        $null = New-Item -ItemType Directory -Path $installedScriptDirectory -Force

        if ([IO.Path]::GetFullPath($SourceLauncherPath) -ne
                [IO.Path]::GetFullPath($InstalledLauncherPath)) {
            Copy-Item -LiteralPath $SourceLauncherPath -Destination $InstalledLauncherPath -Force
        }
        if ([IO.Path]::GetFullPath($PSCommandPath) -ne
                [IO.Path]::GetFullPath($InstalledSetupScriptPath)) {
            Copy-Item -LiteralPath $PSCommandPath -Destination $InstalledSetupScriptPath -Force
        }
        if (Test-Path -LiteralPath $SourceCleanupLauncherPath) {
            Copy-Item -LiteralPath $SourceCleanupLauncherPath `
                -Destination $InstalledCleanupLauncherPath -Force
        }
        if (Test-Path -LiteralPath $SourceCleanupScriptPath) {
            Copy-Item -LiteralPath $SourceCleanupScriptPath `
                -Destination $InstalledCleanupScriptPath -Force
        }

        Write-DesktopShortcut
        Write-Ok "デスクトップショートカット: Narration Video Gen"
        return $true
    } catch {
        Write-Warn "デスクトップショートカットを作成できませんでした: $($_.Exception.Message)"
        return $false
    }
}

function Ensure-DesktopShortcut {
    if (Test-Path -LiteralPath $DesktopShortcutPath) {
        $null = Install-DesktopShortcut
        return
    }
    if (Confirm-ActionDefaultYes "デスクトップに「Narration Video Gen」のショートカットを作成しますか？") {
        $null = Install-DesktopShortcut
    }
}

function Reset-TtsWebUiPassword([string]$Distro) {
    Invoke-Wsl $Distro `
        'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen tts web reset-password'
    if ($script:WslExitCode -ne 0) {
        Write-Warn "TTS WebUIのパスワードを再設定できませんでした。表示された内容を確認してください。"
        return $false
    }
    return $true
}

function Start-TtsWebUi([string]$Distro) {
    Write-Host ""
    Write-Host "Web UIの公開範囲を選んでください。"
    Write-Host "  1. このPCからのみアクセス (localhost)"
    Write-Host "  2. ローカルネットワークからもアクセス (パスワード必須)"
    Write-Host "  0. キャンセル"

    while ($true) {
        $choice = Read-Host "選択 [1/2/0]"
        if ($null -eq $choice) { return }
        switch ($choice.Trim()) {
            "1" {
                Invoke-Wsl $Distro `
                    'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen tts web start'
                break
            }
            "2" {
                $passwordConfigured = Test-Wsl $Distro `
                    'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen tts web password-status >/dev/null 2>&1'
                if (-not $passwordConfigured) {
                    Write-Warn "ローカルネットワークへ公開するにはパスワードの設定が必要です。"
                    if (-not (Reset-TtsWebUiPassword $Distro)) { return }
                }
                Invoke-Wsl $Distro `
                    'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen tts web start --lan'
                break
            }
            "0" { return }
            default {
                Write-Warn "0、1、2のいずれかを入力してください。"
                continue
            }
        }

        if ($script:WslExitCode -ne 0) {
            Write-Warn "キャラクター・音声作成ページを起動できませんでした。表示された内容を確認してください。"
        }
        return
    }
}

function Show-VideoMenu([string]$Distro, [int]$ProgressColumns) {
    while ($true) {
        Write-Section "動画生成メニュー"
        Write-Host "  1. キャラクターと音声を作る (Web UI)"
        Write-Host "  2. 動画の構成選択と準備 (plan)"
        Write-Host "  3. 動画を生成 (run)"
        Write-Host "  4. 生成状況を確認 (status)"
        Write-Host "  5. 実行中の生成を中止 (cancel)"
        Write-Host "  6. TTS WebUIのパスワードを再設定"
        Write-Host "  7. 容量を解放する (cleanup)"
        Write-Host "  8. リポジトリとWindows起動ファイルを更新"
        Write-Host "  0. 終了"
        $choice = Read-Host "番号を入力してください"
        if ($null -eq $choice) { return }
        $choice = $choice.Trim()

        switch ($choice) {
            "1" {
                Start-TtsWebUi $Distro
            }
            "2" {
                Invoke-Wsl $Distro `
                    ('cd "$HOME/narration-video-gen" && COLUMNS={0} NVG_DOWNLOAD_PROGRESS=inline ./bin/narration-video-gen plan' -f $ProgressColumns)
                if ($script:WslExitCode -ne 0) {
                    $request = Get-PendingPlanWslSetup $Distro
                    if ($request -and (Apply-PendingPlanWslSetup $Distro $request)) {
                        Invoke-Wsl $Distro `
                            ('cd "$HOME/narration-video-gen" && COLUMNS={0} NVG_DOWNLOAD_PROGRESS=inline ./bin/narration-video-gen plan --profile {1}' -f $ProgressColumns, $request.ProfileId)
                    }
                    if ($script:WslExitCode -ne 0) {
                        Write-Warn "動画の準備は完了していません。表示された内容を確認してください。"
                    }
                }
            }
            "3" {
                Invoke-Wsl $Distro `
                    'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen run'
                if ($script:WslExitCode -ne 0) {
                    Write-Warn "動画生成は開始されませんでした。表示された内容を確認してください。"
                }
            }
            "4" {
                Invoke-Wsl $Distro `
                    'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen status'
            }
            "5" {
                if (Confirm-Action "実行中の動画生成を中止しますか？") {
                    Invoke-Wsl $Distro `
                        'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen cancel'
                }
            }
            "6" {
                $null = Reset-TtsWebUiPassword $Distro
            }
            "7" {
                $cleanupLauncher = if (Test-Path -LiteralPath $InstalledCleanupLauncherPath) {
                    $InstalledCleanupLauncherPath
                } else { $SourceCleanupLauncherPath }
                if (Test-Path -LiteralPath $cleanupLauncher) {
                    Start-Process -FilePath $cleanupLauncher `
                        -WorkingDirectory (Split-Path -Parent $cleanupLauncher)
                } else {
                    Write-Warn "クリーンアップを起動できません。setup.cmdを更新してください。"
                }
            }
            "8" {
                if (Update-RepositoryAndWindowsLauncher $Distro) { return }
            }
            "0" { return }
            default { Write-Warn "0から8の番号を入力してください。" }
        }
    }
}

function Read-SetupPurpose {
    Write-Host ""
    Write-Host "用途を選んでください。"
    Write-Host "  1. 音声作成だけ（CPUでも利用可能）"
    Write-Host "  2. 動画生成も使う（NVIDIA GPUが必要）"
    Write-Host "  0. 終了"
    while ($true) {
        $choice = Read-Host "選択 [1/2/0]"
        if ($null -eq $choice) { return $null }
        switch ($choice.Trim()) {
            "1" { return "Tts" }
            "2" { return "Video" }
            "0" { return $null }
            default { Write-Warn "0、1、2のいずれかを入力してください。" }
        }
    }
}

function Get-SavedPurpose {
    if (-not (Test-Path -LiteralPath $PurposeStatePath)) { return $null }
    try {
        $saved = (Get-Content -LiteralPath $PurposeStatePath -Raw).Trim()
        if ($saved -in @("Tts", "Video")) { return $saved }
    } catch {
        Write-Warn "保存済みの用途を読み取れません。今回の用途を選び直します。"
    }
    return $null
}

function Save-SetupPurpose([string]$Value) {
    try {
        $null = New-Item -ItemType Directory -Path $SetupStateDirectory -Force
        Set-Content -LiteralPath $PurposeStatePath -Value $Value -Encoding ASCII -NoNewline
    } catch {
        Write-Warn "用途を保存できません。次回の実行でも用途の選択が必要になります。"
    }
}

function Get-PurposeLabel([string]$Value) {
    if ($Value -eq "Tts") { return "音声作成だけ" }
    return "動画生成も使う"
}

function Get-Wsl2Setting([string]$Name) {
    if (-not (Test-Path -LiteralPath $WslConfigPath)) { return $null }
    $inWsl2 = $false
    $pattern = '^\s*{0}\s*=\s*([^#;]+)' -f [regex]::Escape($Name)
    foreach ($line in Get-Content -LiteralPath $WslConfigPath) {
        if ($line -match '^\s*\[([^]]+)\]') {
            $inWsl2 = $matches[1] -ieq 'wsl2'
            continue
        }
        if ($inWsl2 -and $line -match $pattern) { return $matches[1].Trim() }
    }
    return $null
}

function Convert-WslSizeToGiB([string]$Value) {
    if (-not $Value -or $Value -notmatch '^\s*(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)\s*$') {
        return $null
    }
    $number = [double]::Parse($matches[1], [Globalization.CultureInfo]::InvariantCulture)
    switch ($matches[2].ToUpperInvariant()) {
        'KB' { return $number / 1MB }
        'MB' { return $number / 1024 }
        'GB' { return $number }
        'TB' { return $number * 1024 }
    }
}

function Set-Wsl2Resources([int]$MemoryGiB, [int]$SwapGiB) {
    # Keep allocations that already exceed the minimum when the other value
    # needs an update (for example, memory=48GB with swap=32GB).
    $existingMemory = Convert-WslSizeToGiB (Get-Wsl2Setting "memory")
    $existingSwap = Convert-WslSizeToGiB (Get-Wsl2Setting "swap")
    if ($null -ne $existingMemory) { $MemoryGiB = [math]::Max($MemoryGiB, [math]::Ceiling($existingMemory)) }
    if ($null -ne $existingSwap) { $SwapGiB = [math]::Max($SwapGiB, [math]::Ceiling($existingSwap)) }
    $hadConfig = Test-Path -LiteralPath $WslConfigPath
    $lines = if ($hadConfig) { @(Get-Content -LiteralPath $WslConfigPath) } else { @() }
    if ($hadConfig) {
        $backup = "$WslConfigPath.backup-$((Get-Date).ToString('yyyyMMdd-HHmmss-fffffff'))"
        Copy-Item -LiteralPath $WslConfigPath -Destination $backup
        Write-Ok "既存の.wslconfigをバックアップしました: $backup"
    }

    $output = New-Object System.Collections.Generic.List[string]
    $inWsl2 = $false
    $foundWsl2 = $false
    $memoryWritten = $false
    $swapWritten = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*\[([^]]+)\]') {
            if ($inWsl2) {
                if (-not $memoryWritten) { $output.Add("memory=${MemoryGiB}GB"); $memoryWritten = $true }
                if (-not $swapWritten) { $output.Add("swap=${SwapGiB}GB"); $swapWritten = $true }
            }
            $inWsl2 = $matches[1] -ieq 'wsl2'
            if ($inWsl2) { $foundWsl2 = $true }
            $output.Add($line)
            continue
        }
        if ($inWsl2 -and $line -match '^\s*memory\s*=') {
            if (-not $memoryWritten) { $output.Add("memory=${MemoryGiB}GB") }
            $memoryWritten = $true
            continue
        }
        if ($inWsl2 -and $line -match '^\s*swap\s*=') {
            if (-not $swapWritten) { $output.Add("swap=${SwapGiB}GB") }
            $swapWritten = $true
            continue
        }
        $output.Add($line)
    }
    if ($inWsl2) {
        if (-not $memoryWritten) { $output.Add("memory=${MemoryGiB}GB") }
        if (-not $swapWritten) { $output.Add("swap=${SwapGiB}GB") }
    } elseif (-not $foundWsl2) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1]) { $output.Add("") }
        $output.Add("[wsl2]")
        $output.Add("memory=${MemoryGiB}GB")
        $output.Add("swap=${SwapGiB}GB")
    }

    $temporary = "$WslConfigPath.tmp-$PID"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporary,
        (($output -join [Environment]::NewLine) + [Environment]::NewLine), $encoding)
    Move-Item -LiteralPath $temporary -Destination $WslConfigPath -Force
}

function Get-WslDistributions {
    # Windows PowerShell 5.1 promotes native stderr to an ErrorRecord when the
    # script-wide preference is Stop.  An unconfigured WSL writes its guidance
    # to stderr, which must mean "no distributions" rather than aborting before
    # the installation prompt.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = & wsl.exe --list --quiet 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { return @() }
    return @(($raw -join "`n") -replace "`0", "" -split "`r?`n" |
        ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-WslDistributionVersion([string]$Distro) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = & wsl.exe --list --verbose 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { return $null }
    $pattern = '^\s*\*?\s*{0}\s+.+?\s+([12])\s*$' -f [regex]::Escape($Distro)
    foreach ($line in (($raw -join "`n") -replace "`0", "" -split "`r?`n")) {
        if ($line -match $pattern) { return [int]$matches[1] }
    }
    return $null
}

function Ensure-Wsl2([string]$Distro) {
    $version = Get-WslDistributionVersion $Distro
    if ($version -eq 2) { return $true }
    if ($version -ne 1) {
        Write-Ng "WSLのバージョンを確認できません。PowerShellで wsl --list --verbose を確認してください。"
        return $false
    }
    Write-Warn "$Distro はWSL1です。Docker Desktopを使うにはWSL2への変換が必要です。"
    if ($Check) { return $false }
    if (-not (Confirm-Action "$Distro をWSL2へ変換しますか？ 完了まで数分かかる場合があります。")) { return $false }
    & wsl.exe --set-version $Distro 2 | Out-Host
    if ($LASTEXITCODE -ne 0 -or (Get-WslDistributionVersion $Distro) -ne 2) {
        Write-Ng "WSL2への変換が完了していません。表示された内容を確認してsetup.cmdを再実行してください。"
        return $false
    }
    Write-Ok "$Distro をWSL2へ変換しました。"
    return $true
}

function Get-WslOsRelease([string]$Distro) {
    # Read the file directly instead of sending a compound shell expression
    # through Windows PowerShell's native-command argument conversion. This is
    # also valid immediately after Ubuntu's interactive first-run process exits.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = @(& wsl.exe -d $Distro -- cat /etc/os-release 2>$null)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { return $null }

    $values = @{}
    foreach ($line in (($raw -join "`n") -replace "`0", "" -split "`r?`n")) {
        if ($line -match '^([A-Z_]+)=(.*)$') {
            $value = $matches[2].Trim()
            if ($value.Length -ge 2 -and
                    (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or
                     ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$matches[1]] = $value
        }
    }
    return [pscustomobject]@{
        Id = $values["ID"]
        VersionId = $values["VERSION_ID"]
    }
}

function Test-SupportedUbuntu([string]$Distro) {
    $release = Get-WslOsRelease $Distro
    return ($null -ne $release -and $release.Id -eq "ubuntu" -and
            $release.VersionId -in @("24.04", "26.04"))
}

function Invoke-Wsl([string]$Distro, [string]$Command) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & wsl.exe -d $Distro -- bash -lc $Command
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $script:WslExitCode = $exitCode
}

function Copy-WslLaunchFilesToStage([string]$Distro, [string]$Staging) {
    $previousStage = [Environment]::GetEnvironmentVariable("NVG_UPDATE_STAGE", "Process")
    $previousWslEnv = [Environment]::GetEnvironmentVariable("WSLENV", "Process")
    try {
        $env:NVG_UPDATE_STAGE = $Staging
        $wslEnvEntries = @($previousWslEnv -split ':' | Where-Object {
            $_ -and $_ -notmatch '^(?i:NVG_UPDATE_STAGE)(?:/.*)?$'
        })
        $env:WSLENV = (@($wslEnvEntries) + "NVG_UPDATE_STAGE/p") -join ':'
        $copyScript = @'
set -eu
repo="$HOME/narration-video-gen"
stage="$NVG_UPDATE_STAGE"
test -d "$stage"
cp -- "$repo/setup.cmd" "$stage/file-0"
cp -- "$repo/cleanup.cmd" "$stage/file-1"
cp -- "$repo/scripts/setup-windows.ps1" "$stage/file-2"
cp -- "$repo/scripts/cleanup-windows.ps1" "$stage/file-3"
'@
        $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($copyScript))
        Invoke-Wsl $Distro ("printf %s {0} | base64 -d | bash" -f $encoded)
        return $script:WslExitCode -eq 0
    } finally {
        if ($null -eq $previousStage) {
            Remove-Item Env:NVG_UPDATE_STAGE -ErrorAction SilentlyContinue
        } else {
            $env:NVG_UPDATE_STAGE = $previousStage
        }
        if ($null -eq $previousWslEnv) {
            Remove-Item Env:WSLENV -ErrorAction SilentlyContinue
        } else {
            $env:WSLENV = $previousWslEnv
        }
    }
}

function Install-WindowsLaunchFilesFromRepository([string]$Distro) {
    $staging = Join-Path ([IO.Path]::GetTempPath()) `
        ("narration-video-gen-update-" + [Guid]::NewGuid().ToString("N"))
    try {
        $null = New-Item -ItemType Directory -Path $staging
        if (-not (Copy-WslLaunchFilesToStage $Distro $staging)) {
            Write-Warn "WSL側から更新用ファイルをコピーできませんでした。"
            return $false
        }
        $files = @(
            [pscustomobject]@{ Stage = Join-Path $staging "file-0"; Destination = $InstalledLauncherPath },
            [pscustomobject]@{ Stage = Join-Path $staging "file-1"; Destination = $InstalledCleanupLauncherPath },
            [pscustomobject]@{ Stage = Join-Path $staging "file-2"; Destination = $InstalledSetupScriptPath },
            [pscustomobject]@{ Stage = Join-Path $staging "file-3"; Destination = $InstalledCleanupScriptPath }
        )
        foreach ($file in $files) {
            if (-not (Test-Path -LiteralPath $file.Stage -PathType Leaf)) {
                throw "staged update file is missing: $($file.Stage)"
            }
            $null = New-Item -ItemType Directory -Path (Split-Path -Parent $file.Destination) -Force
            Copy-Item -LiteralPath $file.Stage -Destination $file.Destination -Force
        }
        Write-DesktopShortcut
        Write-Ok "Windows起動ファイルとデスクトップショートカットを更新しました。"
        return $true
    } catch {
        Write-Warn "Windows起動ファイルを更新できませんでした: $($_.Exception.Message)"
        return $false
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Update-RepositoryAndWindowsLauncher([string]$Distro) {
    Write-Section "更新"
    if (-not (Confirm-Action "WSL側リポジトリとWindows起動ファイルを更新しますか？")) {
        return $false
    }
    $repository = '$HOME/{0}' -f $RepositoryDirectory
    if (-not (Test-Wsl $Distro ('test -d "{0}/.git"' -f $repository))) {
        Write-Warn "WSL側のリポジトリが見つかりません。setup.cmdを再実行してください。"
        return $false
    }
    if (-not (Test-Wsl $Distro `
            ('test "$(git -C "{0}" remote get-url origin)" = "{1}"' -f $repository, $RepositoryUrl))) {
        Write-Warn "WSL側リポジトリのoriginが公式URLと一致しないため、更新しません。"
        return $false
    }
    if (-not (Test-Wsl $Distro ('test -z "$(git -C "{0}" status --porcelain)"' -f $repository))) {
        Write-Warn "WSL側リポジトリに未コミット変更があります。変更を整理してから更新してください。"
        return $false
    }
    if (-not (Test-Wsl $Distro ('test "$(git -C "{0}" branch --show-current)" = main' -f $repository))) {
        Write-Warn "WSL側リポジトリがmainブランチではないため、更新しません。"
        return $false
    }

    Invoke-Wsl $Distro ('git -C "{0}" fetch --prune origin' -f $repository)
    if ($script:WslExitCode -ne 0) {
        Write-Warn "最新版を取得できませんでした。ネットワーク接続を確認してください。"
        return $false
    }
    if (-not (Test-Wsl $Distro `
            ('git -C "{0}" merge-base --is-ancestor HEAD origin/main' -f $repository))) {
        Write-Warn "WSL側mainに未pushのコミットがあるため、自動更新しません。"
        return $false
    }
    Invoke-Wsl $Distro ('git -C "{0}" merge --ff-only origin/main' -f $repository)
    if ($script:WslExitCode -ne 0) {
        Write-Warn "WSL側リポジトリをfast-forward更新できませんでした。"
        return $false
    }
    if (-not (Install-WindowsLaunchFilesFromRepository $Distro)) {
        Write-Warn "WSL側は更新済みですが、Windows起動ファイルの更新は完了していません。"
        return $false
    }
    Write-Ok "更新が完了しました。"
    Write-Warn "この画面を閉じ、デスクトップの「Narration Video Gen」を開き直してください。"
    return $true
}

function Get-PendingPlanWslSetup([string]$Distro) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = & wsl.exe -d $Distro -- bash -lc `
            'cd "$HOME/narration-video-gen" && ./bin/narration-video-gen --json show' 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0 -or -not $raw) { return $null }
    try {
        $payload = (($raw -join "`n") -replace "`0", "") | ConvertFrom-Json
        $setup = $payload.host_setup
        if (-not $setup -or
                -not ($setup.PSObject.Properties.Name -contains "wsl_swap_gib")) {
            return $null
        }
        $profileId = [string]$payload.profile.id
        $resolution = [int]$payload.recipe.resolution[1]
        $memoryGiB = [int][math]::Ceiling([double]$setup.wsl_ram_gib)
        $swapGiB = [int][math]::Ceiling([double]$setup.wsl_swap_gib)
        if ($profileId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' -or
                $resolution -lt 720 -or $memoryGiB -lt 1 -or $memoryGiB -gt 256 -or
                $swapGiB -lt 1 -or $swapGiB -gt 256) {
            return $null
        }
        return [pscustomobject]@{
            ProfileId = $profileId
            Resolution = $resolution
            MemoryGiB = $memoryGiB
            SwapGiB = $swapGiB
            CurrentMemoryGiB = [double]$setup.current_ram_gib
            CurrentSwapGiB = [double]$setup.current_swap_gib
        }
    } catch {
        Write-Warn "planが返したWSL設定要求を読み取れませんでした。"
        return $null
    }
}

function Apply-PendingPlanWslSetup([string]$Distro, $Request) {
    Write-Section "720p用WSL設定"
    Write-Host ("現在のswap: {0:N1} GiB / 720pに必要: {1} GiB" -f `
        $Request.CurrentSwapGiB, $Request.SwapGiB)
    $additionalGiB = [math]::Max(0, $Request.SwapGiB - $Request.CurrentSwapGiB)
    Write-Host ("swapファイルは現在より最大約{0:N1} GiB多くディスクを使用する可能性があります。" -f `
        $additionalGiB)
    if (-not (Confirm-Action ".wslconfigのswapを$($Request.SwapGiB)GBへ増やしますか？")) {
        Write-Warn "WSL設定は変更しません。現在の設定で720pは生成できません。"
        return $false
    }
    Write-Warn "反映時にDocker DesktopとすべてのWSLディストリビューションを停止します。"
    if (-not (Confirm-Action "設定を変更し、wsl --shutdownを実行しますか？")) {
        Write-Warn "WSL設定は変更しません。"
        return $false
    }

    Set-Wsl2Resources $Request.MemoryGiB $Request.SwapGiB
    Write-Ok ".wslconfigを720p用に更新しました: memory=$($Request.MemoryGiB)GB以上, swap=$($Request.SwapGiB)GB以上"
    $dockerDesktop = Get-DockerDesktopExecutable
    if (-not (Stop-DockerDesktopForWslShutdown)) {
        Write-Warn "Docker Desktopを終了してsetup.cmdを再実行してください。"
        return $false
    }
    & wsl.exe --shutdown
    if ($LASTEXITCODE -ne 0) { throw "wsl --shutdown failed with exit code $LASTEXITCODE" }
    Write-Ok "WSLを停止し、720p用設定を反映しました。"
    if ($script:RestartDockerAfterWslShutdown) {
        if (-not $dockerDesktop -or -not (Restart-DockerDesktop $dockerDesktop 120)) {
            return $false
        }
        $script:RestartDockerAfterWslShutdown = $false
    }
    return $true
}

function Test-Wsl([string]$Distro, [string]$Command) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & wsl.exe -d $Distro -- bash -lc $Command *> $null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $exitCode -eq 0
}

function Test-UbuntuInitialized([string]$Distro) {
    # A command supplied with `wsl.exe --` does not start Ubuntu's interactive
    # first-run screen.  Before that screen has created a default user, commands
    # run as root (uid 0); afterwards they run as the new regular user.
    return (Test-Wsl $Distro 'test "$(id -u)" -ge 1000')
}

function Initialize-Ubuntu([string]$Distro) {
    if (Test-UbuntuInitialized $Distro) { return $true }

    Write-Section "Ubuntuの初期設定"
    Write-Host "これからUbuntuの画面が開きます。次の順番で入力してください。"
    Write-Host "  1. 新しいLinuxユーザー名を入力する（Windowsと同じ名前でも構いません）"
    Write-Host "  2. 新しいパスワードを入力する（入力中は文字も * も表示されません）"
    Write-Host "  3. 確認のため、同じパスワードをもう一度入力する"
    Write-Host "  4. user@computer:~$ のような表示になったら exit と入力してEnterを押す"
    Write-Host "設定が終わる前に画面を閉じた場合は、setup.cmdを再実行できます。"
    Write-Host ""
    Read-Host "準備ができたらEnterを押してUbuntuを開きます"

    # The exit code is deliberately not trusted here. Closing the terminal can
    # report STATUS_CONTROL_C_EXIT even after the user was created successfully.
    $null = Start-Process wsl.exe -Wait -PassThru -ArgumentList @("-d", $Distro)
    if (Test-UbuntuInitialized $Distro) {
        Write-Ok "Ubuntuのユーザー作成が完了しました。"
        return $true
    }

    Write-Warn "Ubuntuのユーザー作成はまだ完了していません。setup.cmdを再実行すると、この画面から再開します。"
    return $false
}

function Show-ManualAction([string]$Message, [string]$Url) {
    Write-Ng $Message
    Write-Host "    $Url"
    if (-not $Check -and (Confirm-Action "公式ページをブラウザで開きますか？")) {
        Start-Process $Url
    }
}

function Install-Ubuntu {
    if (-not (Confirm-Action "Ubuntu 24.04とWSL2を導入しますか？ 管理者権限と再起動が必要になる場合があります。")) {
        exit 2
    }
    # Make the restart handoff available before the elevated installer runs.
    # The shortcut uses a self-contained copy under LocalAppData, so the user
    # does not need to find the extracted repository again after rebooting.
    Ensure-DesktopShortcut
    $resumeShortcutReady = Test-Path -LiteralPath $DesktopShortcutPath
    $targetDistribution = "Ubuntu-24.04"
    $process = Start-Process wsl.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList @("--install", "-d", $targetDistribution, "--no-launch")
    $installed = @(Get-WslDistributions) -contains $targetDistribution
    if ($process.ExitCode -ne 0 -and -not $installed) {
        throw "wsl --install failed with exit code $($process.ExitCode)"
    }
    if (-not $installed) {
        if ($resumeShortcutReady) {
            Write-Warn "WSL2の導入を反映するためWindowsを再起動してください。再起動後、デスクトップの「Narration Video Gen」を開くと続きから進みます。"
        } else {
            Write-Warn "WSL2の導入を反映するためWindowsを再起動してください。再起動後、setup.cmdをもう一度実行すると続きから進みます。"
        }
        exit 0
    }
    Write-Ok "$targetDistribution を導入しました。"
    if (-not (Initialize-Ubuntu $targetDistribution)) { exit 0 }
    return $targetDistribution
}

function Get-DockerDesktopExecutable {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Get-DockerCliExecutable {
    $bundled = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    if (Test-Path $bundled) { return $bundled }
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Save-WslUiLanguage([string]$Distro, [string]$Language) {
    if ($Language -notin @("ja", "en")) { throw "unsupported UI language: $Language" }
    $temporaryName = "ui-language.tmp-$([Guid]::NewGuid().ToString('N'))"
    $command = 'umask 077 && install -d -m 700 "$HOME/.config/narration-video-gen" && printf ''%s\n'' "{0}" >"$HOME/.config/narration-video-gen/{1}" && mv -f "$HOME/.config/narration-video-gen/{1}" "$HOME/.config/narration-video-gen/ui-language"' -f $Language, $temporaryName
    Invoke-Wsl $Distro $command
    if ($script:WslExitCode -ne 0) {
        Write-Ng "WSL側へ表示言語を保存できませんでした。"
        return $false
    }
    Write-Ok "表示言語: $(if ($Language -eq 'ja') { '日本語' } else { 'English' })"
    return $true
}

function Get-DockerDesktopControlExecutable {
    $candidate = Join-Path $env:ProgramFiles "Docker\Docker\DockerCli.exe"
    if (Test-Path $candidate) { return $candidate }
    return $null
}

function Test-DockerDesktopEngine {
    $docker = Get-DockerCliExecutable
    if (-not $docker) { return $false }

    # A half-stopped Docker Desktop can leave the CLI waiting forever. Run the
    # probe as a child process so setup.cmd always regains control.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $docker
    $startInfo.Arguments = "version --format '{{.Server.Version}}'"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $false }
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            $process.WaitForExit()
            return $false
        }
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        $process.Dispose()
    }
}

function Stop-DockerDesktopForWslShutdown {
    $backend = Get-Process -Name "com.docker.backend" -ErrorAction SilentlyContinue
    $frontend = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $backend -and -not $frontend) { return $true }

    $control = Get-DockerDesktopControlExecutable
    if (-not $control) {
        Write-Ng "Docker Desktopを安全に終了するDockerCli.exeが見つかりません。"
        return $false
    }

    Write-Warn "WSLを停止する前にDocker Desktopを安全に終了します。"
    $shutdown = Start-Process $control -ArgumentList "-Shutdown" `
        -WindowStyle Hidden -PassThru
    if (-not $shutdown.WaitForExit(30000)) {
        $shutdown.Kill()
        Write-Ng "Docker Desktopの終了要求がタイムアウトしました。"
        return $false
    }

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name "com.docker.backend" -ErrorAction SilentlyContinue)) {
            $script:RestartDockerAfterWslShutdown = $true
            Write-Ok "Docker Desktopを終了しました。"
            return $true
        }
        Start-Sleep -Seconds 1
    }
    Write-Ng "Docker Desktopのバックエンドが終了しませんでした。"
    return $false
}

function Enable-DockerWslIntegration([string]$Distro) {
    if (-not (Test-Path -LiteralPath $DockerSettingsPath)) {
        Write-Ng "Docker Desktopの設定ファイルが見つかりません: $DockerSettingsPath"
        return $false
    }
    try {
        $settings = Get-Content -LiteralPath $DockerSettingsPath -Raw | ConvertFrom-Json
        $distros = @($settings.IntegratedWslDistros | Where-Object { $_ })
        if ($distros -notcontains $Distro) { $distros += $Distro }
        if ($settings.PSObject.Properties.Name -contains "IntegratedWslDistros") {
            $settings.IntegratedWslDistros = @($distros)
        } else {
            $settings | Add-Member -NotePropertyName "IntegratedWslDistros" `
                -NotePropertyValue @($distros)
        }

        $backup = "$DockerSettingsPath.backup-$((Get-Date).ToString('yyyyMMdd-HHmmss-fffffff'))"
        Copy-Item -LiteralPath $DockerSettingsPath -Destination $backup
        $temporary = "$DockerSettingsPath.tmp-$PID"
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temporary,
            (($settings | ConvertTo-Json -Depth 30) + [Environment]::NewLine), $encoding)
        Move-Item -LiteralPath $temporary -Destination $DockerSettingsPath -Force
        Write-Ok "Docker DesktopのWSL統合を有効にしました: $Distro"
        return $true
    } catch {
        Write-Ng "Docker DesktopのWSL統合設定を更新できませんでした: $($_.Exception.Message)"
        return $false
    }
}

function Restart-DockerDesktop([string]$Executable, [int]$Seconds = 120) {
    Start-Process $Executable
    if (Wait-DockerDesktopEngine $Seconds) {
        Write-Ok "Dockerエンジンが再起動しました。"
        return $true
    }
    Write-Ng "Dockerエンジンを再起動できませんでした。Docker Desktop画面のエラーを確認してください。"
    return $false
}

function Wait-DockerDesktopEngine([int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    Write-Host -NoNewline "Dockerエンジンを確認しています"
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerDesktopEngine) {
            Write-Host ""
            return $true
        }
        Start-Sleep -Seconds 3
        Write-Host -NoNewline "."
    }
    Write-Host ""
    return $false
}

function Wait-DockerInWsl([string]$Distro, [int]$Seconds = 60) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    Write-Host -NoNewline "$Distro からDockerを確認しています"
    while ((Get-Date) -lt $deadline) {
        if (Test-Wsl $Distro 'timeout 10s docker version >/dev/null 2>&1 && timeout 10s docker compose version >/dev/null 2>&1') {
            Write-Host ""
            return $true
        }
        Start-Sleep -Seconds 3
        Write-Host -NoNewline "."
    }
    Write-Host ""
    return $false
}

function Install-DockerDesktop {
    Write-Warn "Docker Desktopが必要です。インストール前に利用条件を確認してください。"
    Write-Host "    $DockerDesktopTermsUrl"
    Write-Host "続行すると、WinGetのwingetソースとDocker Desktopパッケージの契約への同意を"
    Write-Host "インストールコマンドへ渡します。再起動は自動では行いません。"
    if (-not (Confirm-Action "WinGetを使ってDocker DesktopをこのPCへインストールしますか？")) {
        return $null
    }

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Show-ManualAction "WinGetを利用できません。Docker Desktopの公式手順から手動で導入してください。" $DockerDesktopUrl
        return $null
    }

    Write-Warn "Docker Desktopをダウンロードしてインストールしています。管理者確認が表示されたら許可してください。"
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        # Run WinGet attached directly to this console. Piping its progress
        # through Out-Host turns carriage-return spinner updates into hundreds
        # of separate lines on Windows PowerShell 5.1.
        $wingetProcess = Start-Process $winget.Source -NoNewWindow -Wait -PassThru `
            -ArgumentList @("install", "--id", $DockerDesktopPackageId,
                "--exact", "--source", "winget", "--silent",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity")
        $exitCode = $wingetProcess.ExitCode
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $executable = Get-DockerDesktopExecutable
    if (-not $executable) {
        Write-Ng "Docker Desktopの自動インストールに失敗しました（WinGet終了コード: $exitCode）。"
        Show-ManualAction "公式手順を確認して手動で導入してください。" $DockerDesktopUrl
        return $null
    }
    Write-Ok "Docker Desktopをインストールしました。"
    return $executable
}

function Test-OptimizeVhdAvailable {
    try {
        Import-Module Hyper-V -ErrorAction Stop
        return $null -ne (Get-Command Optimize-VHD -ErrorAction SilentlyContinue)
    } catch { return $false }
}

function Ensure-OptimizeVhdSupport {
    if (Test-OptimizeVhdAvailable) {
        Write-Ok "VHDX縮小ツール: Optimize-VHD"
        return $true
    }
    if ($Check) {
        Write-Ng "VHDX縮小用のHyper-V PowerShell管理ツールがありません。"
        return $false
    }
    Write-Warn "後でWSLの使用容量をWindowsへ返すため、Hyper-V PowerShell管理ツールを準備します。"
    Write-Host "Hyper-V仮想マシンを作成せず、Optimize-VHDを含む管理コンポーネントだけを有効化します。"
    if (-not (Confirm-Action "Hyper-V PowerShell管理ツールを有効化しますか？")) {
        return $false
    }
    $scriptPath = Join-Path ([IO.Path]::GetTempPath()) `
        ("nvg-enable-optimize-vhd-{0}.ps1" -f [Guid]::NewGuid())
    try {
        $content = @'
$ErrorActionPreference = "Stop"
Enable-WindowsOptionalFeature -Online `
    -FeatureName Microsoft-Hyper-V-Management-PowerShell -All -NoRestart | Out-Null
'@
        [IO.File]::WriteAllText($scriptPath, $content,
            (New-Object System.Text.UTF8Encoding($true)))
        $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
        $process = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden `
            -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "Hyper-V PowerShell管理ツールの有効化に失敗しました。"
        }
    } finally {
        Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    }
    if (Test-OptimizeVhdAvailable) {
        Write-Ok "VHDX縮小ツールを有効化しました。"
    } else {
        Write-Warn "管理ツールは有効化され、Windows再起動後に利用可能になります。セットアップは続行できます。"
    }
    return $true
}

Write-Host "Narration Video Gen - Windows + WSL2 セットアップ" -ForegroundColor White
Write-Host "必要な項目を順に確認してセットアップします。"

if (-not $Purpose) {
    $savedPurpose = Get-SavedPurpose
    if ($savedPurpose) {
        $Purpose = $savedPurpose
    } elseif ($Check) {
        $Purpose = "Video"
        Write-Warn "動画用の状態を確認します。音声だけなら -Purpose Tts を指定してください。"
    } else {
        $Purpose = Read-SetupPurpose
        if (-not $Purpose) { exit 0 }
        Save-SetupPurpose $Purpose
    }
} elseif (-not $Check) {
    Save-SetupPurpose $Purpose
}

function Ensure-VideoWslResources([string]$Distro) {
    $memoryText = Get-Wsl2Setting "memory"
    $swapText = Get-Wsl2Setting "swap"
    $memoryGiB = Convert-WslSizeToGiB $memoryText
    $swapGiB = Convert-WslSizeToGiB $swapText
    $configReady = ($null -ne $memoryGiB -and $memoryGiB -ge $VideoWslMemoryGiB -and
                    $null -ne $swapGiB -and $swapGiB -ge $VideoWslSwapGiB)
    $runtimeReady = Test-Wsl $Distro `
        'awk ''/MemTotal/{ram=\$2}/SwapTotal/{swap=\$2} END{exit !(ram >= 19*1024*1024 && swap >= 31*1024*1024)}'' /proc/meminfo'

    if ($configReady -and $runtimeReady) {
        Write-Ok "WSLリソース: memory=$memoryText, swap=$swapText"
        return $true
    }
    if ($Check) {
        if (-not $configReady) {
            Write-Ng ".wslconfigに動画用のmemory=${VideoWslMemoryGiB}GBとswap=${VideoWslSwapGiB}GBが必要です。"
        } else {
            Write-Ng ".wslconfigは設定済みですが、まだWSLへ反映されていません。"
        }
        return $false
    }

    if (-not $configReady) {
        Write-Warn "動画生成の480p構成にはWSLのmemory=${VideoWslMemoryGiB}GBとswap=${VideoWslSwapGiB}GBが必要です。"
        Write-Host "現在: memory=$(if ($memoryText) { $memoryText } else { '未設定' }), swap=$(if ($swapText) { $swapText } else { '未設定' })"
        Write-Host "現在の設定をバックアップし、不足しているメモリとswapの割り当てを増やします。"
        Write-Host "swapの保存先には少なくとも${VideoWslSwapGiB}GBの空きが必要です（既定はC:ドライブ）。"
        if (-not (Confirm-Action ".wslconfigを動画生成用に更新しますか？")) { return $false }
        Set-Wsl2Resources $VideoWslMemoryGiB $VideoWslSwapGiB
        Write-Ok ".wslconfigを更新しました: $WslConfigPath"
    }

    Write-Warn "設定の反映には、Docker Desktopを含むすべてのWSLディストリビューションの停止が必要です。"
    if (-not (Confirm-Action "wsl --shutdownを実行しますか？")) { return $false }
    if (-not (Stop-DockerDesktopForWslShutdown)) {
        Write-Warn "Docker Desktopを終了してからsetup.cmdを再実行してください。"
        return $false
    }
    & wsl.exe --shutdown
    if ($LASTEXITCODE -ne 0) { throw "wsl --shutdown failed with exit code $LASTEXITCODE" }
    Write-Ok "WSLを停止しました。次の起動で設定を確認します。"
    return $true
}
Write-Ok "用途: $(Get-PurposeLabel $Purpose)"

$ready = $true

Write-Section "Windows"
$computer = Get-CimInstance Win32_ComputerSystem
$physicalRamGiB = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
$systemDrive = Get-PSDrive -Name C
$freeGiB = [math]::Round($systemDrive.Free / 1GB, 1)
Write-Ok "物理RAM: $physicalRamGiB GiB"
$freeWarningGiB = if ($Purpose -eq "Video") { 100 } else { 10 }
if ($freeGiB -lt $freeWarningGiB) {
    Write-Warn "C: 空き容量: $freeGiB GiB。モデルとWSL swapを使う場合は不足する可能性があります。"
} else {
    Write-Ok "C: 空き容量: $freeGiB GiB"
}

if ($Purpose -eq "Video") {
    Write-Section "Windows管理ツール"
    if (-not (Ensure-OptimizeVhdSupport)) { $ready = $false }
}

$windowsNvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($windowsNvidia) {
    # Capture the native exit code before running another pipeline. This avoids
    # intermittent false negatives from Select-Object closing a native pipe.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $gpuOutput = & nvidia-smi.exe --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null
        $gpuExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $gpu = $gpuOutput | Select-Object -First 1
    if ($gpuExitCode -eq 0 -and $gpu) {
        Write-Ok "NVIDIA GPU: $gpu"
    } else {
        Write-Ng "NVIDIAドライバーはありますがGPU情報を取得できません。"
        if ($Purpose -eq "Video") { $ready = $false }
    }
} elseif ($Purpose -eq "Video") {
    Show-ManualAction "WindowsでNVIDIAドライバーを確認できません。ドライバーは自動導入しません。" $NvidiaDriverUrl
    $ready = $false
} else {
    Write-Warn "NVIDIA GPUを確認できません。音声作成はCPUで利用できます。"
}

Write-Section "WSL2"
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Show-ManualAction "このWindowsではwsl.exeを利用できません。Windows Update後にWSLの公式手順を確認してください。" $WslInstallUrl
    exit 2
}

$distros = @(Get-WslDistributions)
if (-not $Distribution) {
    $Distribution = $distros | Where-Object { $_ -eq "Ubuntu-24.04" } | Select-Object -First 1
    if (-not $Distribution) {
        $Distribution = $distros | Where-Object { $_ -eq "Ubuntu-26.04" } | Select-Object -First 1
    }
    if (-not $Distribution) {
        $Distribution = $distros | Where-Object { $_ -match '^Ubuntu' } | Select-Object -First 1
    }
}
if (-not $Distribution) {
    Write-Ng "Ubuntuディストリビューションがありません。"
    if ($Check) { exit 2 }
    $Distribution = Install-Ubuntu
    $distros = @(Get-WslDistributions)
}
if ($distros -notcontains $Distribution) {
    Write-Ng "指定されたWSLディストリビューションがありません: $Distribution"
    exit 2
}
Write-Ok "使用するディストリビューション: $Distribution"
if (-not (Ensure-Wsl2 $Distribution)) { exit 2 }

if (-not (Test-UbuntuInitialized $Distribution)) {
    if ($Check) {
        Write-Ng "$Distribution の初回ユーザー作成が完了していません。"
        exit 2
    }
    if (-not (Initialize-Ubuntu $Distribution)) { exit 0 }
}

if (-not (Test-SupportedUbuntu $Distribution)) {
    Write-Ng "$Distribution は対応するUbuntuとして確認できません。Ubuntu 24.04または26.04 LTSを使用してください。"
    exit 2
}

if (-not $Check -and -not (Save-WslUiLanguage $Distribution $UiLanguage)) {
    $ready = $false
}

if ($Purpose -eq "Video") {
    Write-Section "WSLリソース"
    if (-not (Ensure-VideoWslResources $Distribution)) { $ready = $false }
}

Write-Section "Docker Desktop"
$dockerDesktop = Get-DockerDesktopExecutable
$dockerJustInstalled = $false
if (-not $dockerDesktop) {
    if ($Check) {
        Write-Ng "Docker Desktopがありません。通常実行では確認後にWinGetで自動導入できます。"
        $ready = $false
    } else {
        $dockerDesktop = Install-DockerDesktop
        $dockerJustInstalled = [bool]$dockerDesktop
        if (-not $dockerDesktop) { $ready = $false }
    }
}
if ($dockerDesktop) {
    Write-Ok "Docker Desktopを確認しました。"
    if ($script:RestartDockerAfterWslShutdown) {
        Write-Warn "WSLの再起動後にDocker Desktopを起動しています。"
        if (-not (Restart-DockerDesktop $dockerDesktop 120)) { $ready = $false }
    }
    if (-not (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
        Write-Warn "Docker Desktopが起動していません。"
        $startDocker = $dockerJustInstalled
        if (-not $Check -and -not $startDocker) {
            $startDocker = Confirm-Action "Docker Desktopを起動しますか？"
        }
        if ($startDocker) {
            Start-Process $dockerDesktop
            Write-Warn "Docker Desktopの画面が開きます。初回画面が出たら利用条件を確認し、案内に沿ってDashboardまで進んでください。"
            $null = Read-Host "Docker DesktopのDashboardが開いたら、この画面へ戻ってEnterを押します"
            if (Wait-DockerDesktopEngine 30) {
                Write-Ok "Dockerエンジンが起動しました。"
            } else {
                Write-Warn "Dockerエンジンの起動をまだ確認できません。Docker Desktop画面のエラーを確認してください。"
            }
        }
    }
    if (Test-Wsl $Distribution 'timeout 10s docker version >/dev/null 2>&1 && timeout 10s docker compose version >/dev/null 2>&1') {
        Write-Ok "Docker DesktopのWSL統合を確認しました。"
    } else {
        Write-Ng "$Distribution からDockerを利用できません。"
        if (-not $Check -and (Test-DockerDesktopEngine)) {
            Write-Host "Docker Desktopの設定をバックアップし、$Distribution のWSL統合を有効にできます。"
            if ((Confirm-Action "WSL統合を有効にしてDocker Desktopを再起動しますか？") -and
                (Stop-DockerDesktopForWslShutdown) -and
                (Enable-DockerWslIntegration $Distribution)) {
                & wsl.exe --terminate $Distribution *> $null
                if (-not (Restart-DockerDesktop $dockerDesktop 120)) {
                    $ready = $false
                } elseif (Wait-DockerInWsl $Distribution 90) {
                    Write-Ok "Docker DesktopのWSL統合を確認しました。"
                } else {
                    Write-Ng "WSL統合を確認できません。Docker Desktopの Settings > Resources > WSL Integration を確認してください。"
                    $ready = $false
                }
            } else {
                Write-Warn "WSL統合の自動設定を行いませんでした。setup.cmdを再実行すると再確認します。"
                $ready = $false
            }
        } else {
            Write-Host "Docker Desktopの Settings > Resources > WSL Integration で $Distribution を有効にし、Apply & restartを押してください。"
            $ready = $false
        }
    }
}

Write-Section "実行用リポジトリ"
$repoExists = Test-Wsl $Distribution `
    'test -d "$HOME/narration-video-gen/.git" && test -x "$HOME/narration-video-gen/bin/narration-video-gen"'
if ($repoExists) {
    Write-Ok "WSL側: ~/$RepositoryDirectory"
} elseif ($Check) {
    Write-Ng "WSL側の ~/$RepositoryDirectory にリポジトリがありません。"
    $ready = $false
} else {
    if (Test-Wsl $Distribution 'test -e "$HOME/narration-video-gen"') {
        Write-Ng "~/$RepositoryDirectory は存在しますが、この製品のGitリポジトリではありません。上書きしません。"
        exit 2
    }
    if (-not (Test-Wsl $Distribution 'command -v git >/dev/null 2>&1')) {
        Write-Warn "UbuntuにGitがありません。"
        if (-not (Confirm-Action "UbuntuへGitとCA証明書を導入しますか？")) { exit 2 }
        Invoke-Wsl $Distribution `
            'sudo apt-get update && sudo apt-get install -y git ca-certificates'
        if ($script:WslExitCode -ne 0) {
            throw "Git installation failed with exit code $script:WslExitCode"
        }
    }
    if (-not (Confirm-Action "リポジトリをWSL側の ~/$RepositoryDirectory へ配置しますか？")) { exit 2 }
    $cloneCommand = 'git clone "{0}" "$HOME/{1}"' -f $RepositoryUrl, $RepositoryDirectory
    Invoke-Wsl $Distribution $cloneCommand
    if ($script:WslExitCode -ne 0) {
        throw "git clone failed with exit code $script:WslExitCode"
    }
    Write-Ok "WSL側へ配置しました: ~/$RepositoryDirectory"
    $repoExists = $true
}

if ($repoExists -and $Purpose -eq "Video") {
    Write-Section "共通診断"
    Invoke-Wsl $Distribution `
        'cd "$HOME/narration-video-gen" && scripts/setup-linux.sh --check'
    if ($script:WslExitCode -ne 0) { $ready = $false }
} elseif ($repoExists) {
    Write-Ok "音声作成用リポジトリを確認しました。"
}

if ($Purpose -eq "Video") {
    Write-Section "動画生成用GPU"
    if (-not (Test-Wsl $Distribution 'nvidia-smi -L >/dev/null 2>&1')) {
        Write-Ng "WSLからNVIDIA GPUを確認できません。WindowsドライバーとWSL2を確認してください。"
        $ready = $false
    } elseif (-not (Test-Wsl $Distribution `
        "docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia")) {
        Write-Ng "DockerにNVIDIA runtimeがありません。Docker DesktopのWSL統合とGPUサポートを確認してください。"
        $ready = $false
    } else {
        Write-Ok "WSLとDockerからNVIDIA runtimeを確認しました。"
        if (-not $Check -and
            (Confirm-Action "GPUテスト用イメージを取得し、DockerからGPUを確認しますか？")) {
            Invoke-Wsl $Distribution `
                'cd "$HOME/narration-video-gen" && scripts/setup-linux.sh --test-gpu --yes'
            if ($script:WslExitCode -ne 0) {
                $ready = $false
            } else {
                Write-Ok "コンテナからGPUを確認しました。"
            }
        }
    }
}

Write-Section "結果"
if (-not $ready) {
    Write-Ng "準備が完了していません。上の項目を直した後、同じsetup.cmdを再実行してください。"
    exit 2
}

Write-Ok "Windows + WSL2の基本準備が完了しました。"
if (-not $Check) {
    Ensure-DesktopShortcut
}
if ($Purpose -eq "Tts") {
    Write-Host "音声作成: wsl.exe -d $Distribution -- bash -lc 'cd ~/$RepositoryDirectory && ./bin/narration-video-gen tts'"
    if (-not $Check -and (Confirm-Action "音声作成ページを起動しますか？")) {
        Start-TtsWebUi $Distribution
    }
} else {
    $progressColumns = 100
    try {
        if ([Console]::WindowWidth -ge 40) { $progressColumns = [Console]::WindowWidth }
    } catch {
        # A redirected setup has no console width; the downloader uses this fallback.
    }
    Write-Host "動画の準備: wsl.exe -d $Distribution -- bash -lc 'cd ~/$RepositoryDirectory && COLUMNS=$progressColumns NVG_DOWNLOAD_PROGRESS=inline ./bin/narration-video-gen plan'"
    Write-Host "動画の生成: wsl.exe -d $Distribution -- bash -lc 'cd ~/$RepositoryDirectory && ./bin/narration-video-gen run'"
    if (-not $Check) {
        Show-VideoMenu $Distribution $progressColumns
    }
}
