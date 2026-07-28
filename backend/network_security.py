"""模型服务出站地址的统一安全边界。

Base URL 同时被“模型连接测试”和真正的 Agent 运行使用。若两个入口各自校验，
就容易形成 confused deputy：UI 测试的是安全地址，运行时却访问另一套未校验地址。
本模块因此同时负责 URL 语法、DNS 解析结果和部署模式策略。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit


class ModelBaseUrlError(ValueError):
    """可安全返回给客户端的 Base URL 校验错误。"""


Resolver = Callable[..., Iterable[tuple]]

_CLOUD_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}
_CLOUD_METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
}


def validate_base_url_syntax(base_url: str) -> str:
    """校验不需要网络访问的 URL 结构，并返回去除首尾空白的值。"""

    value = base_url.strip()
    if not value:
        raise ModelBaseUrlError("请填写 Base URL")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ModelBaseUrlError("Base URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ModelBaseUrlError("Base URL 仅允许使用 http 或 https")
    if not parsed.hostname:
        raise ModelBaseUrlError("Base URL 必须包含主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ModelBaseUrlError("Base URL 不得包含用户名或密码")
    if parsed.fragment:
        raise ModelBaseUrlError("Base URL 不得包含 fragment")
    return value


def _is_loopback_bind(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def private_model_access_allowed() -> bool:
    """解析本地模型访问策略。

    未显式配置时，只有绑定回环地址的桌面/开发服务可访问回环和私网模型。
    一旦后端对局域网或公网监听，默认转为拒绝；管理员仍可通过环境变量作出
    明确的部署级授权，而不是让单次浏览器请求扩大服务器的网络能力。
    """

    configured = os.getenv("ALLOW_PRIVATE_MODEL_BASE_URL")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return _is_loopback_bind(os.getenv("BACKEND_HOST", "127.0.0.1"))


def _resolved_addresses(hostname: str, port: int, resolver: Resolver) -> set:
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ModelBaseUrlError("Base URL 主机名无法解析") from exc

    addresses = set()
    for record in records:
        sockaddr = record[4]
        if not sockaddr:
            continue
        raw_address = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        addresses.add(address)
    if not addresses:
        raise ModelBaseUrlError("Base URL 主机名未解析到有效 IP 地址")
    return addresses


def validate_model_base_url(
    base_url: str,
    *,
    allow_private: bool | None = None,
    resolver: Resolver | None = None,
) -> str:
    """校验 URL 及其全部 DNS 结果，阻断 SSRF 常见的地址别名绕过。"""

    value = validate_base_url_syntax(base_url)
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname in _CLOUD_METADATA_HOSTNAMES:
        raise ModelBaseUrlError("Base URL 不得指向云元数据服务")

    effective_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    addresses = _resolved_addresses(
        hostname,
        effective_port,
        resolver or socket.getaddrinfo,
    )
    permit_private = (
        private_model_access_allowed() if allow_private is None else allow_private
    )

    for address in addresses:
        if address in _CLOUD_METADATA_ADDRESSES or address.is_link_local:
            raise ModelBaseUrlError("Base URL 不得指向云元数据或链路本地地址")
        if address.is_unspecified or address.is_multicast or address.is_reserved:
            raise ModelBaseUrlError("Base URL 解析到了不可访问的保留地址")
        if not permit_private and (address.is_loopback or address.is_private):
            raise ModelBaseUrlError(
                "后端对非回环地址提供服务时，默认禁止访问回环或私网模型地址"
            )
    return value
