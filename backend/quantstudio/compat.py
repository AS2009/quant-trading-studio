# -*- coding: utf-8 -*-
"""可选依赖探测：pandas / numpy 存在则启用加速路径，不存在则用纯标准库实现。

当前版本的核心逻辑**不依赖** pandas；此处仅为后续因子研究预留接入点，
例如：`if HAS_PANDAS: df = to_dataframe(bars)`。
"""

try:  # pragma: no cover - 取决于运行环境
    import numpy as _np  # noqa: F401

    HAS_NUMPY = True
except Exception:  # pragma: no cover
    HAS_NUMPY = False

try:  # pragma: no cover
    import pandas as _pd  # noqa: F401

    HAS_PANDAS = True
except Exception:  # pragma: no cover
    HAS_PANDAS = False

HAS_VECTOR_BACKEND = HAS_NUMPY and HAS_PANDAS


def backend_info() -> dict:
    """供 /api/system/status 展示当前计算后端。"""
    return {
        "numpy": HAS_NUMPY,
        "pandas": HAS_PANDAS,
        "mode": "pandas+numpy" if HAS_VECTOR_BACKEND else "stdlib",
    }


def to_dataframe(bars):  # pragma: no cover - 可选路径
    """把 Bar 列表转成 DataFrame（仅在已安装 pandas 时可用）。

    后续做因子研究 / 向量化回测时可直接复用：
        df = to_dataframe(provider.kline("600519.SH", days=1200))
        df["ma20"] = df["close"].rolling(20).mean()
    """
    if not HAS_PANDAS:
        raise RuntimeError("未安装 pandas；请 pip install pandas 或使用纯标准库路径")
    import pandas as _pd

    return _pd.DataFrame([b.to_dict() if hasattr(b, "to_dict") else b for b in bars])
