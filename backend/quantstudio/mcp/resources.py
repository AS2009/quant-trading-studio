# -*- coding: utf-8 -*-
"""MCP 资源：把仓库里的文档以 ``quantstudio://docs/<文件名>`` 暴露给大模型。

设计要点：

* **白名单**：只有下面 ``DOCUMENTS`` 里写死的文件可读。uri 必须**精确命中**白名单，
  永远不会用模型给的字符串去拼路径，因此天然免疫 ``../`` 之类的路径穿越；
* **按需读取**：文档正文不进上下文，模型需要时才 ``resources/read``，避免一次性灌满；
* **缺文件就跳过**：用户裁剪了 docs 目录（只留程序）时，``list()`` 不会抛异常，
  只是少列几个资源；
* **模板只声明**：``quantstudio://strategies/{strategy_id}`` 说明「策略源码也能读」，
  但真正读源码请走 ``strategy_read_source`` 工具（模板由客户端展示，服务器不实现读取）。
"""

import os
from typing import Any, Dict, List, Optional, Tuple

#: 文档资源的 uri 前缀
DOCS_URI_PREFIX = "quantstudio://docs/"
#: 策略源码模板（读取请用 strategy_read_source 工具）
STRATEGY_URI_TEMPLATE = "quantstudio://strategies/{strategy_id}"

#: 文档白名单：(uri 后缀, 资源 name, title, description（什么时候该读）, 相对仓库根的路径, mimeType)
DOCUMENTS: Tuple[Tuple[str, str, str, str, str, str], ...] = (
    (
        "strategy-spec.md", "strategy-spec.md", "策略编写规范（写策略前必读）",
        "准备动手写/改策略时先读：命名、目录结构、文件格式、必守规则、校验要求与上线检查清单。",
        "docs/strategy-spec.md", "text/markdown",
    ),
    (
        "strategy-examples.md", "strategy-examples.md", "策略完整示例（可复制骨架）",
        "需要照抄骨架、对照正确写法或排查常见错误时读。",
        "docs/strategy-examples.md", "text/markdown",
    ),
    (
        "strategy-api.md", "strategy-api.md", "策略可用 API 参考",
        "写策略时查 ctx 上下文、BaseStrategy 助手、参数 schema 字段、返回值语义就读它。",
        "docs/strategy-api.md", "text/markdown",
    ),
    (
        "ai-strategy-guide.md", "ai-strategy-guide.md", "让 AI 一次写对策略",
        "由大模型生成策略代码时的必读文件、生成流程、提示词模板与自检循环。",
        "docs/ai-strategy-guide.md", "text/markdown",
    ),
    (
        "desktop-gui.md", "desktop-gui.md", "Windows 桌面版使用与打包指南",
        "用户问桌面版怎么用、界面/快捷键、数据目录、命令行参数、打包或排障时读。",
        "docs/desktop-gui.md", "text/markdown",
    ),
    (
        "README.md", "README.md", "项目总览（仓库根 README）",
        "想快速了解项目能做什么、怎么安装启动、数据源降级链与回测口径时读。",
        "README.md", "text/markdown",
    ),
    (
        "data-dir.md", "backend/data/README.md", "运行时数据目录说明",
        "需要知道数据文件放在哪、CSV 导入格式、持仓/订单文件格式时读。",
        "backend/data/README.md", "text/markdown",
    ),
    (
        "docs-index.md", "docs/README.md", "文档索引",
        "不确定该读哪篇文档时先读它：按读者列出全部文档与内容简介。",
        "docs/README.md", "text/markdown",
    ),
)

_SUFFIX_MIME = {".json": "application/json", ".md": "text/markdown", ".txt": "text/plain"}


class DocsResources:
    """文档资源提供者（鸭子类型接口见 ``mcp/server.py``）。"""

    def __init__(self, context: Any = None) -> None:
        self.context = context
        #: uri → 白名单条目，read() 只认这张表
        self._by_uri: Dict[str, Tuple[str, str, str, str, str, str]] = {
            DOCS_URI_PREFIX + entry[0]: entry for entry in DOCUMENTS
        }

    # ------------------------------------------------------------------ 路径解析
    def _roots(self) -> List[str]:
        """候选仓库根目录（按优先级）。

        ``ctx.repo_root`` 优先（便于整体搬移）；随后是配置里的 ``PROJECT_DIR``；
        最后用本文件位置兜底。文档按相对仓库根的路径查找，任一候选命中即用。
        """
        candidates: List[str] = []
        root = getattr(self.context, "repo_root", "") or ""
        if root:
            candidates.append(os.path.abspath(root))
        try:
            from ..config import PROJECT_DIR

            candidates.append(os.path.abspath(PROJECT_DIR))
        except Exception:                          # noqa: BLE001 - 配置不可用时靠包位置兜底
            pass
        try:
            import sys

            meipass = getattr(sys, "_MEIPASS", "")          # PyInstaller 解包目录（包根）
            if meipass:
                candidates.append(os.path.abspath(meipass))
        except Exception:                                   # noqa: BLE001
            pass
        here = os.path.dirname(os.path.abspath(__file__))          # .../quantstudio/mcp
        candidates.append(os.path.dirname(os.path.dirname(os.path.dirname(here))))
        seen: List[str] = []
        for item in candidates:
            if item and item not in seen:
                seen.append(item)
        return seen

    def _resolve(self, relative: str) -> Optional[str]:
        """把白名单里的相对路径解析成真实文件；不存在返回 ``None``。"""
        parts = [part for part in relative.split("/") if part]
        for root in self._roots():
            path = os.path.join(root, *parts)
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _mime_for(relative: str, declared: str) -> str:
        return declared or _SUFFIX_MIME.get(os.path.splitext(relative)[1].lower(), "text/plain")

    # ------------------------------------------------------------------ 资源列表
    def list(self) -> List[Dict[str, Any]]:
        """列出真实存在的文档资源；文件缺失的条目直接跳过（不抛异常）。"""
        items: List[Dict[str, Any]] = []
        for token, name, title, description, relative, declared_mime in DOCUMENTS:
            if self._resolve(relative) is None:
                continue
            items.append({
                "uri": DOCS_URI_PREFIX + token,
                "name": name,
                "title": title,
                "description": description,
                "mimeType": self._mime_for(relative, declared_mime),
            })
        return items

    def read(self, uri: str) -> Optional[Dict[str, Any]]:
        """读取白名单内的文档；uri 不在白名单或文件缺失/不可读时返回 ``None``。"""
        if not isinstance(uri, str):
            return None
        entry = self._by_uri.get(uri)
        if entry is None:
            return None
        _token, _name, _title, _description, relative, declared_mime = entry
        path = self._resolve(relative)
        if path is None:
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            return None
        return {
            "contents": [{
                "uri": uri,
                "mimeType": self._mime_for(relative, declared_mime),
                "text": text,
            }],
        }

    def templates(self) -> List[Dict[str, Any]]:
        """声明「策略源码」资源模板（服务器不实现模板读取，读取请用 strategy_read_source）。"""
        return [{
            "uriTemplate": STRATEGY_URI_TEMPLATE,
            "name": "strategy-source",
            "description": "读取某个策略的源码（strategy_id 如 dual_ma、st_breakout_atr）。"
                           "本服务器只声明该模板，实际读取请调用 strategy_read_source 工具。",
        }]


__all__ = ["DocsResources", "DOCUMENTS", "DOCS_URI_PREFIX", "STRATEGY_URI_TEMPLATE"]
