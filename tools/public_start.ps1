param([string]$BlenderPath = '')
$ErrorActionPreference = 'Stop'
$publicEditorRoot = Split-Path -Parent $PSScriptRoot
if (-not $BlenderPath) {
    $publicBlenderCommand = Get-Command blender -ErrorAction SilentlyContinue
    if ($publicBlenderCommand) { $BlenderPath = $publicBlenderCommand.Source }
}
if (-not $BlenderPath) {
    $publicBlenderInstalls = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match '^Blender' -and $_.InstallLocation } |
        Sort-Object DisplayVersion -Descending
    foreach ($publicBlenderInstall in $publicBlenderInstalls) {
        $publicBlenderCandidate = Join-Path $publicBlenderInstall.InstallLocation 'blender.exe'
        if (Test-Path -LiteralPath $publicBlenderCandidate -PathType Leaf) { $BlenderPath = $publicBlenderCandidate; break }
    }
}
if (-not $BlenderPath -or -not (Test-Path -LiteralPath $BlenderPath -PathType Leaf)) {
    throw 'Install Blender, or run tools/public_start.ps1 -BlenderPath "path\to\blender.exe". Tested with Blender 5.2.2.'
}
Push-Location -LiteralPath $publicEditorRoot
try { & $BlenderPath --python (Join-Path $PSScriptRoot 'public_launcher.py') }
finally { Pop-Location }
