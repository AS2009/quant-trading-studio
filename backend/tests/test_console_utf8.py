# -*- coding: utf-8 -*-
"""``quantstudio.console`` 的回归测试。

背景：Windows 的 ``sys.stdout`` 默认不是 UTF-8（控制台代码页 cp1252/cp936，重定向到
管道同理）。此时 ``print("中文")`` 抛 ``UnicodeEncodeError``，让 CLI 以非 0 退出——
GitHub Actions 的 windows-latest 上就真的这样红过一次（``strategies/lint.py``）。

这里用「cp1252 编码的文本流」在本机复现同一类环境（macOS/Linux 上默认是 UTF-8，
无法直接复现），断言兜底函数能把它切到 UTF-8 且中文可正常写出。
"""

import io
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio import console  # noqa: E402


def make_stream(encoding: str) -> io.TextIOWrapper:
    """构造一个用指定编码写入内存缓冲的文本流（模拟被重定向的标准输出）。"""
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n", write_through=True)


class ForceUtf8OutputTests(unittest.TestCase):

    def test_cp1252_stream_is_switched_and_chinese_writes(self):
        stream = make_stream("cp1252")
        self.assertFalse(console.is_utf8(stream))
        with self.assertRaises(UnicodeEncodeError):        # 前提：cp1252 写不了中文
            stream.write("中文")
            stream.flush()

        saved = sys.stdout
        sys.stdout = stream
        try:
            changed = console.force_utf8_output("stdout")
        finally:
            sys.stdout = saved

        self.assertEqual(changed, [("stdout", "cp1252")])
        self.assertTrue(console.is_utf8(stream))
        saved = sys.stdout
        sys.stdout = stream
        try:
            sys.stdout.write("中文 OK\n")                  # 不应再抛异常
        finally:
            sys.stdout = saved
        stream.flush()
        self.assertIn("中文 OK".encode("utf-8"), stream.buffer.getvalue())

    def test_errors_are_replaced_instead_of_raising(self):
        """无法表示的字符退化成 ?，绝不因为打印而崩。"""
        stream = make_stream("ascii")
        saved = sys.stdout
        sys.stdout = stream
        try:
            console.force_utf8_output("stdout")
            sys.stdout.write("中文 → OK\n")
        finally:
            sys.stdout = saved
        stream.flush()
        data = stream.buffer.getvalue()
        self.assertIn(b"OK", data)

    def test_already_utf8_is_untouched(self):
        stream = make_stream("utf-8")
        saved = sys.stdout
        sys.stdout = stream
        try:
            changed = console.force_utf8_output("stdout")
        finally:
            sys.stdout = saved
        self.assertEqual(changed, [], "已经是 UTF-8 就不该动它")
        self.assertTrue(console.is_utf8(stream))

    def test_none_and_foreign_streams_are_skipped(self):
        """``None``（windowed 打包）与不支持 reconfigure 的对象都必须安全跳过。"""
        class NoReconfigure(object):
            encoding = "cp936"

        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = None, NoReconfigure()
        try:
            self.assertEqual(console.force_utf8_output(), [])
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err

    def test_is_utf8_recognizes_cp65001(self):
        class Cp65001(object):
            encoding = "cp65001"

        self.assertTrue(console.is_utf8(Cp65001()))
        self.assertTrue(console.is_utf8(make_stream("utf-8")))
        self.assertFalse(console.is_utf8(make_stream("cp1252")))


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
