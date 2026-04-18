import sys
import traceback
from typing import Any

from agents.common.protocol import (
    ApiUsage,
    ResultEnvelope,
    TaskEnvelope,
    TaskStatus,
)


class AgentBase:
    """Base class for all Crucible agents.

    Handles the stdin/stdout JSON protocol. Subclasses implement execute().
    """

    def __init__(self) -> None:
        self._logs: list[str] = []

    def run(self) -> None:
        task_json = sys.stdin.read()
        task = TaskEnvelope.model_validate_json(task_json)

        try:
            result_data, usage = self.execute(task)
            result = ResultEnvelope(
                task_id=task.task_id,
                status=TaskStatus.SUCCESS,
                result=result_data,
                usage=usage,
                logs=self._logs,
            )
        except Exception as exc:
            result = ResultEnvelope(
                task_id=task.task_id,
                status=TaskStatus.FAILURE,
                result={"error": str(exc), "traceback": traceback.format_exc()},
                usage=ApiUsage(),
                logs=self._logs,
            )

        sys.stdout.write(result.model_dump_json())
        sys.stdout.flush()

    def execute(self, task: TaskEnvelope) -> tuple[dict[str, Any], ApiUsage]:
        raise NotImplementedError("Subclasses must implement execute()")

    def log(self, message: str) -> None:
        self._logs.append(message)
