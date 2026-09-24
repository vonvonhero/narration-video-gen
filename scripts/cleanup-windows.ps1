[CmdletBinding()]
param(
    [string]$Distribution,
    [switch]$Check,
    [switch]$Models,
    [switch]$Images,
    [switch]$CompactWsl,
    [switch]$All,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$RepositoryDirectory = "narration-video-gen"
$RepositoryPath = "/home/{0}/$RepositoryDirectory"
$ProjectImageRepositories = @(
    "narration-video-gen-comfy",
    "narration-video-gen-musetalk",
    "narration-video-gen-tts"
)
$ProjectContainerNames = @("narration-video-gen-comfy", "narration-video-gen-tts")
$WslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$ManagedWslMemoryGiB = 20
$ManagedWslSwapGiB = 32
$script:RestartDocker = $false

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {}

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "== $Title" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) { Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[--] $Message" -ForegroundColor Yellow }
function Write-Ng([string]$Message) { Write-Host "[NG] $Message" -ForegroundColor Red }

function Confirm-Action([string]$Message) {
    $answer = Read-Host "$Message [y/N]"
    return $answer -match '^(?i:y|yes)$'
}

function Format-Bytes([long]$Bytes) {
    if ($Bytes -ge 1TB) { return "{0:N2} TiB" -f ($Bytes / 1TB) }
    if ($Bytes -ge 1GB) { return "{0:N2} GiB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N1} MiB" -f ($Bytes / 1MB) }
    return "{0:N0} KiB" -f ($Bytes / 1KB)
}

function Get-WslDistributions {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = & wsl.exe --list --quiet 2>$null
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) { return @() }
    return @(($raw -join "`n") -replace "`0", "" -split "`r?`n" |
        ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Select-Distribution {
    $distros = @(Get-WslDistributions)
    if ($Distribution) {
        if ($distros -notcontains $Distribution) {
            throw "指定されたWSLディストリビューションがありません: $Distribution"
        }
        return $Distribution
    }
    foreach ($name in @("Ubuntu-24.04", "Ubuntu-26.04")) {
        if ($distros -contains $name) { return $name }
    }
    return $distros | Where-Object { $_ -match '^Ubuntu' } | Select-Object -First 1
}

function Get-WslUser([string]$Distro) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = @(& wsl.exe -d $Distro -- id -un 2>$null)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) { return $null }
    return (($raw -join "") -replace "`0", "").Trim()
}

function Get-ModelInventory([string]$Distro, [string]$Repo) {
    $helper = "$Repo/scripts/cleanup-models.py"
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = @(& wsl.exe -d $Distro -- python3 $helper --root $Repo --check 2>$null)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0 -or -not $raw) {
        throw "WSL側のモデル一覧を取得できません。"
    }
    return ($raw -join "`n") | ConvertFrom-Json
}

function Remove-ManagedModels([string]$Distro, [string]$Repo) {
    $helper = "$Repo/scripts/cleanup-models.py"
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = @(& wsl.exe -d $Distro -- python3 $helper --root $Repo --delete)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) {
        $detail = ($raw -join "`n").Trim()
        throw "モデルを削除できませんでした。$detail"
    }
    return ($raw -join "`n") | ConvertFrom-Json
}

function Repair-ManagedModelPermissions([string]$Distro, [string]$Repo, [string]$User) {
    if ($User -notmatch '^[a-z_][a-z0-9_-]*\$?$') {
        throw "安全でないWSLユーザー名のため、モデル権限を変更しません。"
    }
    $expectedRepo = "/home/$User/$RepositoryDirectory"
    if ($Repo -cne $expectedRepo) {
        throw "モデル権限の変更先が実行用リポジトリではありません。"
    }
    $target = "$expectedRepo/models/irodori-tts-v4.1-small"
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & wsl.exe -d $Distro -- test -e $target 2>$null
        if ($LASTEXITCODE -ne 0) { return }

        # TTS downloads run in a container and can leave root-owned cache files.
        # find stays on this filesystem, while chown -h never follows symlinks.
        & wsl.exe -d $Distro -u root -- find $target -xdev `
            -exec chown -h -- "${User}:${User}" "{}" "+" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "TTSモデルの所有権を修復できませんでした。" }
        & wsl.exe -d $Distro -u root -- find $target -xdev -type d `
            -exec chmod u+rwx -- "{}" "+" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "TTSモデルのディレクトリ権限を修復できませんでした。" }
        & wsl.exe -d $Distro -u root -- find $target -xdev -type f `
            -exec chmod u+rw -- "{}" "+" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "TTSモデルのファイル権限を修復できませんでした。" }
    } finally { $ErrorActionPreference = $previous }
}

function Invoke-DockerJson([string]$Distro, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = @(& wsl.exe -d $Distro -- docker @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) { return $null }
    $rows = @()
    foreach ($line in $raw) {
        if ($line) { $rows += ($line | ConvertFrom-Json) }
    }
    return @($rows)
}

function Test-DockerEngine([string]$Distro) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & wsl.exe -d $Distro -- docker info --format "{{json .ServerVersion}}" `
            2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } finally { $ErrorActionPreference = $previous }
}

