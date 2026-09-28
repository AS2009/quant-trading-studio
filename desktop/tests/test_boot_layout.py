# -*- coding: utf-8 -*-
"""``boot`` 用户目录布局测试（v1.5.1：安装版把用户数据放在**程序目录**里）。

覆盖：

* 源码运行 = 仓库 ``backend/data``（开发期布局不变，含包内策略目录）；
* 打包运行且程序目录可写 = ``<程序目录>/data``（账本/缓存/CSV 导入）与 ``<程序目录>/strategies``（策略）；
* 打包运行但程序目录不可写 = 退回用户目录，并如实标注 mode；
* 显式 ``QUANTSTUDIO_DATA_DIR`` / ``QUANTSTUDIO_STRATEGIES_DIR`` 优先；
* 首次启动：播种内置示例策略、写各文件夹说明；已经有 .py 时不再播种；
* 旧版本用户目录（≤ v1.5.0）的数据只迁移一次，且不覆盖新位置已有文件。

为了能在开发机（macOS/Linux）上验证 Windows 安装版的语义，用例会把 ``sys.frozen``、
``sys.executable``、``sys.platform``、``LOCALAPPDATA`` 临时改掉，结束后完整还原。

运行::

    cd desktop && PYTHONPATH=..:. /usr/bin/python3 -m unittest discover -s tests
    /usr/bin/python3 desktop/tests/test_boot_layout.py
"""

import os
import shutil
import sys
import tempfile
import unittest

from unittest import mock  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio_desktop import boot  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")


