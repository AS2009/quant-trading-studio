#!/usr/bin/env bash
# QuantTrading Studio 桌面版打包脚本（macOS / Linux，供开发者验证打包）
#
# 用法：
#   ./desktop/build/build_unix.sh                 # onedir + 产物自检
#   ./desktop/build/build_unix.sh --onefile       # 额外构建单文件版
#   ./desktop/build/build_unix.sh --skip-tests    # 只打包
#   PYTHON=/usr/bin/python3 ./desktop/build/build_unix.sh   # 指定解释器
#   ./desktop/build/build_unix.sh --clean --skip-deps
#
# 说明：
#   * Windows 上的正式产物请用 desktop/build/build_windows.ps1 或 GitHub Actions；
#     本脚本仅用于在 macOS/Linux 上验证 spec 与打包产物是否可用（CI 无法覆盖这两端）；
#   * 解释器**必须自带 tkinter**（macOS 上 python.org 安装包或系统 /usr/bin/python3 才有；
#     Homebrew python 默认没有 Tk，会看到「缺少 tkinter」自检失败）；
#   * 产物在 desktop/build/output/ 下（已在 .gitignore 中忽略）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DESKTOP_DIR}/.." && pwd)"

PYTHON="${PYTHON:-python3}"
OUTPUT_DIR="${SCRIPT_DIR}/output"
DIST_ONEDIR="${OUTPUT_DIR}/dist"
WORK_ONEDIR="${OUTPUT_DIR}/work"
DIST_ONEFILE="${OUTPUT_DIR}/dist-onefile"
WORK_ONEFILE="${OUTPUT_DIR}/work-onefile"
REPORT_FILE="${TMPDIR:-/tmp}/quantstudio_selftest.txt"

ONE_FILE=0
SKIP_TESTS=0
SKIP_DEPS=0
CLEAN=0

for arg in "$@"; do
  case "${arg}" in
    --onefile)     ONE_FILE=1 ;;
    --skip-tests)  SKIP_TESTS=1 ;;
    --skip-deps)   SKIP_DEPS=1 ;;
    --clean)       CLEAN=1 ;;
    -h|--help)
      sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：${arg}（可用：--onefile --skip-tests --skip-deps --clean）" >&2; exit 2 ;;
  esac
done

