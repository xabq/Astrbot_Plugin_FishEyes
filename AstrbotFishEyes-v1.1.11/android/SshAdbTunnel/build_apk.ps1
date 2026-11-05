# 在 Windows 上直连 Android 构建工具链编译 SshAdbTunnel APK
# 用法:  pwsh -File .\build_apk.ps1
# 依赖: JDK 17 (含 javac/jar/keytool)。Build-Tools 与 Platform 34 缺失时脚本自动下载。

$ErrorActionPreference = 'Stop'

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR

$APP_PACKAGE = 'com.example.sshadbtunnel'
$DL_DIR      = Join-Path $SCRIPT_DIR '.build-dl'
$BUILD_DIR   = Join-Path $SCRIPT_DIR 'build_direct'
$OUT_APK     = Join-Path $SCRIPT_DIR 'SshAdbTunnel-universal.apk'

# ---------- 1. 定位 JDK ----------
function Find-Jdk {
    # D8 8.2.2 在 JDK 25 上会抛 NPE，因此优先选用 JDK 17
    $candidates = @(
        'F:\Java\jdk-17.0.17.10-hotspot',
        'C:\Program Files\Eclipse Adoptium\jdk-17*',
        'C:\Program Files\Java\jdk-17*',
        'C:\Program Files\Microsoft\jdk-17*'
    )
    if ($env:JAVA_HOME) { $candidates += $env:JAVA_HOME }
    $candidates += @('F:\Java\jdk-21', 'F:\Java\LibericaJDK-21')
    foreach ($c in $candidates) {
        $paths = @()
        if (Test-Path $c -PathType Container) {
            $paths += (Resolve-Path $c).Path
        }
        foreach ($g in (Get-ChildItem -Path $c -Directory -ErrorAction SilentlyContinue)) {
            $paths += $g.FullName
        }
        foreach ($p in $paths) {
            if (Test-Path (Join-Path $p 'bin\javac.exe')) { return $p }
        }
    }
    $javac = Get-Command javac.exe -ErrorAction SilentlyContinue
    if ($javac) { return (Split-Path -Parent (Split-Path -Parent $javac.Source)) }
    throw '未找到 JDK (需要 javac.exe)。请安装 JDK 17 并设置 JAVA_HOME。'
}

$JDK = Find-Jdk
$JAVAC    = Join-Path $JDK 'bin\javac.exe'
$JAR      = Join-Path $JDK 'bin\jar.exe'
$JAVA     = Join-Path $JDK 'bin\java.exe'
$KEYTOOL  = Join-Path $JDK 'bin\keytool.exe'
Write-Host "JDK: $JDK"

# ---------- 2. 准备 SDK 组件 ----------
New-Item -ItemType Directory -Force -Path $DL_DIR | Out-Null

function Download-File($Url, $Dest) {
    if ((Test-Path $Dest) -and ((Get-Item $Dest).Length -gt 1000000)) {
        Write-Host "已存在，跳过下载: $Dest"
        return
    }
    Write-Host "下载 $Url ..."
    # 关闭环境变量代理干扰：优先直连，失败再走系统代理
    $py = 'F:\python3.14.6\python.exe'
    if (Test-Path $py) {
        $code = @"
import urllib.request, ssl, os, time
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
for a in range(4):
    try:
        req = urllib.request.Request(r'$Url', headers={'User-Agent':'Mozilla/5.0'})
        with opener.open(req, timeout=180) as r, open(r'$Dest','wb') as f:
            while True:
                c = r.read(1<<20)
                if not c: break
                f.write(c)
        print('OK'); break
    except Exception as e:
        print('retry', a+1, type(e).__name__, e); time.sleep(3)
else:
    raise SystemExit(1)
"@
        $code | & $py -
        if ($LASTEXITCODE -ne 0) { throw "下载失败: $Url" }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
    }
}

$BT_ZIP   = Join-Path $DL_DIR 'build-tools_r34-windows.zip'
$PLAT_ZIP = Join-Path $DL_DIR 'platform-34.zip'

Download-File 'https://dl.google.com/android/repository/build-tools_r34-windows.zip' $BT_ZIP
Download-File 'https://dl.google.com/android/repository/platform-34-ext12_r01.zip'     $PLAT_ZIP

$BT_ROOT   = Join-Path $DL_DIR 'build-tools'
$PLAT_ROOT = Join-Path $DL_DIR 'platform'
if (-not (Get-ChildItem $BT_ROOT -Recurse -Filter aapt2.exe -ErrorAction SilentlyContinue)) {
    Write-Host '解压 Build-Tools ...'
    Expand-Archive -Path $BT_ZIP -DestinationPath $BT_ROOT -Force
}
if (-not (Get-ChildItem $PLAT_ROOT -Recurse -Filter android.jar -ErrorAction SilentlyContinue)) {
    Write-Host '解压 Platform 34 ...'
    Expand-Archive -Path $PLAT_ZIP -DestinationPath $PLAT_ROOT -Force
}

