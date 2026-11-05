# 在 Windows 上打包源码发布包
# 用法:
#   pwsh -File .\package_source.ps1              # 按当前版本号打包
#   pwsh -File .\package_source.ps1 -BumpMinor   # 先版本号 +0.1，再打包
#
# 版本号唯一来源是 app/build.gradle 的 versionName，本脚本会同步写入 AndroidManifest.xml
# 并校验两者一致，避免出现两处版本号打架的情况。

param(
    [switch]$BumpMinor,
    [switch]$SkipApkBuild
)

$ErrorActionPreference = 'Stop'

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR

$GRADLE_FILE   = Join-Path $SCRIPT_DIR 'app\build.gradle'
$MANIFEST_FILE = Join-Path $SCRIPT_DIR 'app\src\main\AndroidManifest.xml'

# ---------- 1. 读取版本号 ----------
$gradleText = [System.IO.File]::ReadAllText($GRADLE_FILE, [System.Text.Encoding]::UTF8)

if ($BumpMinor) {
    $m = [regex]::Match($gradleText, 'versionName\s+"(\d+)\.(\d+)\.(\d+)"')
    if (-not $m.Success) { throw 'app/build.gradle 里找不到形如 versionName "1.2.3" 的版本号' }

    $major = [int]$m.Groups[1].Value
    $minor = [int]$m.Groups[2].Value
    $patch = [int]$m.Groups[3].Value

    $minor += 1
    if ($minor -ge 10) { $major += 1; $minor = 0 }
    $newVersion = "$major.$minor.0"

    $gradleText = $gradleText -replace 'versionName\s+"[^"]+"', "versionName `"$newVersion`""

    $codeMatch = [regex]::Match($gradleText, 'versionCode\s+(\d+)')
    if ($codeMatch.Success) {
        $newCode = [int]$codeMatch.Groups[1].Value + 1
        $gradleText = $gradleText -replace 'versionCode\s+\d+', "versionCode $newCode"
    }

    [System.IO.File]::WriteAllText($GRADLE_FILE, $gradleText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "版本号已累加: $($m.Groups[1].Value).$($m.Groups[2].Value).$($m.Groups[3].Value) -> $newVersion"
}

$gradleText = [System.IO.File]::ReadAllText($GRADLE_FILE, [System.Text.Encoding]::UTF8)
$vm = [regex]::Match($gradleText, 'versionName\s+"([^"]+)"')
if (-not $vm.Success) { throw '读取 versionName 失败' }
$VERSION = $vm.Groups[1].Value

$vc = [regex]::Match($gradleText, 'versionCode\s+(\d+)')
$VERSION_CODE = if ($vc.Success) { $vc.Groups[1].Value } else { '0' }

# ---------- 2. 同步并校验 manifest ----------
$manifestText = [System.IO.File]::ReadAllText($MANIFEST_FILE, [System.Text.Encoding]::UTF8)
$manifestText = $manifestText -replace 'android:versionName="[^"]*"', "android:versionName=`"$VERSION`""
$manifestText = $manifestText -replace 'android:versionCode="[^"]*"', "android:versionCode=`"$VERSION_CODE`""
[System.IO.File]::WriteAllText($MANIFEST_FILE, $manifestText, (New-Object System.Text.UTF8Encoding($false)))

$check = [regex]::Match($manifestText, 'android:versionName="([^"]+)"').Groups[1].Value
if ($check -ne $VERSION) { throw "manifest 版本号同步失败: $check != $VERSION" }

Write-Host "当前版本: $VERSION (versionCode $VERSION_CODE)"

# ---------- 3. 构建 APK ----------
$APK = Join-Path $SCRIPT_DIR 'SshAdbTunnel-universal.apk'
if (-not $SkipApkBuild) {
    Write-Host '开始构建 APK ...'
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $SCRIPT_DIR 'build_apk.ps1')
    if (-not (Test-Path $APK)) { throw 'APK 构建失败，未找到输出文件' }
}

# ---------- 4. 收集要打包的文件（白名单，避免把无关目录打进去） ----------
$include = @(
    'app',
    'build.gradle',
    'settings.gradle',
    'gradle.properties',
    'build_universal_apk.sh',
    'build_apk.ps1',
    'package_source.ps1',
    '.github',
    'tools\sshtest',
    'server',
    'CHANGELOG.md',
    'SshAdbTunnel-审查报告.md',
    '长连接与截屏推流说明.md'
)

# 排除规则：构建产物、SDK 缓存、测试输出、测试私钥
$excludePatterns = @(
    '\\build\\',
    '\\build_direct\\',
    '\\\.gradle\\',
    '\\out\\',
    '\\\.build-dl\\',
    '\\keys\\',
    '\\\.idea\\'
)

function Test-Excluded([string]$fullPath) {
    $rel = $fullPath.Substring($SCRIPT_DIR.Length)
    foreach ($p in $excludePatterns) {
        if ($rel -match $p) { return $true }
    }
    return $false
}

$files = New-Object System.Collections.Generic.List[string]
foreach ($item in $include) {
    $full = Join-Path $SCRIPT_DIR $item
    if (-not (Test-Path $full)) { continue }
    if (Test-Path $full -PathType Leaf) {
        $files.Add($full)
    } else {
        Get-ChildItem $full -Recurse -File | ForEach-Object {
            if (-not (Test-Excluded $_.FullName)) { $files.Add($_.FullName) }
        }
    }
}

# APK 一并放入
if (Test-Path $APK) { $files.Add($APK) }

# ---------- 5. 打 zip ----------
$distDir = Join-Path $SCRIPT_DIR 'dist'
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$zipPath = Join-Path $distDir "SshAdbTunnel_Android_Source_v$VERSION.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
try {
    foreach ($f in $files) {
        $rel = $f.Substring($SCRIPT_DIR.Length).TrimStart('\').Replace('\', '/')
        $entryName = "SshAdbTunnel_v$VERSION/$rel"
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip, $f, $entryName, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally {
    $zip.Dispose()
}

# ---------- 6. 摘要 ----------
$size = [math]::Round((Get-Item $zipPath).Length / 1KB, 1)
$hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash

Write-Host ''
Write-Host '=================================================================='
Write-Host "源码包: $zipPath"
Write-Host "版本:   $VERSION (versionCode $VERSION_CODE)"
Write-Host "文件数: $($files.Count)"
Write-Host "大小:   $size KB"
Write-Host "SHA256: $hash"
Write-Host '=================================================================='
