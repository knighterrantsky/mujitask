#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu Bitable API helpers."""

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

BASE_URL = "https://base-api.feishu.cn/open-apis/bitable/v1"
DRIVE_URL = "https://base-api.feishu.cn/open-apis/drive/v1"


@dataclass
class FeishuAPIError(Exception):
    message: str
    code: Optional[int] = None
    status: Optional[int] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        details = []
        if self.code is not None:
            details.append(f"code={self.code}")
        if self.status is not None:
            details.append(f"status={self.status}")
        if details:
            return f"{self.message} ({', '.join(details)})"
        return self.message


def parse_table_url(table_url: str) -> Dict[str, str]:
    parsed = urlparse(table_url)
    path_parts = [p for p in parsed.path.split("/") if p]

    app_token = ""
    if "base" in path_parts:
        base_index = path_parts.index("base")
        if base_index + 1 < len(path_parts):
            app_token = path_parts[base_index + 1]
    elif path_parts:
        app_token = path_parts[-1]

    query = parse_qs(parsed.query)
    table_id = query.get("table", [""])[0]

    if not app_token or not table_id:
        raise ValueError("Invalid table_url, missing app_token or table_id")

    return {"app_token": app_token, "table_id": table_id}


class FeishuBitableClient:
    def __init__(self, access_token: str, timeout: int = 30) -> None:
        self.access_token = access_token
        self.timeout = timeout
        self.session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        url = f"{BASE_URL}{path}"
        for attempt in range(retries):
            response = self.session.request(
                method=method,
                url=url,
                headers=self._headers(),
                params=params,
                json=payload,
                timeout=self.timeout,
            )

            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            try:
                data = response.json()
            except ValueError:
                raise FeishuAPIError("Invalid JSON response", status=response.status_code)

            if response.status_code >= 400:
                raise FeishuAPIError(
                    data.get("msg", "Request failed"),
                    code=data.get("code"),
                    status=response.status_code,
                )

            if data.get("code", 0) != 0:
                raise FeishuAPIError(
                    data.get("msg", "Feishu API error"),
                    code=data.get("code"),
                    status=response.status_code,
                )

            return data

        raise FeishuAPIError("Request failed after retries")

    def list_records(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 20,
        filter_expr: Optional[str] = None,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page_size": page_size}
        if filter_expr:
            params["filter"] = filter_expr
        if page_token:
            params["page_token"] = page_token
        return self._request(
            "GET",
            f"/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )

    def list_all_records(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 100,
        filter_expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            data = self.list_records(
                app_token,
                table_id,
                page_size=page_size,
                filter_expr=filter_expr,
                page_token=page_token,
            )
            payload = data.get("data", {})
            items.extend(payload.get("items", []))
            if not payload.get("has_more"):
                break
            page_token = payload.get("page_token")
            if not page_token:
                break

        return items

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"fields": fields}
        return self._request(
            "PUT",
            f"/apps/{app_token}/tables/{table_id}/records/{record_id}",
            payload=payload,
        )

    def upload_media(
        self,
        file_name: str,
        file_data: bytes,
        parent_type: str = "bitable_file",
        parent_node: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        """上传素材到飞书，返回 file_token。

        Args:
            file_name: 文件名（带扩展名）
            file_data: 文件二进制数据
            parent_type: 上传文件的类型，多维表格附件使用 "bitable_file"
            parent_node: 父节点token，多维表格附件传 app_token
            extra: 额外参数

        Returns:
            file_token: 上传成功后的文件token
        """
        url = f"{DRIVE_URL}/medias/upload_all"

        # 构建 multipart/form-data
        files = {
            "file": (file_name, file_data),
        }
        data = {
            "file_name": file_name,
            "parent_type": parent_type,
            "parent_node": parent_node,
            "size": str(len(file_data)),
        }
        if extra:
            import json
            data["extra"] = json.dumps(extra)

        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        for attempt in range(3):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=60,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise FeishuAPIError(f"Upload connection error: {str(e)}")

            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            try:
                result = response.json()
            except ValueError:
                raise FeishuAPIError("Invalid JSON response", status=response.status_code)

            if response.status_code >= 400:
                raise FeishuAPIError(
                    result.get("msg", "Upload failed"),
                    code=result.get("code"),
                    status=response.status_code,
                )

            if result.get("code", 0) != 0:
                raise FeishuAPIError(
                    result.get("msg", "Upload failed"),
                    code=result.get("code"),
                    status=response.status_code,
                )

            return result.get("data", {}).get("file_token", "")

        raise FeishuAPIError("Upload failed after retries")
