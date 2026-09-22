# -*- coding: utf-8 -*-
"""接口层统一错误处理与请求体工具。

约定：``/api/`` 下的任何错误（HTTP 异常 / 业务异常 / 未捕获异常）都返回 JSON
``{"error": str, "code": int, "field": str|null}``；非 ``/api/`` 路径保持 Flask
默认行为（静态资源 404 仍是 HTML）。
"""

from flask import jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError

from ..core.errors import (
    BrokerUnavailable,
    DataSourceError,
    OrderRejected,
    QuantStudioError,
    SymbolNotFound,
    ValidationError,
)
from ..services.common import Forbidden, NotFound


def _json_error(message, code, field=None):
    return jsonify({"error": str(message), "code": int(code), "field": field}), int(code)


_HTTP_MESSAGES = {
    400: "请求参数不合法",
    403: "无权限执行该操作",
    404: "接口或资源不存在",
    405: "请求方法不允许",
    415: "不支持的请求体格式",
    503: "服务暂时不可用",
}


def _http_message(err) -> str:
    """把 Werkzeug 的英文默认描述换成可读中文，自定义描述（多为中文）原样保留。"""
    desc = (getattr(err, "description", "") or "").strip()
    if not desc or desc.startswith("The "):
        return _HTTP_MESSAGES.get(getattr(err, "code", None)) or desc or "请求失败"
    return desc


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def json_body(field="body"):
    """读取并校验 JSON 请求体；非法 JSON / 非对象 → ValidationError(400)。"""
    payload = request.get_json(silent=True)
    if payload is None:
        raise ValidationError("请求体需为合法 JSON", field=field)
    if not isinstance(payload, dict):
        raise ValidationError("请求体需为 JSON 对象", field=field)
    return payload


def register_error_handlers(app):
    """注册全局错误处理（业务异常优先，其次 HTTPException，最后兜底 500）。"""

    @app.errorhandler(ValidationError)
    def _handle_validation(err):
        return _json_error(err, 400, getattr(err, "field", None))

    @app.errorhandler(SymbolNotFound)
    def _handle_symbol(err):
        return _json_error(err, 404, getattr(err, "field", None))

    @app.errorhandler(NotFound)
    def _handle_not_found(err):
        return _json_error(err, 404, getattr(err, "field", None))

    @app.errorhandler(Forbidden)
    def _handle_forbidden(err):
        return _json_error(err, 403, getattr(err, "field", None))

    @app.errorhandler(OrderRejected)
    def _handle_rejected(err):
        return _json_error(err, 400, getattr(err, "field", None))

    @app.errorhandler(BrokerUnavailable)
    def _handle_broker(err):
        return _json_error(err, 503, None)

    @app.errorhandler(DataSourceError)
    def _handle_datasource(err):
        return _json_error(err, 502, getattr(err, "field", None))

    @app.errorhandler(QuantStudioError)
    def _handle_quant(err):
        app.logger.warning("量化服务异常 %s %s: %s", request.method, request.path, err)
        return _json_error(err, 500, getattr(err, "field", None))

    @app.errorhandler(HTTPException)
    def _handle_http(err):
        if not _is_api_request():
            return err  # 非 API 路径交回 Flask 默认处理（HTML）
        return _json_error(_http_message(err), err.code or 500, None)

    @app.errorhandler(Exception)
    def _handle_uncaught(err):
        if isinstance(err, HTTPException):
            return _handle_http(err)
        app.logger.exception("未捕获异常 %s %s", request.method, request.path)
        if _is_api_request():
            return _json_error("服务内部错误，请查看后端日志", 500, None)
        return InternalServerError()