class BootLayoutTest(unittest.TestCase):
    """把 ``boot`` 当成「打包运行」来跑，结束后完整还原解释器状态。"""

    ENV_KEYS = ("QUANTSTUDIO_DATA_DIR", "QUANTSTUDIO_STRATEGIES_DIR", "LOCALAPPDATA", "HOME", "USERPROFILE")
    SYS_ATTRS = ("frozen", "_MEIPASS", "executable", "platform")

    def setUp(self):
        self._env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self._attrs = {name: getattr(sys, name, None) for name in self.SYS_ATTRS}
        self._dirs = []
        self.localappdata = self._mk("qs-localappdata-")
        # 旧版本（≤ v1.5.0）的数据位置：Windows 语义下就是 <LOCALAPPDATA>\QuantTradingStudio\data
        self.legacy = os.path.join(self.localappdata, "QuantTradingStudio", "data")

    def tearDown(self):
        for name, value in self._attrs.items():
            if value is None:
                try:
                    delattr(sys, name)
                except AttributeError:
                    pass
            else:
                setattr(sys, name, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in self._dirs:
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _mk(self, prefix):
        path = tempfile.mkdtemp(prefix=prefix)
        self._dirs.append(path)
        return path

    def _frozen(self, program_dir, localappdata=None):
        """切到「打包运行（Windows）」状态，返回程序目录。"""
        os.environ.pop("QUANTSTUDIO_DATA_DIR", None)
        os.environ.pop("QUANTSTUDIO_STRATEGIES_DIR", None)
        os.environ["LOCALAPPDATA"] = localappdata or self.localappdata
        sys.frozen = True                                     # type: ignore[attr-defined]
        sys.platform = "win32"                                # type: ignore[attr-defined]
        sys.executable = os.path.join(program_dir, "QuantTradingStudio.exe")  # type: ignore[attr-defined]
        sys._MEIPASS = BACKEND_DIR                            # type: ignore[attr-defined]

    def _write(self, path, text):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # ------------------------------------------------------------------ 源码运行
    def test_source_run_keeps_repository_layout(self):
        os.environ.pop("QUANTSTUDIO_DATA_DIR", None)
        os.environ.pop("QUANTSTUDIO_STRATEGIES_DIR", None)
        resolved = boot.layout()
        self.assertEqual(resolved["mode"], "source")
        self.assertEqual(resolved["data_dir"], os.path.join(BACKEND_DIR, "data"))
        # 源码运行仍用包内 strategies/local（不导出环境变量，交给 registry 默认值）
        self.assertEqual(resolved["strategies_dir"], "")

    # ------------------------------------------------------------------ 安装目录可写
    def test_frozen_uses_program_directory(self):
        program = self._mk("qs-install-")
        self._frozen(program)
        resolved = boot.prepare_dirs()
        self.assertEqual(resolved["mode"], "install")
        self.assertEqual(resolved["root"], program)
        self.assertEqual(resolved["data_dir"], os.path.join(program, "data"))
        self.assertEqual(resolved["strategies_dir"], os.path.join(program, "strategies"))
        # 导出给核心包（config / registry 只认环境变量）
        self.assertEqual(os.environ["QUANTSTUDIO_DATA_DIR"], os.path.join(program, "data"))
        self.assertEqual(os.environ["QUANTSTUDIO_STRATEGIES_DIR"], os.path.join(program, "strategies"))
        for sub in ("csv", "level2", "cache"):
            self.assertTrue(os.path.isdir(os.path.join(program, "data", sub)), "缺少 data/%s" % sub)
        self.assertTrue(os.path.isdir(os.path.join(program, "strategies")))

    def test_first_run_seeds_bundled_strategies_and_notes(self):
        program = self._mk("qs-install-")
        self._frozen(program)
        resolved = boot.prepare_dirs()
        seeded = sorted(name for name in os.listdir(os.path.join(program, "strategies"))
                        if name.endswith(".py"))
        self.assertGreaterEqual(len(seeded), 3, "应当把内置示例策略复制进来：%s" % seeded)
        self.assertIn("_template.py", seeded, "模板也要复制一份，方便用户改名使用")
        self.assertEqual(resolved["seeded"], len(seeded))
        self.assertTrue(os.path.isfile(os.path.join(program, "strategies", "说明-把策略文件复制到这里.txt")))
        self.assertTrue(os.path.isfile(os.path.join(program, "data", "csv", "说明-把行情CSV复制到这里.txt")))
        self.assertTrue(os.path.isfile(os.path.join(program, "data", "level2", "说明-把盘口CSV复制到这里.txt")))

    def test_seed_does_not_touch_existing_strategies(self):
        program = self._mk("qs-install-")
        self._frozen(program)
        mine = self._write(os.path.join(program, "strategies", "my_own.py"), "# 我自己的策略\n")
        resolved = boot.prepare_dirs()
        self.assertEqual(resolved["seeded"], 0, "目录里已有 .py 时不应再播种（删掉的示例不该复活）")
        with open(mine, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# 我自己的策略\n")

    def test_notes_are_written_only_once(self):
        program = self._mk("qs-install-")
        self._frozen(program)
        boot.prepare_dirs()
        note = os.path.join(program, "strategies", "说明-把策略文件复制到这里.txt")
        with open(note, "a", encoding="utf-8") as handle:
            handle.write("\n（用户自己加的一行）\n")
        boot.prepare_dirs()
        with open(note, encoding="utf-8") as handle:
            self.assertIn("（用户自己加的一行）", handle.read(), "说明文件不该被覆盖")

    # ------------------------------------------------------------------ 旧数据迁移
    def test_legacy_user_data_migrated_once(self):
        program = self._mk("qs-install-")
        self._write(os.path.join(self.legacy, "watchlist.json"), '["600519.SH"]')
        self._write(os.path.join(self.legacy, "csv", "600000.csv"), "code,date,close\n600000.SH,2026-01-05,10.0\n")
        self._write(os.path.join(self.legacy, "cache", "x.json"), "{}")
        self._frozen(program)
        first = boot.prepare_dirs()
        self.assertTrue(first["migrated"], "旧位置有数据时应当迁移一次")
        self.assertTrue(os.path.isfile(os.path.join(program, "data", "watchlist.json")))
        self.assertTrue(os.path.isfile(os.path.join(program, "data", "csv", "600000.csv")))
        self.assertFalse(os.path.exists(os.path.join(program, "data", "cache", "x.json")),
                         "缓存目录不该被迁移（可重建）")
        self.assertTrue(os.path.isfile(os.path.join(self.legacy, "watchlist.json")), "原位置必须保留")
        # 用户改过新位置的文件后，再次启动不能再被旧数据覆盖
        self._write(os.path.join(program, "data", "watchlist.json"), '["000001.SH"]')
        second = boot.prepare_dirs()
        self.assertFalse(second["migrated"], "只迁移一次")
        with open(os.path.join(program, "data", "watchlist.json"), encoding="utf-8") as handle:
            self.assertIn("000001.SH", handle.read())

    # ------------------------------------------------------------------ 只读程序目录
    def test_readonly_program_dir_falls_back_to_user_dir(self):
        """程序目录不可写 → 退回用户目录（选择性 mock，跨平台一致：Windows 不认 chmod 权限位）。

        只把**程序目录**判为不可写：用户目录那一段仍走真实探测，否则会连回退位置一起否掉。
        """
        program = self._mk("qs-readonly-")
        self._frozen(program)
        program_abs = os.path.abspath(program)
        real_probe = boot.is_writable_dir

        def probe(path):
            if os.path.abspath(str(path)).startswith(program_abs):
                return False
            return real_probe(path)

        with mock.patch.object(boot, "is_writable_dir", side_effect=probe):
            resolved = boot.prepare_dirs()
        self.assertEqual(resolved["mode"], "user", "程序目录不可写时必须退回用户目录")
        self.assertEqual(resolved["data_dir"], os.path.join(self.localappdata, "QuantTradingStudio", "data"))
        self.assertEqual(resolved["strategies_dir"],
                         os.path.join(self.localappdata, "QuantTradingStudio", "strategies"))
        self.assertEqual(os.environ["QUANTSTUDIO_STRATEGIES_DIR"], resolved["strategies_dir"])

    @unittest.skipIf(sys.platform == "win32", "Windows 不按 POSIX 权限位判可写，chmod 无效")
    def test_is_writable_dir_rejects_readonly_dir(self):
        """真探测函数本身：POSIX 下只读目录必须判为不可写（安装到只读介质的情形）。"""
        target = self._mk("qs-ro-probe-")
        self.assertTrue(boot.is_writable_dir(target))
        os.chmod(target, 0o555)
        try:
            self.assertFalse(boot.is_writable_dir(target))
        finally:
            os.chmod(target, 0o755)

    # ------------------------------------------------------------------ 显式覆盖
    def test_explicit_env_wins(self):
        program = self._mk("qs-install-")
        custom_data = self._mk("qs-custom-data-")
        custom_strategies = self._mk("qs-custom-strategies-")
        self._frozen(program)
        os.environ["QUANTSTUDIO_DATA_DIR"] = custom_data
        os.environ["QUANTSTUDIO_STRATEGIES_DIR"] = custom_strategies
        resolved = boot.prepare_dirs()
        self.assertEqual(resolved["mode"], "explicit")
        self.assertEqual(resolved["data_dir"], custom_data)
        self.assertEqual(os.environ["QUANTSTUDIO_STRATEGIES_DIR"], custom_strategies)
        self.assertEqual(os.environ["QUANTSTUDIO_DATA_DIR"], custom_data)

    # ------------------------------------------------------------------ macOS 包
    def test_macos_bundle_keeps_user_dir(self):
        """macOS 的 .app 是**包**：即使包目录可写也不能往里写数据（会破坏签名）。"""
        bundle = self._mk("qs-app-")
        self._frozen(bundle)
        sys.platform = "darwin"                          # type: ignore[attr-defined]
        # expanduser("~") 指到临时目录，别污染真机：POSIX 读 HOME，Windows 读 USERPROFILE
        os.environ["HOME"] = self.localappdata
        os.environ["USERPROFILE"] = self.localappdata
        resolved = boot.prepare_dirs()
        self.assertEqual(resolved["mode"], "user")
        self.assertEqual(resolved["data_dir"],
                         os.path.join(self.localappdata, "Library", "Application Support",
                                      "QuantTradingStudio", "data"))
        self.assertFalse(os.path.exists(os.path.join(bundle, boot.DATA_DIRNAME)),
                         "不该往 .app 包里写数据（macOS 只能用自己的可写目录）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