step() { printf '\n==== %s\n' "$1"; }
die()  { printf '\n[错误] %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 环境
step "0/6 环境"
[ -d "${REPO_ROOT}/backend/quantstudio" ] || die "找不到 ${REPO_ROOT}/backend —— 请从仓库中运行本脚本"
[ -f "${DESKTOP_DIR}/quantstudio_desktop/__main__.py" ] || die "找不到桌面版入口 __main__.py"
echo "仓库根目录 : ${REPO_ROOT}"
echo "平台       : $(uname -s) ($(uname -m))"
echo "Python     : ${PYTHON}"
"${PYTHON}" -c 'import sys; print("版本       :", sys.version.split()[0])'
"${PYTHON}" -c 'import tkinter; print("tkinter    : 可用（Tk %s）" % tkinter.TkVersion)' \
  || die "该 Python 没有 tkinter：请改用自带 Tk 的解释器（macOS 可用 /usr/bin/python3）"

if [ "${CLEAN}" = "1" ] && [ -d "${OUTPUT_DIR}" ]; then
  echo "清理       : ${OUTPUT_DIR}"
  rm -rf "${OUTPUT_DIR}"
fi

# ---------------------------------------------------------------- 1. 打包依赖
step "1/6 打包依赖"
if [ "${SKIP_DEPS}" = "1" ]; then
  echo "已跳过（--skip-deps）"
else
  if ! "${PYTHON}" -c 'import PyInstaller' >/dev/null 2>&1; then
    echo "安装 PyInstaller（requirements-desktop.txt）…"
    "${PYTHON}" -m pip install -r "${REPO_ROOT}/requirements-desktop.txt"
  fi
fi
"${PYTHON}" -m PyInstaller --version | sed 's/^/PyInstaller : /'

# ---------------------------------------------------------------- 2. 测试与校验
step "2/6 核心测试 / 策略校验"
if [ "${SKIP_TESTS}" = "1" ]; then
  echo "已跳过（--skip-tests）"
else
  if "${PYTHON}" -c 'import flask' >/dev/null 2>&1; then
    ( cd "${REPO_ROOT}" && "${PYTHON}" -m unittest discover -s backend/tests -v )
  else
    echo "[警告] 未安装 flask（Web 版测试 backend/tests/test_api.py 需要），跳过核心测试"
  fi
  ( cd "${REPO_ROOT}" && "${PYTHON}" scripts/check_strategies.py )
fi

# ---------------------------------------------------------------- 3. 源码自检
step "3/6 源码自检"
( cd "${DESKTOP_DIR}" && "${PYTHON}" -m quantstudio_desktop --selftest )

# ---------------------------------------------------------------- 4. 打包
step "4/6 PyInstaller 打包"
build_spec() {  # $1=spec 相对路径  $2=distpath  $3=workpath
  echo "-> $1"
  ( cd "${REPO_ROOT}" && "${PYTHON}" -m PyInstaller "$1" --noconfirm \
      --distpath "$2" --workpath "$3" )
}
build_spec "desktop/build/quantstudio.spec" "${DIST_ONEDIR}" "${WORK_ONEDIR}"
if [ "${ONE_FILE}" = "1" ]; then
  build_spec "desktop/build/quantstudio-onefile.spec" "${DIST_ONEFILE}" "${WORK_ONEFILE}"
fi

# 找产物可执行文件：onedir 是目录下的同名文件；macOS 若被包成 .app 则在 Contents/MacOS 下
find_exe() {  # $1=dist 目录  $2=名字
  local dist_dir="$1" name="$2"
  local candidates=(
    "${dist_dir}/${name}/${name}"
    "${dist_dir}/${name}.app/Contents/MacOS/${name}"
    "${dist_dir}/${name}"
    "${dist_dir}/${name}.exe"
  )
  for candidate in "${candidates[@]}"; do
    if [ -f "${candidate}" ]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------- 5. 产物自检
step "5/6 产物自检（关键）"
EXE="$(find_exe "${DIST_ONEDIR}" "QuantTradingStudio")" || die "找不到 onedir 产物可执行文件（${DIST_ONEDIR}）"
echo "产物       : ${EXE}"
rm -f "${REPORT_FILE}"
set +e
"${EXE}" --selftest
SELFTEST_CODE=$?
set -e
echo "退出码     : ${SELFTEST_CODE}"
if [ -f "${REPORT_FILE}" ]; then
  echo "---- 自检报告（${REPORT_FILE}）----"
  cat "${REPORT_FILE}"
  echo "---- 报告结束 ----"
fi
[ "${SELFTEST_CODE}" -eq 0 ] || die "产物 --selftest 失败（exit=${SELFTEST_CODE}）"

if [ "${ONE_FILE}" = "1" ]; then
  EXE_ONE="$(find_exe "${DIST_ONEFILE}" "QuantTradingStudio")" || die "找不到 onefile 产物（${DIST_ONEFILE}）"
  echo ""
  echo "onefile 产物: ${EXE_ONE}"
  "${EXE_ONE}" --selftest
  echo "onefile 退出码: $?"
fi

# ---------------------------------------------------------------- 6. 汇总
step "6/6 产物"
du -sh "${DIST_ONEDIR}"/* 2>/dev/null || true
if [ "${ONE_FILE}" = "1" ]; then
  du -sh "${DIST_ONEFILE}"/* 2>/dev/null || true
fi
echo ""
echo "打包完成 ✅  onedir 产物目录：${DIST_ONEDIR}/QuantTradingStudio"
echo "Windows 正式发布请使用 build_windows.ps1 / GitHub Actions（.exe + 版本资源 + 图标）。"
