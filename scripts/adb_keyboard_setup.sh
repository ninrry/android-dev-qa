#!/bin/bash
# ADBKeyboard Setup Script
# Installs and configures ADBKeyboard for Unicode input on Android emulator
#
# Usage: ./adb_keyboard_setup.sh [serial]
# Default serial: emulator-5554

set -e

# Configuration
ADB="/mnt/c/Users/d5u5ei/AppData/Local/Android/Sdk/platform-tools/adb.exe"
SERIAL="${1:-emulator-5554}"
APK_URL="https://github.com/senzhk/ADBKeyBoard/raw/master/ADBKeyboard.apk"
APK_DIR="$(dirname "$0")"
APK_PATH="$APK_DIR/ADBKeyboard.apk"
IME_PACKAGE="com.android.adbkeyboard/.AdbIME"

echo "=== ADBKeyboard Setup ==="
echo "Device: $SERIAL"
echo ""

# Step 1: Download APK if not exists
if [ ! -f "$APK_PATH" ]; then
    echo "[1/4] Downloading ADBKeyboard APK..."
    curl -L -o "$APK_PATH" "$APK_URL"
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to download ADBKeyboard APK"
        exit 1
    fi
    echo "  Downloaded to: $APK_PATH"
else
    echo "[1/4] ADBKeyboard APK already exists at: $APK_PATH"
fi

# Step 2: Verify emulator is connected
echo "[2/4] Checking emulator connection..."
DEVICES=$("$ADB" devices | grep "$SERIAL" || true)
if [ -z "$DEVICES" ]; then
    echo "ERROR: Device $SERIAL not found. Is the emulator running?"
    exit 1
fi
echo "  Device connected: $SERIAL"

# Step 3: Install APK
echo "[3/4] Installing ADBKeyboard..."
"$ADB" -s "$SERIAL" install -r "$APK_PATH"
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install ADBKeyboard"
    exit 1
fi
echo "  Installation successful"

# Step 4: Enable and set as default IME
echo "[4/4] Configuring ADBKeyboard as default IME..."
"$ADB" -s "$SERIAL" shell ime enable "$IME_PACKAGE"
"$ADB" -s "$SERIAL" shell ime set "$IME_PACKAGE"
echo "  ADBKeyboard set as default IME"

# Verify installation
echo ""
echo "=== Verification ==="
echo "Installed IMEs:"
"$ADB" -s "$SERIAL" shell ime list -s
echo ""
echo "Default IME:"
"$ADB" -s "$SERIAL" shell settings get secure default_input_method
echo ""
echo "=== Setup Complete ==="
echo "You can now use ADBKeyboard for Unicode input via ADB commands."
echo "Example: adb -s $SERIAL shell am broadcast -a ADB_INPUT_TEXT --es msg 'Your Unicode text here'"
