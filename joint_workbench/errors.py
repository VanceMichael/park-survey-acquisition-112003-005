"""工作台异常。"""


class WorkbenchError(RuntimeError):
    """工作台或上游状态不满足操作前提。"""


class SourceError(WorkbenchError):
    """数据源返回了不可解释的响应。"""

    def __init__(self, status, payload, path=""):
        self.status = status
        self.payload = payload
        self.path = path
        super().__init__(f"数据源 {path} 返回 {status}: {payload!r}")


class SubmissionBlocked(WorkbenchError):
    """送审内容被办公室范围、部件身份或幂等约束挡住。"""

    def __init__(self, message, details=None):
        self.details = details or []
        super().__init__(message)
