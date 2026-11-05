#!/usr/bin/env bash
set -euo pipefail

# 自动定位脚本所在目录（支持包含空格和括号的路径）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_PACKAGE="com.example.sshadbtunnel"

echo "=================================================================="
echo "=== 正在编译通用架构 APK (Universal APK: 全架构兼容) ==="
echo "=== 工程目录: $SCRIPT_DIR"
echo "=================================================================="

CACHE_DIR="$HOME/.cache/sshadbtunnel-android-sdk"
mkdir -p "$CACHE_DIR"

for cmd in unzip curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "❌ 缺少命令: $cmd，请先执行: sudo apt update && sudo apt install -y curl unzip"
    exit 1
  fi
done

# 若系统仅安装了 JRE (缺少 javac 或 jar)，自动下载便携版 Temurin JDK 17
if ! command -v javac >/dev/null 2>&1 || ! command -v jar >/dev/null 2>&1; then
  PORTABLE_JDK_JAVAC="$(find "$CACHE_DIR/jdk17" -name "javac" -type f 2>/dev/null | head -n 1 || true)"
  if [ -z "$PORTABLE_JDK_JAVAC" ]; then
    echo "⬇️ 检测到系统缺少 javac 编译器，正在下载便携版 OpenJDK 17 (~180MB，仅首次下载)..."
    mkdir -p "$CACHE_DIR/jdk17"
    curl -fL "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse" -o "$CACHE_DIR/jdk17.tar.gz"
    tar -xzf "$CACHE_DIR/jdk17.tar.gz" -C "$CACHE_DIR/jdk17"
    rm -f "$CACHE_DIR/jdk17.tar.gz"
    PORTABLE_JDK_JAVAC="$(find "$CACHE_DIR/jdk17" -name "javac" -type f | head -n 1)"
  fi
  JDK_BIN_DIR="$(dirname "$PORTABLE_JDK_JAVAC")"
  export PATH="$JDK_BIN_DIR:$PATH"
fi

BUILD_DIR="$SCRIPT_DIR/build_direct"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/dex"

# 1. Android Build-Tools r34 (aapt2 / d8 / zipalign / apksigner)
AAPT2="$(find "$CACHE_DIR" -name "aapt2" -type f 2>/dev/null | head -n 1 || true)"
if [ -z "$AAPT2" ]; then
  echo "⬇️ [1/3] 正在下载 Google 官方 Android Build-Tools 34.0.0 (~55MB，仅首次下载)..."
  curl -fL "https://dl.google.com/android/repository/build-tools_r34-linux.zip" -o "$CACHE_DIR/build-tools.zip"
  unzip -q -o "$CACHE_DIR/build-tools.zip" -d "$CACHE_DIR/build-tools"
  rm -f "$CACHE_DIR/build-tools.zip"
  AAPT2="$(find "$CACHE_DIR/build-tools" -name "aapt2" -type f | head -n 1)"
fi

ZIPALIGN="$(find "$CACHE_DIR/build-tools" -name "zipalign" -type f | head -n 1)"
D8_JAR="$(find "$CACHE_DIR/build-tools" -name "d8.jar" -type f | head -n 1)"
APKSIGNER_JAR="$(find "$CACHE_DIR/build-tools" -name "apksigner.jar" -type f | head -n 1)"
chmod +x "$AAPT2" "$ZIPALIGN"

# 2. Android Platform 34 (android.jar)
ANDROID_JAR="$(find "$CACHE_DIR" -name "android.jar" -type f 2>/dev/null | head -n 1 || true)"
if [ -z "$ANDROID_JAR" ]; then
  echo "⬇️ [2/3] 正在下载 Google 官方 Android SDK Platform 34 (~60MB，仅首次下载)..."
  curl -fL "https://dl.google.com/android/repository/platform-34-ext12_r01.zip" -o "$CACHE_DIR/platform.zip"
  unzip -q -o "$CACHE_DIR/platform.zip" -d "$CACHE_DIR/platform"
  rm -f "$CACHE_DIR/platform.zip"
  ANDROID_JAR="$(find "$CACHE_DIR/platform" -name "android.jar" -type f | head -n 1)"
fi

# 3. 注入 package 属性生成临时清单
#    AGP 8 起 manifest 不允许再写 package，因此源码清单不带 package，
#    由本脚本在构建目录里生成带 package 的副本供 aapt2 使用。
echo "🔨 [Step 0/5] 生成带 package 的临时 AndroidManifest.xml ..."
sed "s|<manifest |<manifest package=\"$APP_PACKAGE\" |" \
  "$SCRIPT_DIR/app/src/main/AndroidManifest.xml" > "$BUILD_DIR/AndroidManifest.xml"

echo "🔨 [Step 1/5] 正在使用 aapt2 编译并链接资源..."
"$AAPT2" compile --dir "$SCRIPT_DIR/app/src/main/res" -o "$BUILD_DIR/res.zip"
"$AAPT2" link \
  -o "$BUILD_DIR/unaligned.apk" \
  -I "$ANDROID_JAR" \
  --manifest "$BUILD_DIR/AndroidManifest.xml" \
  --java "$BUILD_DIR/gen" \
  "$BUILD_DIR/res.zip"

echo "🔨 [Step 2/5] 正在使用 javac 编译 Java 源码..."
JAVA_SOURCES=()
while IFS= read -r -d '' file; do
  JAVA_SOURCES+=("$file")
done < <(find "$SCRIPT_DIR/app/src/main/java" "$BUILD_DIR/gen" -name "*.java" -print0)

javac --release 11 -encoding UTF-8 \
  -cp "$ANDROID_JAR" \
  -d "$BUILD_DIR/classes" \
  "${JAVA_SOURCES[@]}"

jar cf "$BUILD_DIR/app-classes.jar" -C "$BUILD_DIR/classes" .

echo "🔨 [Step 3/5] 正在使用 D8 转换为 Dalvik DEX ..."
java -cp "$D8_JAR" com.android.tools.r8.D8 \
  --min-api 29 \
  --lib "$ANDROID_JAR" \
  --output "$BUILD_DIR/dex" \
  "$BUILD_DIR/app-classes.jar"

echo "🔨 [Step 4/5] 正在合并 classes.dex 并执行 4 字节对齐 (zipalign)..."
python3 - "$BUILD_DIR/unaligned.apk" "$BUILD_DIR/dex/classes.dex" << 'PY'
import sys, zipfile
apk_path, dex_path = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk_path, 'a', zipfile.ZIP_DEFLATED) as zf:
    zf.write(dex_path, 'classes.dex')
PY

"$ZIPALIGN" -f -p 4 "$BUILD_DIR/unaligned.apk" "$BUILD_DIR/aligned.apk"

echo "🔨 [Step 5/5] 正在签名 (v1 + v2 + v3)..."
KEYSTORE="$CACHE_DIR/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
  keytool -genkeypair -v \
    -keystore "$KEYSTORE" \
    -storepass android \
    -alias androiddebugkey \
    -keypass android \
    -keyalg RSA \
    -keysize 2048 \
    -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1
fi

OUT_APK="$SCRIPT_DIR/SshAdbTunnel-universal.apk"
java -jar "$APKSIGNER_JAR" sign \
  --ks "$KEYSTORE" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "$OUT_APK" \
  "$BUILD_DIR/aligned.apk"

rm -rf "$BUILD_DIR"

echo "=================================================================="
echo "✅ 通用架构 APK 编译成功！"
echo "📦 输出文件: $OUT_APK"
ls -lh "$OUT_APK"
echo "=================================================================="
