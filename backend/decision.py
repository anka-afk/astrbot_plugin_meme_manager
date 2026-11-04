from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from urllib.parse import urlsplit

import aiohttp

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUESTIONS = 32
MAX_OPTIONS = 255

CLOUDFLARE_BASE_URL = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_MODELS = {
    "clef-flash": "Clef Flash · 快速 9B",
    "clef": "Clef · 27B",
    "clef-omni": "Clef Omni · 音视频输入",
}
CLOUDFLARE_MAX_QUESTIONS = 64
CLOUDFLARE_REQUEST_BYTES = 13 * 1024 * 1024
CLOUDFLARE_ACCOUNT_ID = re.compile(r"^[0-9a-fA-F]{32}$")
CLOUDFLARE_QUESTION_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
# 仅映射可稳定识别的错误码，其余按 HTTP 状态处理，避免暴露上游细节。
CLOUDFLARE_ERROR_CODES = {
    10000: "unauthorized",
    2021: "insufficient_credits",
}


class DecisionError(Exception):
    """提供不含凭据或上游详情的稳定错误码。"""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class DecisionAnswers(dict):
    """保留每次请求的元信息，不向答案映射添加额外键。"""

    def __init__(self, answers, *, model="", usage=None, credits=None):
        super().__init__(answers)
        self.model = model
        self.usage = usage
        self.credits = credits


