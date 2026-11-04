"""仅按完整发送轮次计账的三类互动公平规则；不访问时钟或外部服务。"""
from collections.abc import Iterable


class InteractionFairness:
    KINDS = ("comment", "gift", "welcome")

    def __init__(self, threshold: int = 2):
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("threshold must be a positive integer")
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self._waiting = dict.fromkeys(self.KINDS, 0)
        self._cursor = 0

    def _kinds(self, values: Iterable[str]) -> set[str]:
        result = set(values)
        if result - set(self.KINDS):
            raise ValueError("unknown interaction kind")
        return result

    def observe(self, available: Iterable[str]) -> None:
        """池子确实空了才清零；替换候选、暂时预留候选不应报告为空。"""
        available = self._kinds(available)
        for kind in self.KINDS:
            if kind not in available:
                self._waiting[kind] = 0

    def _after_round(self, served: set[str], available: set[str]) -> dict[str, int]:
        return {
            kind: self._waiting[kind] + 1 if kind in available and kind not in served else 0
            for kind in self.KINDS
        }

    def complete(self, served: Iterable[str], available: Iterable[str]) -> None:
        """仅在整轮必要信号全部发送成功后调用一次；失败/取消不得调用。

        available 应包括预留给已承诺后续轮的有效候选，避免暂时移出池子清账。
        """
        served, available = self._kinds(served), self._kinds(available)
        self._waiting = self._after_round(served, available)
        # 轮换只随实际服务推进。混合轮按当前优先顺序的最后一项推进，
        # 让本轮没有被服务的类别获得后续并列优先权。
        order = self._order()
        covered = [kind for kind in order if kind in served]
        if covered:
            self._cursor = (self.KINDS.index(covered[-1]) + 1) % len(self.KINDS)

    def _order(self) -> tuple[str, ...]:
        return self.KINDS[self._cursor:] + self.KINDS[:self._cursor]

    def choose(self, available: Iterable[str], current_served: Iterable[str] | None = None) -> dict:
        """返回必须安排的类别；None 表示交给编排器自由选择。

        current_served=None 表示没有执行轮；空集合表示执行中的主讲/人工轮。
        预测不会提交成功账，也不会因反复查询改变并列优先顺序。
        """
        available = self._kinds(available)
        if current_served is None:
            waiting = {kind: self._waiting[kind] if kind in available else 0 for kind in self.KINDS}
        else:
            waiting = self._after_round(self._kinds(current_served), available)
        eligible = [kind for kind in self._order() if kind in available and waiting[kind] >= self.threshold]
        required = max(eligible, key=waiting.__getitem__) if eligible else None
        return {"required_kind": required, "waiting_rounds": waiting}

    def snapshot(self) -> dict:
        return {"waiting_rounds": dict(self._waiting), "tie_break_order": list(self._order())}
