#!/usr/bin/env bash
# QuantTrading Studio —— macOS 打包（生成 QuantTradingStudio.app + zip + dmg）
#
# 用法：
#   ./desktop/build/build_macos.sh                    # 默认 universal2 + 自检 + zip + dmg
#   ./desktop/build/build_macos.sh --arch arm64        # 只打当前机器架构（体积更小、构建更快）
#   ./desktop/build/build_macos.sh --clean --skip-tests
#   ./desktop/build/build_macos.sh --no-dmg --no-zip
#   PYTHON=/usr/bin/python3 ./desktop/build/build_macos.sh
#
# 说明：
#   * 解释器**必须自带 tkinter**：macOS 上用 /usr/bin/python3（Xcode 命令行工具自带 Tk 8.5）
#     或 python.org 官方安装包；Homebrew 的 python 默认没有 Tk（可 brew install python-tk@3.12 补上）；
#   * Tcl/Tk 在 macOS 11+ 由系统 dyld 共享缓存提供（/System/Library/Frameworks 里是 stub，没有磁盘二进制），
#     所以 .app **不会**把 Tk 复制进去，运行时用系统 Tk 8.5 —— 这样产物体积小，但要求目标机器有该系统框架；
#   * 产物是 **ad-hoc 签名**（arm64 上必须至少 ad-hoc 签名才能启动），**未做开发者签名与公证**：
#     用户首次打开需右键 →「打开」，或执行 xattr -dr com.apple.quarantine 去掉隔离标记；
#   * 全部产物在 desktop/build/output-macos/（已在 .gitignore 中忽略），不污染仓库。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DESKTOP_DIR}/.." && pwd)"

PYTHON="${PYTHON:-/usr/bin/python3}"
APP_NAME="QuantTradingStudio"
OUT_DIR="${SCRIPT_DIR}/output-macos"
DIST_DIR="${OUT_DIR}/dist"
WORK_DIR="${OUT_DIR}/work"
PKG_DIR="${OUT_DIR}/pkg"
APP_PATH="${DIST_DIR}/${APP_NAME}.app"
REPORT_FILE="${TMPDIR:-/tmp}quantstudio_selftest.txt"
#: 目标架构：auto（按解释器判定：Homebrew/官方单架构 → 本机架构；universal2 解释器 → universal2）
ARCH="${QUANTSTUDIO_MACOS_ARCH:-auto}"

MAKE_DMG=1
MAKE_ZIP=1
SKIP_TESTS=0
CLEAN=0
REBUILD_ICON=0

for arg in "$@"; do
  case "${arg}" in
    --arch=*)      ARCH="${arg#*=}" ;;
    --arch)        echo "请用 --arch=arm64 / --arch=x86_64 / --arch=universal2" >&2; exit 2 ;;
    --no-dmg)      MAKE_DMG=0 ;;
    --no-zip)      MAKE_ZIP=0 ;;
    --skip-tests)  SKIP_TESTS=1 ;;
    --clean)       CLEAN=1 ;;
    --rebuild-icon) REBUILD_ICON=1 ;;
    -h|--help)
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "未知参数：${arg}（可用：--arch=<arch> --no-dmg --no-zip --skip-tests --clean --rebuild-icon）" >&2; exit 2 ;;
  esac
done
case "${ARCH}" in
  auto) ARCH="" ;;                       # 下面按解释器判定
  universal2|arm64|x86_64) ;;
  *) echo "不支持的 --arch=${ARCH}（可用：auto / universal2 / arm64 / x86_64）" >&2; exit 2 ;;
esac

step() { printf '\n==== %s\n' "$1"; }
die()  { printf '\n[错误] %s\n' "$1" >&2; exit 1; }
note() { printf '  %s\n' "$1"; }

# ---------------------------------------------------------------- 0. 环境
step "0/7 环境"
[ "$(uname -s)" = "Darwin" ] || die "本脚本只在 macOS 上使用（Windows 用 build_windows.ps1 / GitHub Actions）"
[ -d "${REPO_ROOT}/backend/quantstudio" ] || die "找不到 ${REPO_ROOT}/backend —— 请在仓库内运行"
[ -f "${DESKTOP_DIR}/quantstudio_desktop/__main__.py" ] || die "找不到桌面版入口 __main__.py"
[ -x "${PYTHON}" ] || [ "$(command -v "${PYTHON}" || true)" != "" ] || die "找不到解释器：${PYTHON}"
echo "仓库根目录 : ${REPO_ROOT}"
echo "平台       : $(uname -s) $(uname -m) / macOS $(sw_vers -productVersion 2>/dev/null || echo '?')"
echo "解释器     : ${PYTHON}"
"${PYTHON}" -c 'import sys; print("Python     :", sys.version.split()[0])'
echo "Tk         : $("${PYTHON}" -c 'import tkinter; print("%s（Tcl %s）" % (tkinter.TkVersion, tkinter.TclVersion))' 2>/dev/null || echo 缺失)"
"${PYTHON}" -c 'import tkinter' 2>/dev/null \
  || die "该 Python 没有 tkinter：macOS 上推荐 Homebrew 的 python-tk@3.12（自带 Tcl/Tk 8.6/9，会被打进 .app）或 python.org 安装包；/usr/bin/python3 只有系统 Tk 8.5"
