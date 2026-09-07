"""
mineru_api.py - MinerU REST API 的纯 HTTP 层

三个函数，各对应一个 API 操作，不含业务逻辑。
"""

import time
import requests

_BASE = "https://mineru.net/api/v4"
_POLL_INTERVAL = 5     # 轮询间隔（秒）
_POLL_TIMEOUT  = 900   # 最长等待 15 分钟

# 429 限流退避：最多重试次数、初始等待、最长单次等待
_RATE_MAX_RETRIES = 6
_RATE_INITIAL_WAIT = 5
_RATE_MAX_WAIT = 60


def _is_rate_limited(resp: requests.Response) -> bool:
    return resp.status_code == 429


def _retry_on_rate_limit(fn, op_name: str = "MinerU API"):
    """
    对 429 Too Many Requests 做指数退避重试。
    fn: 无参可调用，返回 requests.Response。
    """
    wait = _RATE_INITIAL_WAIT
    for attempt in range(_RATE_MAX_RETRIES + 1):
        resp = fn()
        if not _is_rate_limited(resp):
            return resp
        if attempt == _RATE_MAX_RETRIES:
            break
        # 优先采用服务器 Retry-After 头（秒），否则用指数退避
        retry_after = resp.headers.get("Retry-After")
        try:
            sleep_for = min(float(retry_after), _RATE_MAX_WAIT) if retry_after else wait
        except (TypeError, ValueError):
            sleep_for = wait
        time.sleep(sleep_for)
        wait = min(wait * 2, _RATE_MAX_WAIT)
    return resp  # 最后一次仍 429，交由调用方 raise_for_status 抛出


def request_upload_urls(
    files_meta: list[dict],
    model_version: str,
    extra_formats: list[str],
    enable_formula: bool,
    enable_table: bool,
    language: str,
    token: str,
) -> tuple[str, list[str]]:
    """
    POST /v4/file-urls/batch — 申请预签名上传链接。

    Returns:
        (batch_id, [presigned_url, ...])
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body: dict = {
        "files": files_meta,
        "model_version": model_version,
        "enable_formula": enable_formula,
        "enable_table":   enable_table,
        "language":       language,
    }
    if extra_formats:
        body["extra_formats"] = extra_formats

    resp = _retry_on_rate_limit(
        lambda: requests.post(f"{_BASE}/file-urls/batch", headers=headers, json=body, timeout=30),
        op_name="申请上传链接",
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"申请链接失败：{result.get('msg', result)}")
    return result["data"]["batch_id"], result["data"]["file_urls"]


def upload_file(presigned_url: str, file_path: str) -> None:
    """
    PUT 上传文件到 OSS 预签名地址。
    注意：上传时不需要设置 Content-Type。
    """
    with open(file_path, "rb") as f:
        resp = requests.put(presigned_url, data=f, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"上传失败 HTTP {resp.status_code}")


def poll_batch(
    batch_id: str,
    token: str,
    stop_checker=None,
    log=None,
) -> list[dict]:
    """
    GET /v4/extract-results/batch/{batch_id}
    阻塞轮询，直到所有文件完成（done/failed）或超时。

    Returns:
        extract_result 列表，每项对应一个文件的解析结果

    Raises:
        InterruptedError: 外部请求停止
        TimeoutError:     超过 _POLL_TIMEOUT 秒
    """
    headers  = {"Authorization": f"Bearer {token}"}
    url      = f"{_BASE}/extract-results/batch/{batch_id}"
    deadline = time.time() + _POLL_TIMEOUT
    last_done = -1

    while time.time() < deadline:
        if stop_checker and stop_checker():
            raise InterruptedError("已中断")

        resp = _retry_on_rate_limit(
            lambda: requests.get(url, headers=headers, timeout=30),
            op_name="轮询解析结果",
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"查询失败：{data.get('msg', data)}")

        results  = data["data"].get("extract_result", [])
        done     = sum(1 for r in results if r.get("state") in ("done", "failed"))
        if log and done != last_done:
            log(f"  MinerU 进度：{done}/{len(results)}")
            last_done = done
        if results and done == len(results):
            return results

        time.sleep(_POLL_INTERVAL)

    # 超时前做最后一次查询，返回已完成的部分结果（避免丢弃已成功的文件）
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            results = data["data"].get("extract_result", [])
            done = sum(1 for r in results if r.get("state") in ("done", "failed"))
            if results and done > 0:
                if log:
                    log(f"  MinerU 超时（{_POLL_TIMEOUT}s），返回已完成的部分结果：{done}/{len(results)}")
                return results
    except Exception:
        pass  # 最后查询也失败，抛原始超时

    raise TimeoutError(f"MinerU 解析超时（{_POLL_TIMEOUT}s），batch_id={batch_id}")