function Get-ProjectImages([string]$Distro) {
    $rows = Invoke-DockerJson $Distro @("image", "ls", "--format", "{{json .}}")
    if ($null -eq $rows) { return $null }
    return @($rows | Where-Object { $ProjectImageRepositories -contains $_.Repository } |
        ForEach-Object {
            $reference = if ($_.Tag -and $_.Tag -ne "<none>") {
                if ($_.Tag -notmatch '^[A-Za-z0-9_.-]+$') { throw "不正なDockerタグです。" }
                "$($_.Repository):$($_.Tag)"
            } else { $_.ID }
            [pscustomobject]@{ Reference = $reference; Size = $_.Size; Id = $_.ID }
        })
}

function Get-ProjectContainers([string]$Distro) {
    $rows = Invoke-DockerJson $Distro @("container", "ls", "-a", "--format", "{{json .}}")
    if ($null -eq $rows) { return $null }
    return @($rows | Where-Object {
        $ProjectContainerNames -contains $_.Names -or $_.Names -match '^nvg-musetalk-[0-9a-f]{16}$'
    })
}

function Get-DockerBuildCacheUsage([string]$Distro) {
    $rows = Invoke-DockerJson $Distro @("system", "df", "--format", "{{json .}}")
    if ($null -eq $rows) { return $null }
    return $rows | Where-Object { $_.Type -eq "Build Cache" } | Select-Object -First 1
}

function Invoke-Docker([string]$Distro, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & wsl.exe -d $Distro -- docker @Arguments | Out-Host
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) { throw "docker $($Arguments -join ' ') が失敗しました。" }
}

