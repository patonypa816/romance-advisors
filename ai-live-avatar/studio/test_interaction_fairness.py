"""纯本地公平合同测试；不证明云端发送或真实直播效果。"""
import unittest
from interaction_fairness import InteractionFairness


class InteractionFairnessTests(unittest.TestCase):
    ALL = {"comment", "gift", "welcome"}

    def test_threshold_and_longest_wait(self):
        fair = InteractionFairness()
        fair.complete({"comment"}, self.ALL)
        self.assertIsNone(fair.choose(self.ALL)["required_kind"])
        fair.complete({"comment"}, self.ALL)
        self.assertEqual(fair.choose(self.ALL)["required_kind"], "gift")
        fair.complete({"gift"}, self.ALL)
        self.assertEqual(fair.choose(self.ALL)["required_kind"], "welcome")

    def test_prediction_distinguishes_no_round_from_main_round(self):
        fair = InteractionFairness()
        fair.complete(set(), self.ALL)
        before = fair.snapshot()
        self.assertIsNone(fair.choose(self.ALL)["required_kind"])
        self.assertEqual(fair.choose(self.ALL, set())["required_kind"], "comment")
        predicted = fair.choose(self.ALL, {"comment"})
        self.assertEqual(predicted["waiting_rounds"], {"comment": 0, "gift": 2, "welcome": 2})
        self.assertEqual(predicted["required_kind"], "gift")
        # 失败不调用 complete：丢弃预测后正式账仍完整保留。
        self.assertEqual(fair.snapshot(), before)
        self.assertIsNone(fair.choose(self.ALL)["required_kind"])

    def test_queries_and_snapshots_cannot_change_accounting(self):
        fair = InteractionFairness()
        fair.complete(set(), self.ALL)
        fair.complete(set(), self.ALL)
        original = fair.snapshot()
        for _ in range(20):
            self.assertEqual(fair.choose(self.ALL)["required_kind"], "comment")
        choice = fair.choose(self.ALL)
        choice["waiting_rounds"]["comment"] = 999
        snapshot = fair.snapshot()
        snapshot["waiting_rounds"]["gift"] = 999
        snapshot["tie_break_order"].reverse()
        self.assertEqual(fair.snapshot(), original)

    def test_empty_pool_resets_but_candidate_replacement_does_not(self):
        fair = InteractionFairness()
        fair.complete({"comment"}, self.ALL)
        fair.observe(self.ALL)  # 池中旧候选换成新人，但类别仍非空。
        fair.complete({"comment"}, self.ALL)
        self.assertEqual(fair.choose(self.ALL)["waiting_rounds"]["welcome"], 2)
        fair.observe({"comment", "gift"})
        self.assertEqual(fair.snapshot()["waiting_rounds"]["welcome"], 0)
        fair.observe(self.ALL)
        self.assertEqual(fair.snapshot()["waiting_rounds"]["welcome"], 0)

    def test_tie_rotation_changes_only_after_complete(self):
        fair = InteractionFairness()
        fair.complete({"comment", "welcome"}, self.ALL)
        fair.complete(set(), self.ALL)
        fair.complete(set(), self.ALL)
        self.assertEqual(fair.choose(self.ALL)["required_kind"], "gift")
        fair.complete({"gift"}, self.ALL)
        fair.complete(set(), self.ALL)
        fair.complete(set(), self.ALL)
        self.assertEqual(fair.choose({"comment", "welcome"})["required_kind"], "welcome")

    def test_successful_rounds_bound_starvation_for_every_free_choice(self):
        # 穷举尚未触发保底时的全部选择：空主讲、单类及混合覆盖。
        # 每个可达状态继续探索；持续非空类别最多等待 threshold+2 轮。
        subsets = [set(k for bit, k in enumerate(InteractionFairness.KINDS) if mask & (1 << bit))
                   for mask in range(8)]
        for threshold in (1, 2, 3):
            states = {(0, 0, 0, 0)}
            visited = set()
            while states:
                state = states.pop()
                if state in visited:
                    continue
                visited.add(state)
                fair = InteractionFairness(threshold)
                fair._waiting = dict(zip(fair.KINDS, state[:3]))
                fair._cursor = state[3]
                required = fair.choose(self.ALL)["required_kind"]
                for served in subsets:
                    if required and required not in served:
                        continue
                    branch = InteractionFairness(threshold)
                    branch._waiting = dict(fair._waiting)
                    branch._cursor = fair._cursor
                    branch.complete(served, self.ALL)
                    values = tuple(branch.snapshot()["waiting_rounds"].values())
                    self.assertLessEqual(max(values), threshold + 2)
                    states.add((*values, branch._cursor))

    def test_invalid_input_is_atomic_and_reset_clears_all_state(self):
        fair = InteractionFairness()
        fair.complete({"comment"}, self.ALL)
        before = fair.snapshot()
        for method, args in ((fair.observe, ({"typo"},)),
                             (fair.complete, ({"comment"}, {"typo"})),
                             (fair.choose, (self.ALL, {"typo"}))):
            with self.assertRaises(ValueError):
                method(*args)
            self.assertEqual(fair.snapshot(), before)
        fair.reset()
        self.assertEqual(fair.snapshot(), InteractionFairness().snapshot())
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                InteractionFairness(value)


if __name__ == "__main__":
    unittest.main()
