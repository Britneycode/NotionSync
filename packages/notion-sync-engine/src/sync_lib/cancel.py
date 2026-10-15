"""协作式取消：检查点与可中断等待（设计 v2 §4.2 / §7.1）。"""

import threading


class Cancelled(BaseException):
    """在取消检查点抛出；只会在文件边界或等待期间出现。

    刻意继承 `BaseException` 而非 `Exception`：取消是控制流信号，不是文件级错误。
    `executor.execute_plan` 的 `except Exception` 会把异常记成"该文件失败"并继续跑
    下一个文件——那样取消会被吞掉，还会留下一条假的失败记录。继承 `BaseException`
    就绕开了那个兜底，和 `KeyboardInterrupt` 的处理方式一致。
    """


class CancelToken:
    """跨线程的取消标志。

    只提供"置位"和"检查"，不做抢占：真正的打断点在 executor 的文件边界，
    以及在限流/退避的等待里（`sleep` 用 Event.wait，可立即返回）。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        """替代 time.sleep：等待期间被取消则立刻抛 Cancelled。"""
        if self._event.wait(seconds):
            raise Cancelled()