$AAPT2     = (Get-ChildItem $BT_ROOT   -Recurse -Filter aapt2.exe     | Select-Object -First 1).FullName
$ZIPALIGN  = (Get-ChildItem $BT_ROOT   -Recurse -Filter zipalign.exe  | Select-Object -First 1).FullName
$D8_JAR    = (Get-ChildItem $BT_ROOT   -Recurse -Filter d8.jar        | Select-Object -First 1).FullName
$SIGNER    = (Get-ChildItem $BT_ROOT   -Recurse -Filter apksigner.jar | Select-Object -First 1).FullName
$ANDROID_JAR = (Get-ChildItem $PLAT_ROOT -Recurse -Filter android.jar | Select-Object -First 1).FullName

# ---------- 3. 构建 ----------
if (Test-Path $BUILD_DIR) { Remove-Item $BUILD_DIR -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\gen", "$BUILD_DIR\classes", "$BUILD_DIR\dex" | Out-Null

Write-Host '[Step 0/5] 生成带 package 的临时 AndroidManifest.xml ...'
$manifestSrc = Join-Path $SCRIPT_DIR 'app\src\main\AndroidManifest.xml'
$manifestTmp = Join-Path $BUILD_DIR 'AndroidManifest.xml'
(Get-Content $manifestSrc -Raw) -replace '<manifest ', "<manifest package=`"$APP_PACKAGE`" " | Set-Content $manifestTmp -Encoding UTF8

Write-Host '[Step 1/5] aapt2 编译并链接资源 ...'
& $AAPT2 compile --dir (Join-Path $SCRIPT_DIR 'app\src\main\res') -o "$BUILD_DIR\res.zip"
if ($LASTEXITCODE -ne 0) { throw 'aapt2 compile 失败' }
& $AAPT2 link -o "$BUILD_DIR\unaligned.apk" -I $ANDROID_JAR --manifest $manifestTmp --java "$BUILD_DIR\gen" "$BUILD_DIR\res.zip"
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link 失败' }

Write-Host '[Step 2/5] javac 编译 Java 源码 ...'
$sources = @()
$sources += Get-ChildItem (Join-Path $SCRIPT_DIR 'app\src\main\java') -Recurse -Filter *.java | Select-Object -ExpandProperty FullName
$sources += Get-ChildItem "$BUILD_DIR\gen" -Recurse -Filter *.java | Select-Object -ExpandProperty FullName
& $JAVAC --release 11 -encoding UTF-8 -cp $ANDROID_JAR -d "$BUILD_DIR\classes" $sources
if ($LASTEXITCODE -ne 0) { throw 'javac 编译失败' }

& $JAR cf "$BUILD_DIR\app-classes.jar" -C "$BUILD_DIR\classes" .
if ($LASTEXITCODE -ne 0) { throw 'jar 打包失败' }

Write-Host '[Step 3/5] D8 转换 Dalvik DEX ...'
& $JAVA -cp $D8_JAR com.android.tools.r8.D8 --min-api 29 --lib $ANDROID_JAR --output "$BUILD_DIR\dex" "$BUILD_DIR\app-classes.jar"
if ($LASTEXITCODE -ne 0) { throw 'd8 失败' }

Write-Host '[Step 4/5] 合并 classes.dex 并 zipalign ...'
Copy-Item "$BUILD_DIR\unaligned.apk" "$BUILD_DIR\merged.apk" -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open("$BUILD_DIR\merged.apk", 'Update')
[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$BUILD_DIR\dex\classes.dex", 'classes.dex', [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
$zip.Dispose()

& $ZIPALIGN -f -p 4 "$BUILD_DIR\merged.apk" "$BUILD_DIR\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'zipalign 失败' }

Write-Host '[Step 5/5] 签名 APK (v1+v2+v3) ...'
$keystore = Join-Path $DL_DIR 'debug.keystore'
if (-not (Test-Path $keystore)) {
    & $KEYTOOL -genkeypair -keystore $keystore -storepass android -alias androiddebugkey `
        -keypass android -keyalg RSA -keysize 2048 -validity 10000 `
        -dname 'CN=Android Debug,O=Android,C=US' | Out-Null
}
& $JAVA -jar $SIGNER sign --ks $keystore --ks-pass pass:android --key-pass pass:android --out $OUT_APK "$BUILD_DIR\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'apksigner 签名失败' }

& $JAVA -jar $SIGNER verify --print-certs $OUT_APK

Remove-Item $BUILD_DIR -Recurse -Force

$item = Get-Item $OUT_APK
Write-Host ''
Write-Host '=================================================================='
Write-Host "APK 编译成功: $($item.FullName)"
Write-Host "大小: $([math]::Round($item.Length / 1KB, 1)) KB"
Write-Host '=================================================================='
