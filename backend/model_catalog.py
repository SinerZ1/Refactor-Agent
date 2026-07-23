"""不同模型服务的连接校验与模型目录查询。

本模块把第三方鉴权留在后端，前端只调用同源接口。这样做相当于在 UI 与供应商
API 之间增加 Anti-Corruption Layer：各供应商不同的鉴权、分页和响应格式在此被
归一化为模型 ID 列表，避免这些差异污染 Agent 工作流状态。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from google import genai
from google.auth import load_credentials_from_file
from google.auth.exceptions import GoogleAuthError

Provider = Literal["openai", "gemini_studio", "google_vertex"]
AuthMode = Literal["adc", "api_key"]

GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class ModelCatalogError(RuntimeError):
    """可安全返回给前端的连接或目录查询错误。"""


@dataclass(frozen=True)
class ModelConnectionConfig:
    provider: Provider
    api_key: str = ""
    base_url: str = ""
    project_id: str = ""
    location: str = "global"
    auth_mode: AuthMode = "adc"


@dataclass(frozen=True)
class AdcInspection:
    available: bool
    message: str
    source: str | None = None
    project_id: str | None = None
    credential_type: str | None = None
    path: Path | None = None

    def public_dict(self) -> dict[str, str | bool | None]:
        """只暴露状态元数据，主动隐藏本机凭据路径。"""

        return {
            "available": self.available,
            "message": self.message,
            "source": self.source,
            "project_id": self.project_id,
            "credential_type": self.credential_type,
        }


def _adc_candidates() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    configured_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured_path:
        candidates.append(("environment", Path(configured_path).expanduser()))

    app_data = os.getenv("APPDATA")
    if app_data:
        candidates.append(
            (
                "gcloud",
                Path(app_data) / "gcloud" / "application_default_credentials.json",
            )
        )
    else:
        candidates.append(
            (
                "gcloud",
                Path.home()
                / ".config"
                / "gcloud"
                / "application_default_credentials.json",
            )
        )

    # 保持搜索顺序但去重，避免环境变量恰好指向 gcloud 默认文件时重复解析。
    seen: set[Path] = set()
    unique_candidates: list[tuple[str, Path]] = []
    for candidate in candidates:
        if candidate[1] not in seen:
            seen.add(candidate[1])
            unique_candidates.append(candidate)
    return unique_candidates


def inspect_adc_file() -> AdcInspection:
    """检查 ADC 搜索路径中是否存在可被 google-auth 解析的凭据文件。"""

    invalid_sources: list[str] = []
    for source, path in _adc_candidates():
        if not path.is_file():
            continue
        try:
            credentials, project_id = load_credentials_from_file(
                str(path), scopes=[GOOGLE_CLOUD_SCOPE]
            )
            return AdcInspection(
                available=True,
                message="已找到有效 ADC 凭据",
                source=source,
                project_id=project_id,
                credential_type=credentials.__class__.__name__,
                path=path,
            )
        except (GoogleAuthError, OSError, ValueError):
            invalid_sources.append(source)

    if invalid_sources:
        return AdcInspection(
            available=False,
            message="检测到 ADC 文件，但凭据格式无效",
            source=invalid_sources[0],
        )
    return AdcInspection(available=False, message="未找到有效 ADC 凭据文件")


def _provider_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase or "供应商返回了无法解析的响应"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return response.reason_phrase or "供应商拒绝了连接请求"


async def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str | int] | None = None,
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.RequestError as exc:
        raise ModelCatalogError(f"无法连接模型服务：{exc.__class__.__name__}") from exc

    if not response.is_success:
        raise ModelCatalogError(
            f"连接失败（HTTP {response.status_code}）：{_provider_error(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelCatalogError("模型服务返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise ModelCatalogError("模型服务返回了不支持的数据格式")
    return payload


async def _list_openai_models(config: ModelConnectionConfig) -> list[str]:
    if not config.api_key.strip():
        raise ModelCatalogError("请填写 API Key")
    if not config.base_url.strip():
        raise ModelCatalogError("请填写 Base URL")

    payload = await _request_json(
        f"{config.base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {config.api_key.strip()}"},
    )
    data = payload.get("data")
    if not isinstance(data, list):
        raise ModelCatalogError("该 OpenAI 兼容接口未返回标准模型列表")
    return sorted(
        {
            item["id"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )


async def _list_gemini_models(config: ModelConnectionConfig) -> list[str]:
    if not config.api_key.strip():
        raise ModelCatalogError("请填写 API Key")

    payload = await _request_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": config.api_key.strip(), "pageSize": 1000},
    )
    models = payload.get("models")
    if not isinstance(models, list):
        raise ModelCatalogError("Gemini API 未返回模型列表")

    model_ids: set[str] = set()
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        methods = item.get("supportedGenerationMethods", [])
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        model_ids.add(item["name"].removeprefix("models/"))
    return sorted(model_ids)


def _list_vertex_models_sync(config: ModelConnectionConfig) -> list[str]:
    project_id = config.project_id.strip()
    location = config.location.strip() or "global"
    client: genai.Client | None = None
    try:
        if config.auth_mode == "adc":
            adc = inspect_adc_file()
            if not adc.available or not adc.path:
                raise ModelCatalogError(adc.message)
            credentials, detected_project = load_credentials_from_file(
                str(adc.path), scopes=[GOOGLE_CLOUD_SCOPE]
            )
            project_id = project_id or detected_project or ""
            if not project_id:
                raise ModelCatalogError("ADC 中未包含 Project ID，请手动填写")
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location,
                credentials=credentials,
            )
        else:
            if not config.api_key.strip():
                raise ModelCatalogError("请填写 API Key")
            client = genai.Client(
                vertexai=True,
                api_key=config.api_key.strip(),
            )

        model_ids: set[str] = set()
        for model in client.models.list(config={"page_size": 300, "query_base": True}):
            if not model.name:
                continue
            model_id = model.name.rsplit("/", 1)[-1].split("@", 1)[0]
            if model_id.startswith("gemini-"):
                model_ids.add(model_id)
        return sorted(model_ids)
    except ModelCatalogError:
        raise
    except Exception as exc:
        # SDK 异常往往包含认证或 IAM 细节；保留可操作信息，但不回传凭据内容。
        message = (
            str(exc).replace(config.api_key, "***") if config.api_key else str(exc)
        )
        raise ModelCatalogError(f"Vertex AI 连接失败：{message}") from exc
    finally:
        if client:
            client.close()


async def list_available_models(config: ModelConnectionConfig) -> list[str]:
    """连接对应供应商并返回可用于对话生成的模型 ID。"""

    if config.provider == "openai":
        models = await _list_openai_models(config)
    elif config.provider == "gemini_studio":
        models = await _list_gemini_models(config)
    else:
        models = await asyncio.to_thread(_list_vertex_models_sync, config)

    if not models:
        raise ModelCatalogError("连接成功，但没有找到可用的生成模型")
    return models
