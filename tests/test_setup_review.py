"""Regression checks for setup helpers; no host configuration is changed.

Run with Python in a disposable container. To run the PowerShell checks, pass
--powershell-script and execute the emitted script in a PowerShell container
with this repository mounted at /work.
"""
import os
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def linux_function(name):
    source = (ROOT / "scripts/setup-linux.sh").read_text()
    start = source.index(name + "() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


class LinuxSetupTests(unittest.TestCase):
    def test_swap_removal_uses_integer_bytes_above_two_gib(self):
        for available_kib, expected_code in ((16 * 1024**2, 0), (2 * 1024**2, 1)):
            with self.subTest(available_kib=available_kib), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                swapfile = root / "swapfile"
                swapfile.touch(mode=0o600)
                meminfo = root / "meminfo"
                meminfo.write_text("MemAvailable: %d kB\n" % available_kib)
                code = "\n".join([
                    "set -euo pipefail",
                    'managed_swapfile="$1"; meminfo_path="$2"',
                    'die_ui() { echo "$2" >&2; exit 1; }',
                    'say() { echo "$2"; }',
                    'sudo_run() { echo "operation:$*"; }',
                    'stat() { if [ "$1" = "-c" ] && [ "$2" = "%u" ] && [ "$3" = "$managed_swapfile" ]; then echo 0; else command stat "$@"; fi; }',
                    'fstab_has_managed_swap() { return 0; }',
                    'managed_swap_active() { return 0; }',
                    'remove_managed_swap_fstab() { echo operation:fstab; }',
                    'swapon() { printf "%s 4294967296\\n" "$managed_swapfile"; }',
                    linux_function("managed_swap_used_bytes"),
                    linux_function("remove_managed_swap"),
                    "remove_managed_swap",
                ])
                result = subprocess.run(["bash", "-c", code, "test", str(swapfile), str(meminfo)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertNotIn("integer expression expected", result.stderr)
                if expected_code == 0:
                    self.assertIn("operation:swapoff", result.stdout)
                    self.assertIn("operation:fstab", result.stdout)
                else:
                    self.assertNotIn("operation:", result.stdout)


class DownloadHelperTests(unittest.TestCase):
    def test_verification_checks_only_the_planned_files(self):
        for content, expected in ((b"good", 0), (b"oops", 1)):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for folder in ("scripts", "bin", "models"):
                    (root / folder).mkdir()
                (root / "src").symlink_to(ROOT / "src", target_is_directory=True)
                script = root / "scripts/download-models.sh"
                shutil.copy2(ROOT / "scripts/download-models.sh", script)
                (root / "models/chosen.bin").write_bytes(content)
                plan = {"downloads": {"models": [{"id": "chosen", "path": "chosen.bin", "bytes": 4,
                         "sha256": hashlib.sha256(b"good").hexdigest(), "present": True}]}}
                cli = root / "bin/narration-video-gen"
                cli.write_text("#!/usr/bin/env python3\nimport sys\n"
                               "if 'plan' in sys.argv: print(%r)\n"
                               "else: sys.exit('unrelated model is corrupt')\n" % json.dumps(plan))
                cli.chmod(0o755)
                result = subprocess.run([str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("unrelated model", result.stdout + result.stderr)
                if expected:
                    self.assertIn("chosen.bin", result.stderr)
                    self.assertIn("run plan again", result.stderr)
                else:
                    self.assertIn("[verified] chosen.bin", result.stdout)

    def run_helper(self, *arguments, cli_output="", cli_status=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            script = root / "scripts/download-models.sh"
            shutil.copy2(ROOT / "scripts/download-models.sh", script)
            cli = root / "bin/narration-video-gen"
            cli.write_text("#!/usr/bin/env python3\nimport sys\nprint(%r)\nsys.exit(%d)\n"
                           % (cli_output, cli_status))
            cli.chmod(0o755)
            result = subprocess.run([str(script), *arguments], capture_output=True, text=True)
            self.assertFalse((root / "models").exists())
            return result

    def test_missing_option_values_are_actionable(self):
        for option in ("--profile", "--profile-dir"):
            for tail in ((), ("--profile", "anything"), ("",)):
                with self.subTest(option=option, tail=tail):
                    result = self.run_helper(option, *tail)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("requires a value", result.stderr)
                    self.assertNotIn("unbound variable", result.stderr)

    def test_failed_plan_is_not_reported_as_a_json_traceback(self):
        result = self.run_helper(cli_output='{"best": null}', cli_status=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn("narration-video-gen plan", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        result = self.run_helper(cli_output='{"error": "unknown profile example"}', cli_status=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown profile example", result.stderr)

    def test_help_needs_no_machine_or_models(self):
        result = self.run_helper("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--profile", result.stdout)


class MonitorTests(unittest.TestCase):
    def test_restarting_monitor_preserves_previous_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            (root / "results/run-1").mkdir(parents=True)
            script = root / "scripts/monitor.sh"
            shutil.copy2(ROOT / "scripts/monitor.sh", script)
            csv = root / "results/run-1/metrics.csv"
            previous = "timestamp,vram_used_mib,vram_total_mib,gpu_util_pct,ram_used_gib,ram_available_gib,swap_used_gib\nprevious,100,16000,5,1,10,0\n"
            csv.write_text(previous)
            for name, command in (("nvidia-smi", "echo 100,16000,5"), ("sleep", "exit 1")):
                helper = root / "bin" / name
                helper.write_text("#!/bin/sh\n" + command + "\n")
                helper.chmod(0o755)
            env = dict(os.environ, PATH=str(root / "bin") + ":" + os.environ["PATH"])
            subprocess.run([str(script), "run-1"], env=env, capture_output=True, text=True)
            self.assertTrue(csv.read_text().startswith(previous))
            self.assertEqual(len(csv.read_text().splitlines()), 3)

    def test_invalid_monitor_arguments_do_not_create_outputs(self):
        for arguments, interval in ((("../../outside",), "10"), (("run-1",), "0"), (("run-1",), "oops")):
            with self.subTest(arguments=arguments, interval=interval), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "scripts").mkdir()
                script = root / "scripts/monitor.sh"
                shutil.copy2(ROOT / "scripts/monitor.sh", script)
                result = subprocess.run([str(script), *arguments],
                                        env=dict(os.environ, INTERVAL=interval),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((root / "results").exists())


POWERSHELL_TESTS = r'''
$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    "/work/scripts/setup-windows.ps1", [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$functions = $ast.FindAll({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)
foreach ($definition in $functions) {
    Invoke-Expression $definition.Extent.Text
}
function Assert($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

$root = Join-Path ([IO.Path]::GetTempPath()) ("nvg-setup-test-" + [Guid]::NewGuid())
$null = New-Item -ItemType Directory $root
try {
    $WslConfigPath = Join-Path $root ".wslconfig"
    Set-Wsl2Resources 20 32
    Assert ((Get-Wsl2Setting "swap") -eq "32GB") "Fresh setup did not use 32 GiB"
    Set-Content $WslConfigPath "[wsl2]`nmemory=20GB`nswap=64GB"
    Set-Wsl2Resources 20 32
    Assert ((Get-Wsl2Setting "swap") -eq "64GB") "Existing larger swap was reduced"
    Set-Content $WslConfigPath "[wsl2]`nmemory=48GB`nswap=32GB`nprocessors=8`n[experimental]`nautoMemoryReclaim=gradual"
    Set-Wsl2Resources 20 64
    Assert ((Get-Wsl2Setting "memory") -eq "48GB") "Existing larger memory allocation was reduced"
    Assert ((Get-Wsl2Setting "swap") -eq "64GB") "Swap was not increased"
    $content = Get-Content $WslConfigPath -Raw
    Assert ($content -match "processors=8" -and $content -match "autoMemoryReclaim=gradual") "Unrelated WSL settings were lost"

    Set-Content $WslConfigPath "[wsl2]`nswap=96GB`n[experimental]`nautoMemoryReclaim=gradual`n[wsl2]`nprocessors=8"
    Set-Wsl2Resources 20 64
    $content = Get-Content $WslConfigPath -Raw
    Assert ((Get-Wsl2Setting "swap") -eq "96GB") "Existing larger swap allocation was reduced"
    Assert (([regex]::Matches($content, '(?m)^memory=')).Count -eq 1) "Missing memory setting was written more than once"

    $repository = Join-Path $root "repository"
    $repositoryScripts = Join-Path $repository "scripts"
    $null = New-Item -ItemType Directory -Path $repositoryScripts
    Set-Content (Join-Path $repository "setup.cmd") "new setup launcher"
    Set-Content (Join-Path $repository "cleanup.cmd") "new cleanup launcher"
    Set-Content (Join-Path $repositoryScripts "setup-windows.ps1") "new setup script"
    Set-Content (Join-Path $repositoryScripts "cleanup-windows.ps1") "new cleanup script"
    $SetupStateDirectory = Join-Path $root "installed"
    $InstalledLauncherPath = Join-Path $SetupStateDirectory "setup.cmd"
    $InstalledSetupScriptPath = Join-Path $SetupStateDirectory "scripts/setup-windows.ps1"
    $InstalledCleanupLauncherPath = Join-Path $SetupStateDirectory "cleanup.cmd"
    $InstalledCleanupScriptPath = Join-Path $SetupStateDirectory "scripts/cleanup-windows.ps1"
    $DesktopShortcutPath = Join-Path $SetupStateDirectory "Narration Video Gen.lnk"
    function Get-WslRepositoryWindowsPath { return $repository }
    $script:shortcutWritten = $false
    function Write-DesktopShortcut { $script:shortcutWritten = $true }
    Assert (Install-WindowsLaunchFilesFromRepository "Ubuntu-24.04") `
        "The WSL checkout was not copied to the Windows launcher directory"
    Assert ((Get-Content $InstalledSetupScriptPath -Raw) -match "new setup script") `
        "The installed setup script was not refreshed"
    Assert ($script:shortcutWritten) "The desktop shortcut was not refreshed"

    $RepositoryDirectory = "narration-video-gen"
    $RepositoryUrl = "https://github.com/vonvonhero/narration-video-gen.git"
    $script:updateChecks = [Collections.Generic.Queue[bool]]::new()
    foreach ($value in @($true, $true, $true, $true, $true)) { $script:updateChecks.Enqueue($value) }
    $script:updateCommands = @()
    function Confirm-Action { return $true }
    function Test-Wsl { return $script:updateChecks.Dequeue() }
    function Invoke-Wsl {
        param([string]$Distro, [string]$Command)
        $script:updateCommands += $Command
        $script:WslExitCode = 0
    }
    function Install-WindowsLaunchFilesFromRepository { return $true }
    Assert (Update-RepositoryAndWindowsLauncher "Ubuntu-24.04") `
        "A clean official main checkout was not updated"
    Assert (($script:updateCommands -join "`n") -match "fetch --prune origin" -and
            ($script:updateCommands -join "`n") -match "merge --ff-only origin/main") `
        "The updater did not use a fetch plus fast-forward merge"
    $script:updateChecks = [Collections.Generic.Queue[bool]]::new()
    foreach ($value in @($true, $true, $false)) { $script:updateChecks.Enqueue($value) }
    $script:updateCommands = @()
    Assert (-not (Update-RepositoryAndWindowsLauncher "Ubuntu-24.04")) `
        "A checkout with local changes was updated"
    Assert ($script:updateCommands.Count -eq 0) `
        "The updater contacted the remote for a dirty checkout"

    function wsl.exe {
        param([Parameter(ValueFromRemainingArguments=$true)]$Arguments)
        $global:LASTEXITCODE = 0
        '{"profile":{"id":"windows-wan22-720p-vram16"},"recipe":{"resolution":[1280,720]},"host_setup":{"wsl_ram_gib":16,"wsl_swap_gib":48,"current_ram_gib":19.5,"current_swap_gib":32}}'
    }
    $request = Get-PendingPlanWslSetup "Ubuntu-24.04"
    Assert ($request.ProfileId -eq "windows-wan22-720p-vram16" -and
            $request.Resolution -eq 720 -and $request.SwapGiB -eq 48) `
        "The selected 720p setup request was not parsed"
    Set-Content $WslConfigPath "[wsl2]`nmemory=20GB`nswap=32GB"
    function Confirm-Action { return $false }
    Assert (-not (Apply-PendingPlanWslSetup "Ubuntu-24.04" $request)) `
        "A declined 720p upgrade was applied"
    Assert ((Get-Wsl2Setting "swap") -eq "32GB") "Declining changed the WSL swap"
    function Confirm-Action { return $true }
    function Stop-DockerDesktopForWslShutdown { return $true }
    function Get-DockerDesktopExecutable { return $null }
    Assert (Apply-PendingPlanWslSetup "Ubuntu-24.04" $request) `
        "An approved 720p upgrade failed"
    Assert ((Get-Wsl2Setting "swap") -eq "48GB") "The 720p upgrade did not set 48 GiB swap"

    $script:answers = [Collections.Generic.Queue[string]]::new()
    foreach ($answer in @("invalid", "", " 1 ")) { $script:answers.Enqueue($answer) }
    function Read-Host { param($Prompt) return $script:answers.Dequeue() }
    Assert ((Read-SetupPurpose) -eq "Tts") "Invalid purpose input silently selected video"
    $script:answers.Enqueue("0")
    Assert ($null -eq (Read-SetupPurpose)) "Purpose menu exit did not cancel"

    function wsl.exe {
        param([Parameter(ValueFromRemainingArguments=$true)]$Arguments)
        $global:LASTEXITCODE = 0
        $script:wslCommands += ($Arguments -join " ")
        if ($Arguments[0] -eq "--set-version") { $script:wslVersion = 2 }
        "  NAME                   STATE           VERSION"
        ("* Ubuntu-24.04           Stopped         $script:wslVersion".ToCharArray() -join "`0")
        "  Ubuntu-24X04           Running         1"
    }
    $script:wslVersion = 1
    $script:wslCommands = @()
    Assert ((Get-WslDistributionVersion "Ubuntu-24.04") -eq 1) "WSL1 was not detected"
    Assert ($null -eq (Get-WslDistributionVersion "Missing")) "Unknown distro was assigned a version"
    $Check = $true
    Assert (-not (Ensure-Wsl2 "Ubuntu-24.04")) "Read-only check accepted WSL1"
    Assert (-not ($script:wslCommands -match '--set-version')) "Read-only check converted WSL"
    $Check = $false
    function Confirm-Action { return $false }
    Assert (-not (Ensure-Wsl2 "Ubuntu-24.04")) "Declined conversion was accepted"
    function Confirm-Action { return $true }
    Assert (Ensure-Wsl2 "Ubuntu-24.04") "Approved WSL conversion failed"
    Assert ($script:wslCommands -contains '--set-version Ubuntu-24.04 2') "Conversion was not invoked"

    Write-Host "PASS: PowerShell syntax, resource preservation, Windows update, purpose input, and WSL2 checks"
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force
}
'''


if __name__ == "__main__":
    if sys.argv[1:] == ["--powershell-script"]:
        print(POWERSHELL_TESTS)
    else:
        unittest.main()