command -v iconutil >/dev/null 2>&1 || die "找不到 iconutil（属 Xcode 命令行工具）：xcode-select --install"
command -v codesign >/dev/null 2>&1 || die "找不到 codesign（属 Xcode 命令行工具）：xcode-select --install"
"${PYTHON}" -c 'import PyInstaller' >/dev/null 2>&1 \
  || die "该 Python 未安装 PyInstaller：${PYTHON} -m pip install -r requirements-desktop.txt"
"${PYTHON}" -m PyInstaller --version | sed 's/^/PyInstaller : /'

# 架构：默认按解释器判定 —— Homebrew/单架构解释器只能出本机架构；只有 universal2 解释器才能出通用二进制
if [ -z "${ARCH}" ]; then
  PY_REAL="$("${PYTHON}" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "${PYTHON}")"
  PY_ARCHS="$(lipo -archs "${PY_REAL}" 2>/dev/null || echo unknown)"
  case "${PY_ARCHS}" in
    *x86_64*arm64*|*arm64*x86_64*) ARCH="universal2" ;;
    *) ARCH="$(uname -m)" ;;
  esac
  echo "架构       : ${ARCH}（按解释器判定：${PY_ARCHS}；可用 --arch= 覆盖）"
else
  echo "架构       : ${ARCH}（--arch 指定）"
fi

# Tk 8.5 是 2009 年的老版本，在 macOS 10.14+ 上可能整窗不绘制（白屏）——提醒换新 Tk，但不阻断
if [ "$("${PYTHON}" -c 'import tkinter; print(tkinter.TkVersion)' 2>/dev/null || echo 0)" = "8.5" ]; then
  note "[警告] 当前解释器用的是系统 Tk 8.5：macOS 14+ 上可能白屏，建议改用自带 Tcl/Tk ≥ 8.6 的解释器"
  note "       例如：brew install python-tk@3.12 && PYTHON=\$(brew --prefix python@3.12)/bin/python3.12 \$0"
fi

if [ "${CLEAN}" = "1" ] && [ -d "${OUT_DIR}" ]; then
  echo "清理       : ${OUT_DIR}"
  rm -rf "${OUT_DIR}"
fi
mkdir -p "${DIST_DIR}" "${WORK_DIR}" "${PKG_DIR}"

# ---------------------------------------------------------------- 1. 图标
step "1/7 应用图标 icon.icns"
ICNS="${SCRIPT_DIR}/icon.icns"
if [ "${REBUILD_ICON}" = "1" ] || [ ! -f "${ICNS}" ]; then
  "${PYTHON}" "${SCRIPT_DIR}/make_icns.py"
else
  note "已存在：${ICNS}（$(du -h "${ICNS}" | cut -f1)）；需要重绘加 --rebuild-icon"
fi

# ---------------------------------------------------------------- 2. 源码自检
step "2/7 源码自检（打包前先确认源码树可用）"
if [ "${SKIP_TESTS}" = "1" ]; then
  note "已跳过（--skip-tests）"
else
  ( cd "${DESKTOP_DIR}" && PYTHONPATH="${REPO_ROOT}:${DESKTOP_DIR}" "${PYTHON}" -m quantstudio_desktop --selftest )
fi

# ---------------------------------------------------------------- 3. 打包
step "3/7 PyInstaller 构建 .app（arch=${ARCH}）"
( cd "${REPO_ROOT}" && QUANTSTUDIO_MACOS_ARCH="${ARCH}" "${PYTHON}" -m PyInstaller \
    desktop/build/quantstudio.spec --noconfirm \
    --distpath "${DIST_DIR}" --workpath "${WORK_DIR}" )
[ -d "${APP_PATH}" ] || die "构建结束但找不到 ${APP_PATH}"

# ---------------------------------------------------------------- 4. 签名与结构校验
step "4/7 ad-hoc 签名 + 结构校验"
set +e
codesign --force --deep --sign - "${APP_PATH}" 2>&1 | sed 's/^/  /'
SIGN_CODE="${PIPESTATUS[0]}"
set -e
[ "${SIGN_CODE}" -eq 0 ] || die "ad-hoc 签名失败（codesign exit=${SIGN_CODE}）"
set +e
codesign --verify --deep --strict --verbose=2 "${APP_PATH}" 2>&1 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] || note "[警告] codesign --verify 未通过（ad-hoc 签名下常见，不影响本机运行）"
set -e
EXE="${APP_PATH}/Contents/MacOS/${APP_NAME}"
[ -f "${EXE}" ] || die "找不到 .app 内可执行文件：${EXE}"
echo "  架构       : $(lipo -info "${EXE}" | sed 's/^.*: //')"
echo "  版本       : $("${EXE}" --version 2>&1 | head -1)"
echo "  Info.plist : $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "${APP_PATH}/Contents/Info.plist" 2>/dev/null || echo '?')"
echo "  图标       : $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "${APP_PATH}/Contents/Info.plist" 2>/dev/null || echo '（默认）')"
echo "  包大小     : $(du -sh "${APP_PATH}" | cut -f1)"

