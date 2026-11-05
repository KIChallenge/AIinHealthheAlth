"""Azure v1 Responses API adapter (official async OpenAI client).

SDK retries are disabled: `LlmService` is the only retry layer. Certificate
verification stays at the client default. Sampling parameters are not sent, because
Azure documents reasoning models as not supporting them. `store=False` keeps provider
conversation storage off, and no `previous_response_id` is ever used.
"""

import re

import openai
from openai import AsyncOpenAI

from .errors import (LlmAuthError, LlmDeploymentError, LlmInvalidRequest, LlmTransientError)
from .llm import RawResponse

RATE_HEADERS = ("retry-after", "retry-after-ms", "x-ratelimit-remaining-requests",
                "x-ratelimit-remaining-tokens", "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens")


def retry_after(headers):
    try:
        if headers.get("retry-after-ms"):
            return float(headers["retry-after-ms"]) / 1000
        if headers.get("retry-after"):
            return float(headers["retry-after"])
    except (TypeError, ValueError):
        return None
    return None


def request_arguments(*, model, instructions, input, schema_name, schema, max_output_tokens,
                      reasoning_effort):
    """Keyword arguments for responses.create. No sampling or legacy parameters."""
    return {
        "model": model,
        "instructions": instructions,
        "input": input,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": reasoning_effort},
        "store": False,
        "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema,
                            "strict": True}},
    }


class AzureResponsesAdapter:
    def __init__(self, settings):
        self.deployment = settings.deployment
        self._key = settings.api_key
        self.client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.endpoint, max_retries=0)

    async def close(self):
        await self.client.close()

    def _clean(self, text):
        text = str(text).replace(self._key, "***") if self._key else str(text)
        return re.sub(r"(api[-_]?key[\"':= ]+)[^\s\"',}]+", r"\1***", text, flags=re.IGNORECASE)[:300]

    async def create(self, *, timeout, **kwargs):
        try:
            raw = await self.client.responses.with_raw_response.create(
                **request_arguments(**kwargs), timeout=timeout)
        except openai.AuthenticationError:
            raise LlmAuthError("Azure OpenAI lehnt den API-Schlüssel ab. Bitte AZURE_OPENAI_API_KEY "
                               "prüfen.") from None
        except openai.PermissionDeniedError as exc:
            raise LlmAuthError(f"Azure OpenAI verweigert den Zugriff: {self._clean(exc.message)}") from None
        except openai.NotFoundError:
            raise LlmDeploymentError(
                f"Azure findet das Deployment {self.deployment!r} oder die Responses-Route nicht. Bitte "
                "AZURE_OPENAI_DEPLOYMENT_NAME und den Endpunkt prüfen. Es wird nicht auf einen "
                "anderen Anbieter oder API-Modus ausgewichen.") from None
        except openai.RateLimitError as exc:
            raise LlmTransientError("Azure OpenAI drosselt die Anfragen (429).",
                                    retry_after=retry_after(exc.response.headers), throttled=True) from None
        except openai.BadRequestError as exc:
            raise LlmInvalidRequest(f"Azure OpenAI lehnt die Anfrage ab: {self._clean(exc.message)}") from None
        except openai.APITimeoutError:
            raise LlmTransientError("Zeitüberschreitung bei Azure OpenAI.") from None
        except openai.APIConnectionError:
            raise LlmTransientError("Verbindung zu Azure OpenAI fehlgeschlagen.") from None
        except openai.InternalServerError as exc:
            raise LlmTransientError(f"Dienstfehler bei Azure OpenAI ({exc.status_code}).",
                                    retry_after=retry_after(exc.response.headers)) from None
        except openai.APIStatusError as exc:
            raise LlmInvalidRequest(f"Azure OpenAI antwortet mit Status {exc.status_code}: "
                                    f"{self._clean(exc.message)}") from None
        response = raw.parse()
        return self._to_raw(response, {k: raw.headers.get(k) for k in RATE_HEADERS if raw.headers.get(k)})

    @staticmethod
    def _to_raw(response, headers):
        refusal = None
        for item in response.output or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", "") == "refusal":
                    refusal = getattr(part, "refusal", None) or "Verweigerung"
        details = getattr(response, "incomplete_details", None)
        usage = getattr(response, "usage", None)
        out_details = getattr(usage, "output_tokens_details", None) if usage else None
        return RawResponse(
            status=response.status, output_text=response.output_text or None, refusal=refusal,
            incomplete_reason=getattr(details, "reason", None) if details else None,
            model=response.model, input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            reasoning_tokens=getattr(out_details, "reasoning_tokens", None), headers=headers)
