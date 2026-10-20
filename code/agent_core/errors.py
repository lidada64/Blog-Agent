"""Separate application policy decisions from operating-system failures."""
from functools import wraps


class PolicyDenied(PermissionError):
    """The application rejected an action, not a filesystem operation."""


class PersistenceError(OSError):
    """The checkpoint has not been confirmed durable."""


def tool_error(error):
    category = ('policy_denied' if isinstance(error, PolicyDenied) else
                'storage' if isinstance(error, PersistenceError) else
                'io' if isinstance(error, OSError) else
                'invalid_arguments' if isinstance(error, (ValueError, TypeError)) else 'execution')
    # Preserve the public error type used by existing permission regressions.
    result = {'type': 'PermissionError' if isinstance(error, PolicyDenied) else type(error).__name__,
              'message': str(error), 'category': category}
    if isinstance(error, PersistenceError) and error.__cause__ is not None:
        result.update(cause_type=type(error.__cause__).__name__,
                      errno=getattr(error.__cause__, 'errno', None),
                      winerror=getattr(error.__cause__, 'winerror', None))
    return result


def checkpoint_boundary(method):
    """Stop safely on persistence failure; never claim a second save succeeded."""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        if self.state.get('status') == 'storage_error':
            return 'storage_error', '当前内存状态未确认保存，请重新启动并用 --resume 加载磁盘上的最后检查点。'
        try:
            return await method(self, *args, **kwargs)
        except PersistenceError as error:
            self.state['status'] = 'storage_error'
            self.state['last_error'] = tool_error(error)
            return 'storage_error', '保存会话失败，本轮最新状态尚未确认写入磁盘。请处理文件访问问题后，用 --resume 从最后成功保存的位置恢复；不要重复提交问题。'
    return wrapped
