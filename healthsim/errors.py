"""Application errors. `message` is always German and free of secrets."""


class SimError(Exception):
    code = "fehler"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ConfigurationError(SimError):
    code = "configuration_error"


class SourceError(SimError):
    code = "source_error"


class PermissionDeniedError(SimError):
    code = "permission_denied"


class DeadlineExpired(SimError):
    code = "deadline_expired"


class OversizedRequest(SimError):
    code = "oversized_request"


class LlmFatalError(SimError):
    """Not retryable and not repairable: stops the debate with execution_error."""
    code = "llm_fatal"


class LlmAuthError(LlmFatalError):
    code = "llm_auth"


class LlmDeploymentError(LlmFatalError):
    code = "llm_deployment"


class LlmInvalidRequest(LlmFatalError):
    code = "llm_invalid_request"


class LlmTransientError(SimError):
    code = "llm_transient"

    def __init__(self, message, retry_after=None, throttled=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.throttled = throttled


class LlmOutputError(SimError):
    """The model answered, but the answer cannot be used (eligible for one repair)."""
    code = "llm_output"


class LlmRefusal(LlmOutputError):
    code = "llm_refusal"


class LlmIncomplete(LlmOutputError):
    code = "llm_incomplete"