# ---------------------------------------------------------------- 5. 产物自检
step "5/7 产物自检（关键：跑 .app 内的可执行文件）"
if [ "${SKIP_TESTS}" = "1" ]; then
  note "已跳过（--skip-tests）"
else
  rm -f "${REPORT_FILE}"
  set +e
  "${EXE}" --selftest
  CODE_SELF=$?
  set -e
  echo "  --selftest 退出码：${CODE_SELF}"
  if [ -f "${REPORT_FILE}" ]; then
    echo "  ---- 自检报告（${REPORT_FILE}）----"
    sed 's/^/  /' "${REPORT_FILE}"
    echo "  ---- 报告结束 ----"
  fi
  [ "${CODE_SELF}" -eq 0 ] || die "产物 --selftest 失败（exit=${CODE_SELF}）：若这台机器报 'macOS 1x (…1408) or later required, have instead …'，说明它的系统 Tk 无法加载（常见于 CI runner / VM 的镜像与内核版本不一致），加 --skip-tests 只做构建与结构校验"

  set +e
  MCP_OUT="$("${EXE}" --mcp --selftest 2>&1)"
  CODE_MCP=$?
  set -e
  echo "  --mcp --selftest 退出码：${CODE_MCP}"
  printf '%s\n' "${MCP_OUT}" | tail -3 | sed 's/^/  /'
  [ "${CODE_MCP}" -eq 0 ] || die "产物 --mcp --selftest 失败（exit=${CODE_MCP}）"

  set +e
  "${EXE}" --selftest-gui >/dev/null 2>&1
  CODE_GUI=$?
  set -e
  echo "  --selftest-gui 退出码：${CODE_GUI}（构建全部页面；无窗口会话时为非 0，不阻断）"

  # 自检报告归档到产物目录（CI 的 upload-artifact 会带上，方便排障）
  mkdir -p "${OUT_DIR}/selftest"
  for report in "${REPORT_FILE}" "${TMPDIR:-/tmp}quantstudio_mcp_selftest.txt"; do
    if [ -f "${report}" ]; then
      cp "${report}" "${OUT_DIR}/selftest/"
      echo "  已归档自检报告：$(basename "${report}")"
    fi
  done
fi

# ---------------------------------------------------------------- 6. 打包 zip / dmg
step "6/7 打包 zip / dmg"
SUFFIX="macos-${ARCH}"
ZIP_PATH="${PKG_DIR}/QuantTradingStudio-${SUFFIX}.zip"
DMG_PATH="${PKG_DIR}/QuantTradingStudio-${SUFFIX}.dmg"
if [ "${MAKE_ZIP}" = "1" ]; then
  rm -f "${ZIP_PATH}"
  # ditto 保留符号链接与扩展属性（.app 必须有正确的可执行位与链接结构）
  ditto -c -k --sequesterRsrc --keepParent "${APP_PATH}" "${ZIP_PATH}"
  echo "  zip        : ${ZIP_PATH}（$(du -h "${ZIP_PATH}" | cut -f1)）"
fi
if [ "${MAKE_DMG}" = "1" ]; then
  STAGE="${OUT_DIR}/dmg-stage"
  rm -rf "${STAGE}"
  mkdir -p "${STAGE}"
  cp -R "${APP_PATH}" "${STAGE}/"
  ln -s /Applications "${STAGE}/Applications"      # 拖拽安装用的快捷方式
  rm -f "${DMG_PATH}"
  hdiutil create -volname "QuantTrading Studio" -srcfolder "${STAGE}" -ov -format UDZO \
    -quiet "${DMG_PATH}"
  rm -rf "${STAGE}"
  echo "  dmg        : ${DMG_PATH}（$(du -h "${DMG_PATH}" | cut -f1)）"
fi

# ---------------------------------------------------------------- 7. 汇总
step "7/7 完成"
du -sh "${DIST_DIR}"/* 2>/dev/null | sed 's/^/  /' || true
cat <<EOF

macOS 产物已生成 ✅
  应用      : ${APP_PATH}
  分发文件  : ${PKG_DIR}/QuantTradingStudio-${SUFFIX}.{zip,dmg}

本机试运行：
  open "${APP_PATH}"                     # 正常打开
  "${APP_PATH}/Contents/MacOS/${APP_NAME}" --help

分发给别人（未签名/未公证，对方首次打开会被 Gatekeeper 拦）：
  1) 下载 zip 解压后，把 .app 拖进「应用程序」；
  2) 右键点图标 →「打开」→ 再点「打开」（只需一次）；或
     xattr -dr com.apple.quarantine /Applications/QuantTradingStudio.app
  3) 若系统提示「已损坏」，说明下载器加了隔离标记，用上面第 2 步的命令即可。
EOF
