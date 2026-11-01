from __future__ import annotations

import asyncio
import json
import math
import uuid
from urllib.parse import urlsplit

import aiohttp

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUESTIONS = 32
MAX_OPTIONS = 255


class DecisionError(Exception):
    """Expose a stable error code without credentials or upstream detail."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class DecisionAnswers(dict):
    """Keep per-request metadata without adding keys to the answer mapping."""

    def __init__(self, answers, *, model="", usage=None, credits=None):
        super().__init__(answers)
        self.model = model
        self.usage = usage
        self.credits = credits


class DecisionClient:
    """Share one bounded System One transport between independent tasks."""

    def __init__(self, config: dict):
        """Initialize configuration without starting a session or inference.

        Args:
            config: Service URL, token, model, timeout, and concurrency settings.
        """
        values = config if isinstance(config, dict) else {}
        self.base_url = (
            str(values.get("base_url", "https://system-one.dev/v1") or "")
            .strip()
            .rstrip("/")
        )
        self.token = str(values.get("token") or "").strip()
        self.model = str(values.get("model") or "").strip()
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
        if not self.base_url:
            return "missing_base_url"
        if not self._base_url_valid():
            return "invalid_base_url"
        if not self.token:
            return "missing_token"
        if any(ord(char) < 33 or ord(char) == 127 for char in self.token):
            return "invalid_token"
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
        """Whether the local configuration is valid and the client is open."""
        return self._reason() == "ready"

    def status(self) -> dict:
        """Return local readiness only, without making a request or exposing secrets.

        Returns:
            Redacted service configuration status, not a connectivity guarantee.
        """
        reason = self._reason()
        return {
            "ready": reason == "ready",
            "reason": reason,
            "model": self.model,
            "backend": "systemone",
            "requests_started": dict(self.request_counts),
        }

    def _endpoint(self) -> str:
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
        """Read the public catalog without credentials or an inference request.

        Returns:
            Exact model IDs and display names, preserving catalog order.

        Raises:
            DecisionError: The URL, connection, response size, or catalog is invalid.
            asyncio.CancelledError: The owning discovery request was cancelled.
        """
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

    @staticmethod
    def _question_options(question: dict) -> list[str]:
        """Validate documented question criteria and return distribution keys.

        Args:
            question: A Choice, Noul, or Score question with fixed instructions.

        Returns:
            Allowed probability keys for the selected primitive.

        Raises:
            DecisionError: The request does not follow the System One contract.
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
        if not 1 <= len(options) <= MAX_OPTIONS:
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
        if not 1 <= len(questions) <= MAX_QUESTIONS:
            raise DecisionError("invalid_request")
        for name, question in questions.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 128
                or not isinstance(question, dict)
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
        if len(body) > MAX_REQUEST_BYTES:
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

    @classmethod
    def _validate_answers(cls, questions: dict, answers) -> dict:
        """Validate the complete answer set before exposing any partial result.

        Args:
            questions: Exact questions sent to the service.
            answers: Service-provided answer object.

        Returns:
            Validated answers with optional statistics kept intact.

        Raises:
            DecisionError: Any answer, type, selection, or probability is invalid.
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
            options = cls._question_options(question)
            if kind == "choice":
                if answer.get("choice") not in options:
                    raise DecisionError("invalid_response")
            elif kind == "noul":
                cls._probability(answer.get("noul"))
            else:
                value = answer.get("score")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= len(options) - 1
                ):
                    raise DecisionError("invalid_response")
            cls._validate_answer_numbers(answer, options)
        return answers

    @staticmethod
    async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
        """Read all response chunks while enforcing a total byte limit.

        Args:
            response: Successful HTTP response, possibly chunked.

        Returns:
            Complete response bytes.

        Raises:
            DecisionError: The complete response exceeds its allowed size.
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

    async def evaluate(self, state: dict, questions: dict) -> dict:
        """Execute one inference without retrying or changing its model.

        Args:
            state: Shared task data, independent of the fixed question instructions.
            questions: Named questions using the documented criteria contract.

        Returns:
            The fully validated answers object.

        Raises:
            DecisionError: Configuration, protocol, HTTP, or deadline failure.
            asyncio.CancelledError: The owning task was cancelled.
        """
        reason = self._reason()
        if reason != "ready":
            raise DecisionError(reason)
        body = self._encode_request(state, questions)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

        async def request_once():
            """Return one bounded response within the caller's total deadline."""
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
                    if response.status == 409:
                        raise DecisionError(await self._conflict_reason(response))
                    if not 200 <= response.status <= 299:
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
        """Prevent further requests and close the shared HTTP session."""
        self._closed = True
        async with self._session_lock:
            session = self._session
            self._session = None
            if session is not None and not session.closed:
                await session.close()


__all__ = ["DecisionClient", "DecisionError"]