function Get-DistroVhd([string]$Distro) {
    $keys = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' `
        -ErrorAction SilentlyContinue
    foreach ($key in $keys) {
        $item = Get-ItemProperty $key.PSPath
        if ($item.DistributionName -ne $Distro) { continue }
        $base = [Environment]::ExpandEnvironmentVariables(
            ([string]$item.BasePath -replace '^\\\\\?\\', ''))
        $candidate = [IO.Path]::GetFullPath((Join-Path $base "ext4.vhdx"))
        $baseFull = [IO.Path]::GetFullPath($base).TrimEnd('\') + '\'
        if (-not $candidate.StartsWith($baseFull, [StringComparison]::OrdinalIgnoreCase)) {
            throw "WSL VHDXのパスがディストリビューション領域外です。"
        }
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Get-DockerDataVhds {
    $root = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Docker\wsl"))
    $candidates = @(
        (Join-Path $root "disk\docker_data.vhdx"),
        (Join-Path $root "data\ext4.vhdx")
    )
    $result = @()
    foreach ($candidate in $candidates) {
        $full = [IO.Path]::GetFullPath($candidate)
        if (-not $full.StartsWith($root.TrimEnd('\') + '\',
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "Docker VHDXのパスがDocker領域外です。"
        }
        if ((Test-Path -LiteralPath $full) -and $result -notcontains $full) {
            $result += $full
        }
    }
    return @($result)
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

function Get-WslResourceResetPlan {
    if (-not (Test-Path -LiteralPath $WslConfigPath)) {
        return [pscustomobject]@{ MemoryManaged = $false; SwapManaged = $false; Message = "既にWSL標準です" }
    }
    $lines = @(Get-Content -LiteralPath $WslConfigPath)
    $inWsl2 = $false
    $memory = $null
    $swap = $null
    foreach ($line in $lines) {
        if ($line -match '^\s*\[([^]]+)\]') {
            $inWsl2 = $matches[1] -ieq 'wsl2'
            continue
        }
        if ($inWsl2 -and $line -match '^\s*memory\s*=\s*([^#;]+)') { $memory = $matches[1].Trim() }
        if ($inWsl2 -and $line -match '^\s*swap\s*=\s*([^#;]+)') { $swap = $matches[1].Trim() }
    }
    $memoryGiB = Convert-WslSizeToGiB $memory
    $swapGiB = Convert-WslSizeToGiB $swap
    $memoryManaged = $null -ne $memoryGiB -and [math]::Abs($memoryGiB - $ManagedWslMemoryGiB) -lt 0.001
    $swapManaged = $null -ne $swapGiB -and [math]::Abs($swapGiB - $ManagedWslSwapGiB) -lt 0.001
    $reset = @()
    $keep = @()
    if ($memoryManaged) { $reset += "memory=$memory" } elseif ($memory) { $keep += "memory=$memory" }
    if ($swapManaged) { $reset += "swap=$swap" } elseif ($swap) { $keep += "swap=$swap" }
    $parts = @()
    if ($reset.Count -gt 0) { $parts += (($reset -join ", ") + " を外してWSL標準へ戻します") }
    if ($keep.Count -gt 0) { $parts += (($keep -join ", ") + " は利用者設定として保持します") }
    if ($parts.Count -eq 0) { $parts += "既にWSL標準です" }
    return [pscustomobject]@{
        MemoryManaged = $memoryManaged
        SwapManaged = $swapManaged
        Message = $parts -join "。"
    }
}

function Reset-ManagedWslResources {
    $plan = Get-WslResourceResetPlan
    if (-not ($plan.MemoryManaged -or $plan.SwapManaged)) { Write-Ok $plan.Message; return }
    $lines = @(Get-Content -LiteralPath $WslConfigPath)
    $output = New-Object System.Collections.Generic.List[string]
    $inWsl2 = $false
    $memoryRemoved = $false
    $swapRemoved = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*\[([^]]+)\]') {
            $inWsl2 = $matches[1] -ieq 'wsl2'
        }
        if ($inWsl2 -and $plan.MemoryManaged -and -not $memoryRemoved -and
                $line -match '^\s*memory\s*=') {
            $memoryRemoved = $true
            continue
        }
        if ($inWsl2 -and $plan.SwapManaged -and -not $swapRemoved -and
                $line -match '^\s*swap\s*=') {
            $swapRemoved = $true
            continue
        }
        $output.Add($line)
    }
    if (-not ($memoryRemoved -or $swapRemoved)) { return }
    $backup = "$WslConfigPath.cleanup-backup-$((Get-Date).ToString('yyyyMMdd-HHmmss-fffffff'))"
    Copy-Item -LiteralPath $WslConfigPath -Destination $backup
    $temporary = "$WslConfigPath.tmp-$PID"
    try {
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temporary,
            (($output -join [Environment]::NewLine).TrimEnd() + [Environment]::NewLine), $encoding)
        Move-Item -LiteralPath $temporary -Destination $WslConfigPath -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    Write-Ok "WSLのメモリ・swap割り当てを標準へ戻しました（バックアップ: $backup）"
}

function Get-DockerDesktopControlExecutable {
    $candidate = Join-Path $env:ProgramFiles "Docker\Docker\DockerCli.exe"
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function Get-DockerDesktopExecutable {
    $candidate = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function Stop-DockerDesktop {
    $running = (Get-Process -Name "com.docker.backend" -ErrorAction SilentlyContinue) -or
               (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)
    if (-not $running) { return $true }
    $control = Get-DockerDesktopControlExecutable
    if (-not $control) { Write-Ng "Docker Desktopを安全に終了できません。"; return $false }
    $script:RestartDocker = $true
    $process = Start-Process $control -ArgumentList "-Shutdown" -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(30000)) { $process.Kill(); return $false }
    $limit = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $limit) {
        if (-not (Get-Process -Name "com.docker.backend" -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Test-OptimizeVhdAvailable {
    try {
        Import-Module Hyper-V -ErrorAction Stop
        return $null -ne (Get-Command Optimize-VHD -ErrorAction SilentlyContinue)
    } catch { return $false }
}

function Invoke-OptimizeVhdCompaction([string[]]$Paths) {
    $scriptPath = Join-Path ([IO.Path]::GetTempPath()) `
        ("nvg-optimize-vhd-{0}.ps1" -f [Guid]::NewGuid())
    try {
        $lines = New-Object System.Collections.Generic.List[string]
        $lines.Add('$ErrorActionPreference = "Stop"')
        $lines.Add('Import-Module Hyper-V -ErrorAction Stop')
        foreach ($path in $Paths) {
            if ($path.Contains("'") -or $path.Contains('"')) {
                throw "VHDXパスに引用符が含まれています。"
            }
            $lines.Add("Optimize-VHD -Path '$path' -Mode Full -ErrorAction Stop")
        }
        [IO.File]::WriteAllLines($scriptPath, $lines,
            (New-Object System.Text.UTF8Encoding($true)))
        $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
        $process = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden `
            -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "Optimize-VHDによるVHDX縮小が終了コード$($process.ExitCode)で失敗しました。"
        }
    } finally {
        Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-VhdCompaction([string[]]$Paths) {
    if (-not (Test-OptimizeVhdAvailable)) {
        throw "Optimize-VHDを利用できません。先にsetup.cmdを動画用途で実行してください。"
    }
    Invoke-OptimizeVhdCompaction $Paths
}

function Compact-WslDisks([string]$Distro, [string[]]$Paths) {
    if (-not $Paths) { Write-Warn "縮小対象のVHDXがありません。"; return }
    Write-Warn "WSLのメモリ・swap割り当てを標準へ戻し、WSLとDocker Desktopを停止します。保存中のWSL作業もすべて終了します。"
    if (-not (Confirm-Action "WSLリソースを標準へ戻し、未使用領域をtrimしてVHDXを縮小しますか？")) {
        Write-Warn "VHDX縮小を中止しました。"
        return
    }
    if (-not (Test-OptimizeVhdAvailable)) {
        Write-Ng "Optimize-VHDを利用できません。先にsetup.cmdを動画用途で実行してください。"
        return
    }
    Reset-ManagedWslResources
    & wsl.exe -d $Distro -u root -- fstrim -av 2>$null | Out-Host
    $distros = @(Get-WslDistributions)
    if ($distros -contains "docker-desktop") {
        & wsl.exe -d docker-desktop -u root -- fstrim -av 2>$null | Out-Host
    }
    try {
        if (-not (Stop-DockerDesktop)) { throw "Docker Desktopを停止できませんでした。" }
        & wsl.exe --shutdown
        if ($LASTEXITCODE -ne 0) { throw "wsl --shutdownが失敗しました。" }
        $before = @{}
        foreach ($path in $Paths) { $before[$path] = (Get-Item -LiteralPath $path).Length }
        Invoke-VhdCompaction $Paths
        $totalReclaimed = 0L
        foreach ($path in $Paths) {
            $after = (Get-Item -LiteralPath $path).Length
            $reclaimed = [math]::Max(0L, ([long]$before[$path] - [long]$after))
            $totalReclaimed += $reclaimed
            if ($reclaimed -gt 0) { Write-Ok "VHDX: $path" }
            else { Write-Warn "VHDXのファイルサイズは変わりませんでした: $path" }
            Write-Host "  $((Format-Bytes $before[$path])) -> $((Format-Bytes $after))"
        }
        if ($totalReclaimed -gt 0) {
            Write-Ok "VHDXから解放した容量: $((Format-Bytes $totalReclaimed))"
        } else {
            Write-Warn "DiskPartは完了しましたが、VHDXから追加の容量を解放できませんでした。"
        }
    } finally {
        if ($script:RestartDocker) {
            $desktop = Get-DockerDesktopExecutable
            if ($desktop) {
                Start-Process $desktop -WindowStyle Hidden
                Write-Ok "Docker Desktopを再起動しました。"
            } else { Write-Warn "Docker Desktopを手動で起動してください。" }
        }
    }
}

Write-Host "Narration Video Gen - Windows クリーンアップ" -ForegroundColor White
if ($All) { $Models = $true; $Images = $true; $CompactWsl = $true }
if ($Check -and -not ($Models -or $Images -or $CompactWsl)) {
    $Models = $true; $Images = $true; $CompactWsl = $true
}
if (-not $Check -and -not ($Models -or $Images -or $CompactWsl)) {
    Write-Host "  1. ダウンロード済みモデルを削除"
    Write-Host "  2. 製品のDockerイメージを削除"
    Write-Host "  3. モデルとDockerイメージを削除"
    Write-Host "  4. WSLリソースを標準へ戻して仮想ディスクを縮小"
    Write-Host "  5. すべて実行"
    Write-Host "  0. 中止"
    switch ((Read-Host "番号を入力してください").Trim()) {
        "1" { $Models = $true }
        "2" { $Images = $true }
        "3" { $Models = $true; $Images = $true }
        "4" { $CompactWsl = $true }
        "5" { $Models = $true; $Images = $true; $CompactWsl = $true }
        default { Write-Warn "中止しました。"; exit 0 }
    }
}

$Distribution = Select-Distribution
if (-not $Distribution) { Write-Ng "Ubuntuディストリビューションがありません。"; exit 2 }
$user = Get-WslUser $Distribution
if (-not $user -or $user -eq "root") { Write-Ng "Ubuntuの通常ユーザーを確認できません。"; exit 2 }
$repo = $RepositoryPath -f $user
Write-Ok "使用するディストリビューション: $Distribution"

$inventory = $null
if ($Models -or $Images -or $CompactWsl) {
    $inventory = Get-ModelInventory $Distribution $repo
    if ($inventory.active.Count -gt 0) {
        Write-Ng "モデルダウンロードまたは動画生成が実行中です。完了後に再実行してください。"
        foreach ($process in $inventory.active) { Write-Host "  PID $($process.pid): $($process.command)" }
        if (-not $Check) { exit 2 }
    }
}

$dockerAvailable = $false
$projectImages = @()
$projectContainers = @()
$dockerBuildCache = $null
if ($Models -or $Images) {
    $dockerAvailable = Test-DockerEngine $Distribution
    if ($dockerAvailable) {
        $projectContainers = @(Get-ProjectContainers $Distribution |
            Where-Object { $null -ne $_ })
        if ($Images) {
            $projectImages = @(Get-ProjectImages $Distribution)
            $dockerBuildCache = Get-DockerBuildCacheUsage $Distribution
        }
    }
}
$vhds = @()
if ($CompactWsl) {
    $ubuntuVhd = Get-DistroVhd $Distribution
    if ($ubuntuVhd) { $vhds += $ubuntuVhd }
    $vhds += @(Get-DockerDataVhds)
    $vhds = @($vhds | Select-Object -Unique)
}

Write-Section "削除・縮小対象"
if ($Models) {
    Write-Host "モデル: $((Format-Bytes ([long]$inventory.bytes)))"
    foreach ($target in $inventory.targets | Where-Object { $_.exists }) {
        Write-Host "  $($target.path)  $((Format-Bytes ([long]$target.bytes)))"
    }
    foreach ($container in $projectContainers) {
        Write-Host "  先に停止・削除する製品コンテナ: $($container.Names) [$($container.State)]"
    }
}
if ($Images) {
    if (-not $dockerAvailable) {
        Write-Warn "Docker Engineへ接続できないため、イメージを確認できません。"
        if (-not $Check) { exit 2 }
    } elseif ($projectImages.Count -eq 0) { Write-Host "Dockerイメージ: なし" }
    else {
        Write-Host "Dockerイメージ:"
        foreach ($image in $projectImages) { Write-Host "  $($image.Reference)  $($image.Size)" }
    }
    if ($dockerBuildCache -and $dockerBuildCache.Size -ne "0B") {
        Write-Warn "共有Dockerビルドキャッシュ: $($dockerBuildCache.Size)（他プロジェクトと区別できないため保持）"
    }
}
if ($CompactWsl) {
    $resourcePlan = Get-WslResourceResetPlan
    Write-Host "WSLリソース: $($resourcePlan.Message)"
    Write-Host "VHDX縮小対象:"
    foreach ($path in $vhds) {
        Write-Host "  $path  $((Format-Bytes (Get-Item -LiteralPath $path).Length))"
    }
}
if ($Check) { Write-Ok "確認のみ完了しました。何も削除していません。"; exit 0 }

if (($Models -or $Images) -and -not $Yes) {
    if (-not (Confirm-Action "表示したモデル／Dockerイメージを削除しますか？")) {
        Write-Warn "削除を中止しました。"
        if (-not $CompactWsl) { exit 0 }
        $Models = $false; $Images = $false
    }
}

if (($Models -or $Images) -and $dockerAvailable) {
    foreach ($container in $projectContainers) {
        Invoke-Docker $Distribution @("container", "rm", "-f", $container.Names)
    }
}
if ($Models) {
    Repair-ManagedModelPermissions $Distribution $repo $user
    $removed = Remove-ManagedModels $Distribution $repo
    Write-Ok "モデルを削除しました: $((Format-Bytes ([long]$removed.bytes)))"
}
if ($Images -and $projectImages.Count -gt 0) {
    foreach ($image in $projectImages) {
        Invoke-Docker $Distribution @("image", "rm", $image.Reference)
    }
    Write-Ok "製品のDockerイメージを削除しました。"
}
if ($CompactWsl) { Compact-WslDisks $Distribution $vhds }

Write-Ok "クリーンアップが完了しました。入力・生成物・設定・キャラクターは保持されています。"
