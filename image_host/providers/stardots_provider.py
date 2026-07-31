import hashlib
import json
import logging
import random
import string
import time
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote, urlencode

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..interfaces.image_host import ImageHostInterface

logger = logging.getLogger(__name__)


class StarDotsError(Exception):
    """StarDots 相关错误的基类"""

    pass


class AuthenticationError(StarDotsError):
    """认证错误"""

    pass


class NetworkError(StarDotsError):
    """网络错误"""

    pass


class RateLimitError(StarDotsError):
    """调用频率超限错误"""

    pass


class InvalidResponseError(StarDotsError):
    """响应格式错误"""

    pass


class ImageInfo(TypedDict):
    url: str
    id: str
    filename: str
    category: str
    size: int | None


class StarDotsProvider(ImageHostInterface):
    """StarDots图床提供者实现"""

    BASE_URL = "https://api.stardots.io"
    CATEGORY_SEPARATOR = "@@CAT@@"
    DEFAULT_CATEGORY = "default"
    MIME_TYPES = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }

    def __init__(self, config: dict[str, str]):
        """
        初始化StarDots图床

        Args:
            config: {
                'key': 'your_key',
                'secret': 'your_secret',
                'space': 'your_space_name'
            }
        """
        required_fields = {"key", "secret", "space"}
        missing_fields = required_fields - set(config.keys())
        if missing_fields:
            raise ValueError(f"Missing required config fields: {missing_fields}")
        self.config = config
        self.key = config["key"]
        self.secret = config["secret"]
        self.space = config["space"]
        self.base_url = self.BASE_URL
        self.server_time_offset = 0  # 服务器时间偏移量
        self.list_cache_ttl = int(config.get("list_cache_ttl", 60) or 60)
        self._image_list_cache: dict[str, object] | None = None

        # 禁用SSL警告
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # 配置请求会话
        self.session = requests.Session()
        self.session.verify = False  # 禁用SSL验证

        # 配置重试策略
        retry_strategy = Retry(
            total=3,  # 最大重试次数
            backoff_factor=1,  # 重试间隔
            status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的HTTP状态码
        )

        # 配置适配器
        adapter = HTTPAdapter(
            max_retries=retry_strategy, pool_connections=10, pool_maxsize=10
        )

        # 将适配器应用到会话
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self._sync_server_time()  # 初始化时同步服务器时间
        self.records_file = Path("category_records.json")
        self._load_records()  # 加载分类记录

    def _sync_server_time(self) -> None:
        """同步服务器时间"""
        try:
            # 使用任意API请求来获取服务器时间
            response = requests.get(f"{self.base_url}/openapi/space/list")
            if response.status_code == 200:
                result = response.json()
                server_ts = result.get("ts", 0) // 1000  # 转换为秒
                local_ts = int(time.time())
                self.server_time_offset = server_ts - local_ts
        except Exception:
            self.server_time_offset = 8 * 3600  # 如果失败，使用默认的 UTC+8

    def _generate_headers(self) -> dict[str, str]:
        """生成请求头"""
        # 使用服务器时间偏移量生成时间戳
        timestamp = str(int(time.time() + self.server_time_offset))
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=10))

        # 生成签名
        sign_str = f"{timestamp}|{self.secret}|{nonce}"
        sign = hashlib.md5(sign_str.encode()).hexdigest().upper()

        return {
            "x-stardots-timestamp": timestamp,
            "x-stardots-nonce": nonce,
            "x-stardots-key": self.key,
            "x-stardots-sign": sign,
            "Content-Type": "application/json",
        }

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """统一的请求处理方法"""
        try:
            # 添加默认超时
            kwargs.setdefault("timeout", 30)

            # 添加SSL验证选项
            kwargs.setdefault("verify", True)

            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.SSLError:
            # SSL错误，尝试禁用验证
            kwargs["verify"] = False
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise Exception(f"Request failed: {str(e)}")

    def _load_records(self):
        """从文件加载分类记录"""
        try:
            if self.records_file.exists():
                with open(self.records_file, encoding="utf-8") as f:
                    self._upload_records = json.load(f)
            else:
                self._upload_records = {}
        except Exception:
            self._upload_records = {}

    def _save_records(self):
        """保存分类记录到文件"""
        try:
            with open(self.records_file, "w", encoding="utf-8") as f:
                json.dump(self._upload_records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存分类记录失败: {str(e)}")

    def _encode_category(self, category: str) -> str:
        """将分类路径编码到文件名中"""
        if not category or category == ".":
            return ""
        return category.replace("/", "@@DIR@@").replace("\\", "@@DIR@@")

    def _decode_category(self, encoded: str) -> str:
        """从编码的文件名中解码分类路径"""
        if not encoded:
            return self.DEFAULT_CATEGORY
        return encoded.replace("@@DIR@@", "/")

    def _extract_image_size(self, image_info: dict) -> int | None:
        """尽量从 StarDots 返回数据中提取文件大小。"""
        candidate_keys = ("size", "fileSize", "file_size", "bytes", "length")
        for key in candidate_keys:
            value = image_info.get(key)
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    @staticmethod
    def _is_rate_limit_error(message: str) -> bool:
        lowered = str(message or "").lower()
        keywords = (
            "exceed times limit",
            "rate limit",
            "too many requests",
            "请求频率",
            "调用频次",
            "调用次数",
        )
        return any(keyword in lowered for keyword in keywords)

    def _invalidate_image_list_cache(self) -> None:
        self._image_list_cache = None

    def upload_image(self, file_path: Path) -> ImageInfo:
        """上传图片到StarDots"""
        max_retries = 3
        retry_delay = 2  # 增加重试间隔为2秒

        for attempt in range(max_retries):
            try:
                # 每次尝试前重新同步时间
                self._sync_server_time()
                headers = self._generate_headers()
                headers.pop("Content-Type")  # 上传文件需要移除Content-Type

                # 获取文件信息
                mime_type = self.MIME_TYPES.get(file_path.suffix.lower(), "image/jpeg")

                # 获取相对路径作为分类
                base_dir = Path(self.config.get("local_dir", ""))
                try:
                    rel_path = file_path.relative_to(base_dir)
                except ValueError:
                    rel_path = file_path.name

                category = str(rel_path.parent).replace("\\", "/")
                if category == ".":
                    category = ""

                encoded_category = self._encode_category(category)
                remote_filename = (
                    f"{encoded_category}@@CAT@@{rel_path.name}"
                    if encoded_category
                    else rel_path.name
                )

                logger.debug(f"上传文件: {file_path}")
                logger.info(f"开始上传: {remote_filename}")

                with open(file_path, "rb") as f:
                    files = {
                        "file": (remote_filename, f, mime_type),
                        "space": (None, self.space),
                    }

                    # 使用 PUT 方法上传
                    response = requests.put(
                        f"{self.base_url}/openapi/file/upload",
                        headers=headers,
                        files=files,
                        verify=False,  # 禁用 SSL 验证
                        timeout=60,  # 增加超时时间
                    )

                    if response.status_code == 200:
                        result = response.json()
                        if result["success"]:
                            self._invalidate_image_list_cache()
                            logger.info(f"上传成功 URL: {result['data']['url']}")
                            return {
                                "url": result["data"]["url"],
                                "id": str(rel_path),
                                "filename": rel_path.name,
                                "category": category,
                            }
                    else:
                        error_msg = f"HTTP {response.status_code}"
                        try:
                            error_msg = response.json().get("message", error_msg)
                        except Exception:
                            pass
                        logger.error(f"上传失败: {error_msg}")
                        raise Exception(error_msg)

            except requests.exceptions.RequestException as e:
                logger.error(f"网络错误: {str(e)}，重试中...")
                time.sleep(retry_delay)
                continue
            except Exception as e:
                logger.error(f"上传异常: {str(e)}，重试中...")
                time.sleep(retry_delay)
                continue

        raise Exception(f"Upload failed after {max_retries} retries")

    def delete_image(self, image_id: str) -> bool:
        """从StarDots删除图片"""
        headers = self._generate_headers()

        data = {"space": self.space, "filenameList": [image_id]}

        response = requests.delete(
            f"{self.base_url}/openapi/file/delete", headers=headers, json=data
        )

        if response.status_code == 200:
            result = response.json()
            success = bool(result["success"])
            if success:
                self._invalidate_image_list_cache()
            return success
        return False

    def get_image_list(self) -> list[ImageInfo]:
        """获取StarDots空间中的所有图片"""
        if self._image_list_cache:
            cached_at = float(self._image_list_cache.get("cached_at", 0.0))
            cached_images = self._image_list_cache.get("images")
            if (
                isinstance(cached_images, list)
                and self.list_cache_ttl > 0
                and (time.time() - cached_at) < self.list_cache_ttl
            ):
                return list(cached_images)

        max_retries = 2
        retry_delay = 2
        page = 1
        page_size = 100
        max_pages = 1000
        all_images: list[ImageInfo] = []

        while page <= max_pages:
            page_fetched = False
            last_error: Exception | None = None
            for attempt in range(max_retries):
                try:
                    # 每次请求前重新同步时间
                    self._sync_server_time()
                    headers = self._generate_headers()  # 每次请求生成新的headers
                    params = {"space": self.space, "page": page, "pageSize": page_size}
                    response = self._make_request(
                        "get",
                        f"{self.base_url}/openapi/file/list",
                        headers=headers,
                        params=params,
                        verify=False,
                    )

                    if response.status_code == 200:
                        result = response.json()
                        if result["success"]:
                            data = result["data"]
                            images = data["list"]
                            if not images:  # 如果没有更多图片了
                                self._image_list_cache = {
                                    "cached_at": time.time(),
                                    "images": list(all_images),
                                }
                                return all_images

                            # 处理图片列表
                            for img in images:
                                # 从文件名中提取分类信息
                                filename = img["name"]
                                if "@@CAT@@" in filename:
                                    encoded_category, name = filename.split(
                                        "@@CAT@@", 1
                                    )
                                    category = self._decode_category(encoded_category)
                                    file_id = f"{category}/{name}" if category else name
                                else:
                                    category = ""
                                    name = filename
                                    file_id = name

                                file_id = file_id.replace("\\", "/")

                                all_images.append(
                                    {
                                        "url": img["url"],
                                        "id": file_id,
                                        "filename": name,
                                        "category": category,
                                        "size": self._extract_image_size(img),
                                    }
                                )

                            # 如果返回的图片数量小于页大小，说明是最后一页
                            if len(images) < page_size:
                                self._image_list_cache = {
                                    "cached_at": time.time(),
                                    "images": list(all_images),
                                }
                                return all_images

                            page += 1  # 获取下一页
                            page_fetched = True
                            break  # 成功获取数据，跳出重试循环
                        else:
                            error_message = result.get("message", "未知错误")
                            lowered = error_message.lower()
                            if (
                                "invalid timestamp" in lowered
                                or "invalid nonce" in lowered
                            ):
                                if attempt < max_retries - 1:
                                    logger.warning("StarDots 鉴权字段异常，准备重试")
                                    time.sleep(retry_delay)
                                    continue
                                last_error = AuthenticationError(error_message)
                                break

                            if self._is_rate_limit_error(error_message):
                                last_error = RateLimitError(
                                    "StarDots API 调用频次超限，请稍后重试"
                                )
                                break

                            last_error = InvalidResponseError(
                                f"获取图片列表失败: {error_message}"
                            )
                            if attempt < max_retries - 1:
                                logger.warning(
                                    "获取图片列表失败（第 %s/%s 次重试）: %s",
                                    attempt + 1,
                                    max_retries,
                                    error_message,
                                )
                                time.sleep(retry_delay)
                                continue
                            break

                    else:
                        if response.status_code == 429:
                            last_error = RateLimitError(
                                "StarDots API 调用频次超限，请稍后重试"
                            )
                            break
                        if attempt < max_retries - 1:
                            logger.warning(
                                "获取图片列表 HTTP 错误（第 %s/%s 次重试）: %s",
                                attempt + 1,
                                max_retries,
                                response.status_code,
                            )
                            time.sleep(retry_delay)
                            continue
                        last_error = NetworkError(
                            f"Failed to get image list: {response.text}"
                        )
                        break

                except Exception as e:
                    if isinstance(e, RateLimitError):
                        last_error = e
                        break
                    if attempt < max_retries - 1:
                        logger.warning(
                            "获取图片列表网络异常（第 %s/%s 次重试）: %s",
                            attempt + 1,
                            max_retries,
                            str(e),
                        )
                        time.sleep(retry_delay)
                        continue
                    last_error = NetworkError(f"获取远程文件列表失败: {str(e)}")
                    break

            if page_fetched:
                continue

            if isinstance(last_error, RateLimitError):
                if all_images:
                    logger.warning(
                        "StarDots 限流，返回已获取的 %s 条远程记录", len(all_images)
                    )
                    self._image_list_cache = {
                        "cached_at": time.time(),
                        "images": list(all_images),
                    }
                    return all_images
                raise last_error

            if last_error is not None:
                if all_images:
                    logger.warning(
                        "分页中断，返回已获取的 %s 条远程记录", len(all_images)
                    )
                    self._image_list_cache = {
                        "cached_at": time.time(),
                        "images": list(all_images),
                    }
                    return all_images
                raise last_error

            break

        return all_images

    def download_image(self, image_info: dict[str, str], save_path: Path) -> bool:
        """从StarDots下载图片"""
        max_retries = 3
        retry_delay = 1  # 秒
        temp_path = save_path.with_suffix(".tmp")

        for attempt in range(max_retries):
            try:
                # 每次尝试前重新同步时间
                self._sync_server_time()
                headers = self._generate_headers()

                # 从文件名中提取原始文件名（包含分类）
                encoded_category = self._encode_category(image_info["category"])
                original_name = (
                    f"{encoded_category}@@CAT@@{image_info['filename']}"  # 使用 @@CAT@@ 作为分隔符
                    if encoded_category
                    else image_info["filename"]
                )

                data = {
                    "space": self.space,
                    "filename": original_name,
                }

                # 获取临时访问票据
                ticket_response = self._make_request(
                    "post",
                    f"{self.base_url}/openapi/file/ticket",
                    headers=headers,
                    json=data,
                )

                if ticket_response.status_code == 200:
                    ticket_result = ticket_response.json()
                    if ticket_result["success"]:
                        # 构建正确的下载 URL
                        encoded_space = quote(self.space, safe="")
                        encoded_name = quote(original_name, safe="/")
                        base_url = (
                            f"https://i.stardots.io/{encoded_space}/{encoded_name}"
                        )
                        query = urlencode({"ticket": ticket_result["data"]["ticket"]})
                        url = f"{base_url}?{query}"

                        # 下载文件
                        response = requests.get(url, stream=True, verify=False)

                        # 检查响应头
                        content_type = response.headers.get("Content-Type", "")
                        content_length = response.headers.get("Content-Length", 0)
                        logger.debug(f"响应类型: {content_type}")
                        logger.debug(f"文件大小: {content_length} bytes")

                        if response.status_code == 200 and "image/" in content_type:
                            # 确保目标目录存在
                            save_path.parent.mkdir(parents=True, exist_ok=True)

                            try:
                                # 下载到临时文件
                                with open(temp_path, "wb") as f:
                                    for chunk in response.iter_content(chunk_size=8192):
                                        if chunk:  # 过滤掉保持活动的新块
                                            f.write(chunk)

                                # 验证文件大小
                                if temp_path.stat().st_size > 1000:  # 确保文件大小正常
                                    temp_path.replace(save_path)  # 原子操作
                                    return True
                                else:
                                    logger.error(
                                        f"下载的文件太小: {temp_path.stat().st_size} bytes"
                                    )
                            finally:
                                # 如果还存在临时文件就删除
                                if temp_path.exists():
                                    temp_path.unlink()
                        else:
                            logger.error(f"下载失败，状态码: {response.status_code}")
                            logger.error(f"响应内容: {response.text[:200]}")
                    else:
                        error_msg = ticket_result.get("message", "未知错误")
                        logger.error(f"获取票据失败: {error_msg}")
                else:
                    logger.error(f"票据请求失败，状态码: {ticket_response.status_code}")

                if attempt < max_retries - 1:
                    logger.warning(f"下载失败，重试中: {original_name}")
                    time.sleep(retry_delay)
                    continue

            except Exception as e:
                logger.error(f"下载异常: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return False

        return False