class DecisionClient:
    """为独立任务共享有界传输，统一 System One 与 Cloudflare 的决策契约。"""

    def __init__(self, config: dict):
        """初始化配置，不建立会话或发起推理。

        Args:
            config: 提供方、服务地址或账户 ID、密钥、模型、超时和并发设置。
        """
        values = config if isinstance(config, dict) else {}
        provider = str(values.get("provider") or "systemone").strip().casefold()
        supported = {"systemone", "cloudflare"}
        self.provider = provider if provider in supported else "systemone"
        self._provider_valid = provider == self.provider
        self.account_id = str(values.get("account_id") or "").strip()
        self.base_url = (
            str(values.get("base_url", "https://system-one.dev/v1") or "")
            .strip()
            .rstrip("/")
        )
        self.token = str(values.get("token") or "").strip()
        self.model = str(values.get("model") or "").strip()
        self._choice_min = 2 if self.provider == "cloudflare" else 1
        self.timeout, self._timeout_valid = self._read_timeout(
            values.get("timeout", 15)
        )
        self.max_concurrency, self._concurrency_valid = self._read_concurrency(
            values.get("max_concurrency", 2)
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._closed = False
        self.request_counts: dict[str, int] = {}

    @staticmethod
    def _read_timeout(value) -> tuple[float, bool]:
        try:
            if isinstance(value, bool):
                raise ValueError
            result = float(value)
        except (TypeError, ValueError):
            return 15.0, False
        valid = math.isfinite(result) and 0 < result <= 300
        return (result if valid else 15.0), valid

    @staticmethod
    def _read_concurrency(value) -> tuple[int, bool]:
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
        except (TypeError, ValueError):
            return 2, False
        valid = math.isfinite(number) and number.is_integer() and 1 <= number <= 64
        return (int(number) if valid else 2), valid

    def _base_url_valid(self) -> bool:
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and (port is None or port > 0)
                and not any(char.isspace() or ord(char) < 32 for char in self.base_url)
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    def _reason(self) -> str:
        if self._closed:
            return "closed"
        if not self._provider_valid:
            return "invalid_provider"
        if not self.token:
            return "missing_token"
        if any(ord(char) < 33 or ord(char) == 127 for char in self.token):
            return "invalid_token"
        if self.provider == "cloudflare":
            if not self.account_id:
                return "missing_account_id"
            if not CLOUDFLARE_ACCOUNT_ID.fullmatch(self.account_id):
                return "invalid_account_id"
            if not self.model:
                return "missing_model"
            if self.model not in CLOUDFLARE_MODELS:
                return "unsupported_model"
        else:
            if not self.base_url:
                return "missing_base_url"
            if not self._base_url_valid():
                return "invalid_base_url"
            if not self.model:
                return "missing_model"
            if len(self.model) > 128 or any(
                ord(char) < 32 or ord(char) == 127 for char in self.model
            ):
                return "invalid_model"
        if not self._timeout_valid:
            return "invalid_timeout"
        if not self._concurrency_valid:
            return "invalid_max_concurrency"
        return "ready"

    @property
    def ready(self) -> bool:
        """本地配置是否有效且客户端是否处于打开状态。"""
        return self._reason() == "ready"

    def status(self) -> dict:
        """仅返回本地就绪状态，不发起请求或泄露密钥。

        Returns:
            已隐藏敏感信息的服务配置状态，不保证网络连通性。
        """
        reason = self._reason()
        return {
            "ready": reason == "ready",
            "reason": reason,
            "provider": self.provider,
            "backend": self.provider,
            "model": self.model,
            "requests_started": dict(self.request_counts),
        }

    def _endpoint(self) -> str:
        if self.provider == "cloudflare":
            return (
                f"{CLOUDFLARE_BASE_URL}/accounts/{self.account_id}"
                f"/ai/run/@cf/cloudflare/{self.model}"
            )
        if self.base_url.endswith("/systemone"):
            return self.base_url
        return f"{self.base_url}/systemone"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise DecisionError("closed")
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._closed:
                raise DecisionError("closed")
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def list_models(self) -> list[dict[str, str]]:
        """读取模型候选，不发送凭据或发起推理。

        Returns:
            按目录顺序排列的模型 ID 和名称。Cloudflare 直接返回本地固定目录。

        Raises:
            DecisionError: 地址、连接、响应大小或目录格式无效。
            asyncio.CancelledError: 模型发现请求被取消。
        """
        if not self._provider_valid:
            raise DecisionError("invalid_provider")
        if self.provider == "cloudflare":
            return [
                {"id": model, "name": name} for model, name in CLOUDFLARE_MODELS.items()
            ]
        if not self._base_url_valid():
            raise DecisionError("invalid_base_url")
        base_url = self.base_url.removesuffix("/systemone")
        try:
            session = await self._get_session()
            async with session.get(
                f"{base_url}/models",
                headers={"Accept": "application/json"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=min(self.timeout, 10)),
            ) as response:
                if not 200 <= response.status <= 299:
                    raise self._http_error(response.status)
                raw = await self._read_bounded(response)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise DecisionError("timeout") from None
        except aiohttp.ClientError:
            raise DecisionError("network_error") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise DecisionError("invalid_catalog") from None
        entries = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or len(entries) > 1000:
            raise DecisionError("invalid_catalog")
        models = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            capabilities = entry.get("capabilities")
            question_types = (
                capabilities.get("questions")
                if isinstance(capabilities, dict)
                else None
            )
            if isinstance(question_types, list) and "choice" not in question_types:
                continue
            model_id = entry.get("name")
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or model_id != model_id.strip()
                or len(model_id) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in model_id)
                or model_id in seen
            ):
                continue
            seen.add(model_id)
            name = entry.get("displayName")
            models.append(
                {
                    "id": model_id,
                    "name": name[:200] if isinstance(name, str) else model_id,
                }
            )
        return models

    def _question_options(self, question: dict) -> list[str]:
        """校验文档规定的问题条件，并返回概率分布键。

        Args:
            question: 包含固定指令的 Choice、Noul 或 Score 问题。

        Returns:
            所选基础问题类型允许使用的概率键。

        Raises:
            DecisionError: 请求不符合提供方的协议约定。
        """
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict)
                or any(key not in {"true", "false"} for key in criteria)
            ):
                raise DecisionError("invalid_request")
            return ["true", "false"]
        if kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise DecisionError("invalid_request")
            return [str(index) for index in range(len(criteria))]
        if kind != "choice" or not isinstance(criteria, dict):
            raise DecisionError("invalid_request")
        options = list(criteria)
        if not self._choice_min <= len(options) <= MAX_OPTIONS:
            raise DecisionError("invalid_request")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 128
            or any(ord(char) < 32 for char in item)
            for item in options
        ):
            raise DecisionError("invalid_request")
        return options

    def _encode_request(self, state, questions: dict) -> bytes:
        if not isinstance(state, (str, dict, list)) or not isinstance(questions, dict):
            raise DecisionError("invalid_request")
        max_questions = (
            CLOUDFLARE_MAX_QUESTIONS if self.provider == "cloudflare" else MAX_QUESTIONS
        )
        if not 1 <= len(questions) <= max_questions:
            raise DecisionError("invalid_request")
        for name, question in questions.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 128
                or not isinstance(question, dict)
            ):
                raise DecisionError("invalid_request")
            if self.provider == "cloudflare" and not CLOUDFLARE_QUESTION_NAME.fullmatch(
                name
            ):
                raise DecisionError("invalid_request")
            self._question_options(question)
        try:
            body = json.dumps(
                {"model": self.model, "state": state, "questions": questions},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise DecisionError("invalid_request") from None
        if len(body) > (
            CLOUDFLARE_REQUEST_BYTES
            if self.provider == "cloudflare"
            else MAX_REQUEST_BYTES
        ):
            raise DecisionError("request_too_large")
        return body

    @staticmethod
    def _probability(value) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise DecisionError("invalid_probability")

    @classmethod
    def _validate_answer_numbers(cls, answer: dict, options: list[str]) -> None:
        for key in ("probability", "confidence"):
            if key in answer:
                cls._probability(answer[key])
        for key in ("probabilities", "probability_distribution"):
            if key not in answer:
                continue
            distribution = answer[key]
            if not isinstance(distribution, dict) or not distribution:
                raise DecisionError("invalid_probability")
            if any(option not in options for option in distribution):
                raise DecisionError("invalid_response")
            for value in distribution.values():
                cls._probability(value)

    def _validate_answers(self, questions: dict, answers) -> dict:
        """在提供任何部分结果前校验完整答案集合。

        Args:
            questions: 实际发送给服务的问题。
            answers: 服务返回的答案对象。

        Returns:
            已校验的答案，并完整保留可选统计信息。

        Raises:
            DecisionError: 任一答案、类型、选择或概率无效。
        """
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise DecisionError("invalid_response")
        for name, question in questions.items():
            answer = answers[name]
            if not isinstance(answer, dict):
                raise DecisionError("invalid_response")
            kind = question["type"]
            if answer.get("type") != kind:
                raise DecisionError("invalid_response")
            options = self._question_options(question)
            if kind == "choice":
                if answer.get("choice") not in options:
                    raise DecisionError("invalid_response")
            elif kind == "noul":
                self._probability(answer.get("noul"))
            else:
                value = answer.get("score")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= len(options) - 1
                ):
                    raise DecisionError("invalid_response")
            self._validate_answer_numbers(answer, options)
        return answers

    @staticmethod
    async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
        """读取全部响应分块，并限制总字节数。

        Args:
            response: 成功的 HTTP 响应，可能采用分块传输。

        Returns:
            完整的响应字节。

        Raises:
            DecisionError: 完整响应超出允许的大小。
        """
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_RESPONSE_BYTES:
                    raise DecisionError("response_too_large")
            except ValueError:
                pass
        body = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise DecisionError("response_too_large")
        return bytes(body)

    @staticmethod
    async def _conflict_reason(response: aiohttp.ClientResponse) -> str:
        header_code = response.headers.get("X-System-One-Error-Code", "")
        try:
            body = await DecisionClient._read_bounded(response)
            payload = json.loads(body.decode("utf-8")) if not header_code else {}
            error = payload.get("error") if isinstance(payload, dict) else None
            code = (
                error.get("code")
                if isinstance(error, dict)
                else payload.get("code")
                if isinstance(payload, dict)
                else ""
            )
            normalized = (
                str(header_code or code or "").strip().casefold().replace("-", "_")
            )
        except (aiohttp.ClientError, UnicodeError, ValueError, TypeError):
            return "conflict"
        if normalized in {"idempotency_conflict", "idempotency_payload_mismatch"}:
            return "idempotency_conflict"
        if normalized == "idempotency_failed":
            return "idempotency_failed"
        if normalized in {"request_in_progress", "in_progress", "already_processing"}:
            return "request_in_progress"
        return "conflict"

    @staticmethod
    def _http_error(status: int) -> DecisionError:
        codes = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            408: "request_timeout",
            429: "rate_limited",
        }
        if status in codes:
            return DecisionError(codes[status])
        if 500 <= status <= 599:
            return DecisionError("server_error")
        return DecisionError("http_error")

    async def _cloudflare_error(
        self, response: aiohttp.ClientResponse
    ) -> DecisionError:
        """将 Cloudflare v4 错误信封映射为稳定错误码。

        Args:
            response: Workers AI 的失败响应，可能包含错误信封。

        Returns:
            不含上游消息细节的稳定错误。
        """
        code = None
        try:
            body = await self._read_bounded(response)
            payload = json.loads(body.decode("utf-8"))
        except (aiohttp.ClientError, UnicodeError, ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
            errors = payload["errors"]
            if errors and isinstance(errors[0], dict):
                raw = errors[0].get("code")
                if isinstance(raw, int) and not isinstance(raw, bool):
                    code = raw
        if code in CLOUDFLARE_ERROR_CODES:
            return DecisionError(CLOUDFLARE_ERROR_CODES[code])
        return self._http_error(response.status)

    @staticmethod
    def _cloudflare_payload(payload) -> dict:
        """将 Workers AI v4 信封解包为统一的答案结构。

        Args:
            payload: Workers AI 成功响应解码后的 JSON。

        Returns:
            顶层包含 answers、model 和 usage 的结果。

        Raises:
            DecisionError: 信封格式错误或上游报告失败。
        """
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DecisionError("invalid_response")
        result = payload.get("result")
        if isinstance(result, dict) and "answers" not in result:
            # 经网关计费的决策模型可能在 result 内再包一层结果。
            inner = result.get("result")
            if isinstance(inner, dict) and "answers" in inner:
                result = inner
        if not isinstance(result, dict) or "answers" not in result:
            raise DecisionError("invalid_response")
        return result

    async def evaluate(self, state: dict, questions: dict) -> dict:
        """执行一次推理，不重试或切换模型。

        Args:
            state: 共享任务数据，独立于固定的问题指令。
            questions: 使用文档约定条件的具名问题。

        Returns:
            已完整校验的答案对象。

        Raises:
            DecisionError: 配置、协议、HTTP 或截止时间错误。
            asyncio.CancelledError: 所属任务已被取消。
        """
        reason = self._reason()
        if reason != "ready":
            raise DecisionError(reason)
        body = self._encode_request(state, questions)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        if self.provider != "cloudflare":
            headers["Idempotency-Key"] = str(uuid.uuid4())

        async def request_once():
            """在调用方的总截止时间内返回一个大小受限的响应。"""
            async with self._semaphore:
                session = await self._get_session()
                task_name = state.get("task") if isinstance(state, dict) else "other"
                if not isinstance(task_name, str) or task_name not in {
                    "meme_selection",
                    "auto_collect_judgment",
                }:
                    task_name = "other"
                self.request_counts[task_name] = (
                    self.request_counts.get(task_name, 0) + 1
                )
                async with session.post(
                    self._endpoint(),
                    data=body,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if not 200 <= response.status <= 299:
                        if self.provider == "cloudflare":
                            raise await self._cloudflare_error(response)
                        if response.status == 409:
                            raise DecisionError(await self._conflict_reason(response))
                        raise self._http_error(response.status)
                    return (
                        await self._read_bounded(response),
                        response.headers.get("X-System-One-Credits"),
                    )

        try:
            response_body, raw_credits = await asyncio.wait_for(
                request_once(), timeout=self.timeout
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise DecisionError("timeout") from None
        except aiohttp.ClientError:
            raise DecisionError("network_error") from None
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise DecisionError("invalid_response") from None
        if self.provider == "cloudflare":
            payload = self._cloudflare_payload(payload)
        if not isinstance(payload, dict):
            raise DecisionError("invalid_response")
        answers = self._validate_answers(questions, payload.get("answers"))
        reported_usage = payload.get("usage")
        usage = (
            {
                key: value
                for key, value in reported_usage.items()
                if key in {"input_tokens", "output_tokens", "total_tokens"}
                and type(value) in {int, float}
                and math.isfinite(value)
                and value >= 0
            }
            if isinstance(reported_usage, dict)
            else None
        )
        try:
            credits = float(raw_credits)
            if not math.isfinite(credits) or credits < 0:
                credits = None
        except (TypeError, ValueError):
            credits = None
        model = payload.get("model")
        if not isinstance(model, str) or len(model) > 128:
            model = self.model
        return DecisionAnswers(answers, model=model, usage=usage, credits=credits)

    async def close(self) -> None:
        """停止接受后续请求，并关闭共享 HTTP 会话。"""
        self._closed = True
        async with self._session_lock:
            session = self._session
            self._session = None
            if session is not None and not session.closed:
                await session.close()


__all__ = ["DecisionClient", "DecisionError"]
