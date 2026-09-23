"""Keep field defects separate from failures of the shared research runtime."""
import json

from sqlalchemy.exc import SQLAlchemyError

from deepresearch_agent.harness.budgets import BudgetExceeded
from deepresearch_agent.harness.errors import AppError, ErrorCode, ResearchPauseRequested, RunCancelled


class CellValidationError(AppError):
    def __init__(self, message, *, issues=None):
        super().__init__(ErrorCode.CONFLICT, message,
                         details={'validation_errors': issues} if issues else None)

    def __str__(self):
        issues = self.details.get('validation_errors', [])
        return self.message + ''.join(
            f"；{issue['path']}：要求={json.dumps(issue['expected'], ensure_ascii=False)}"
            f"，实际={json.dumps(issue['actual'], ensure_ascii=False)}" for issue in issues)


def is_global_failure(exc):
    if isinstance(exc, (BudgetExceeded, ResearchPauseRequested, RunCancelled, SQLAlchemyError, OSError)):
        return not isinstance(exc, (TimeoutError, ConnectionError))
    if isinstance(exc, CellValidationError):
        return False
    if isinstance(exc, AppError):
        return exc.code not in {ErrorCode.RETRIEVAL_FAILED, ErrorCode.RETRIEVAL_TIMEOUT, ErrorCode.TAVILY_RATE_LIMITED}
    status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
    return status in {401, 403} or type(exc).__name__ in {'AuthenticationError', 'PermissionDeniedError'}


def failure_detail(exc, stage, attempts):
    # Provider/model exception bodies may contain credentials or source text.
    reason = exc.message if isinstance(exc, AppError) else {
        'retrieval': '检索未成功，尚未取得本字段所需资料',
        'extraction': '资料抽取失败或返回格式不完整',
        'validation': '抽取结果未通过证据校验',
    }[stage]
    detail = {'stage': stage, 'code': exc.code.value if isinstance(exc, AppError) else type(exc).__name__,
              'attempts': attempts, 'reason': reason}
    if isinstance(exc, CellValidationError) and exc.details.get('validation_errors'):
        # Keep unverified values out of the report's prose, but preserve them for
        # the next extraction attempt and the durable failure record.
        detail['validation_errors'] = exc.details['validation_errors']
    return detail
