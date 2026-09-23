"""Local failure artifacts for inspecting model output without truncation."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import traceback
from uuid import uuid4

from pydantic import ValidationError

from deepresearch_agent.models.prefix_cache import get_current_run

logger = logging.getLogger(__name__)


def save_model_failure(*, operation, model, prompt, raw_response, parsed_response, error,
                       call_id=None, attempt=1, run_id=None):
    """Best effort only: diagnostics must never replace the original exception."""
    try:
        now = datetime.now(timezone.utc)
        payload = {
            'timestamp': now.isoformat(), 'run_id': run_id or get_current_run(),
            'operation': operation, 'model': model, 'prompt': prompt,
            'call_id': call_id, 'attempt': attempt,
            'raw_response': raw_response, 'parsed_response': parsed_response,
            'error_type': type(error).__name__, 'error_message': str(error),
            'validation_errors': (error.errors(include_context=False) if isinstance(error, ValidationError)
                                  else getattr(error, 'details', {}).get('validation_errors', [])),
            'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
        }
        directory = Path('data/research_errors')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{now:%Y%m%dT%H%M%S%fZ}_{uuid4().hex}.json"
        with path.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        logger.error('研究错误日志已保存: %s (run_id=%s)', path.resolve(), payload['run_id'])
    except Exception:
        logger.exception('无法保存研究错误日志；保留原始异常')
