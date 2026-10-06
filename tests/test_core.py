"""retry.core 的行为测试：退避封顶与抖动、熔断三态、半开探测配额、最大尝试次数与统计计数。"""

import unittest

from retry.core import (
    BackoffPolicy,
    CircuitBreaker,
    CLOSED,
    HALF_OPEN,
    OPEN,
    RetryKernel,
)


class BackoffPolicyTest(unittest.TestCase):
    """退避：倍增、封顶与抖动序列的消费顺序。"""

    def test_backoff_grows_and_stays_within_the_cap(self):
        """退避逐次倍增，任一次都不超过封顶值，封顶之后不再往上冒。"""
        capped = BackoffPolicy(base_ticks=100, multiplier=3, max_ticks=250)
        self.assertEqual(capped.envelope(1), 100)
        self.assertEqual(capped.envelope(2), 250)
        self.assertEqual(capped.envelope(3), 250)
        self.assertEqual(capped.envelope(6), 250)
        figures = [capped.envelope(attempt) for attempt in range(1, 9)]
        for earlier, later in zip(figures, figures[1:]):
            self.assertLessEqual(earlier, later)
        uncapped = BackoffPolicy(base_ticks=50, multiplier=2)
        self.assertEqual([uncapped.envelope(n) for n in (1, 2, 3, 4)],
                         [50, 100, 200, 400])

    def test_injected_jitter_is_consumed_in_order(self):
        """注入的抖动千分比按顺序逐个生效、用完从头循环，且只削不增。"""
        policy = BackoffPolicy(base_ticks=100, multiplier=3, max_ticks=250,
                               jitter=(0, 500, 1000))
        delays = [policy.delay(attempt) for attempt in range(1, 6)]
        self.assertEqual(delays, [100, 125, 0, 250, 125])
        for attempt, delay in enumerate(delays, start=1):
            self.assertGreaterEqual(delay, 0)
            self.assertLessEqual(delay, policy.envelope(attempt))


class CircuitBreakerTest(unittest.TestCase):
    """熔断器：开闸、打开时长、半开配额与连续成功复位。"""

    def _breaker(self, **kwargs):
        settings = dict(failure_threshold=3, open_ticks=100,
                        success_threshold=2, half_open_limit=1)
        settings.update(kwargs)
        return CircuitBreaker(**settings)

    def test_breaker_opens_after_consecutive_failures_and_rejects(self):
        """连续失败到阈值就开闸，打开之后一律拒绝并且不再累计。"""
        breaker = self._breaker()
        self.assertTrue(breaker.allow(0))
        breaker.on_failure(0)
        self.assertTrue(breaker.allow(0))
        breaker.on_failure(0)
        self.assertTrue(breaker.allow(0))
        self.assertEqual(breaker.state, CLOSED)
        breaker.on_failure(0)
        self.assertEqual(breaker.state, OPEN)
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertFalse(breaker.allow(0))
        self.assertFalse(breaker.allow(50))
        self.assertEqual(breaker.rejected, 2)
        self.assertEqual(breaker.trips, 1)

    def test_open_period_ends_exactly_at_open_ticks(self):
        """打开时长恰好走满的那个刻度上就转入半开，并放行一个探测。"""
        breaker = self._breaker(failure_threshold=2, open_ticks=100)
        breaker.allow(0)
        breaker.on_failure(0)
        breaker.allow(0)
        breaker.on_failure(0)
        self.assertEqual(breaker.state, OPEN)
        self.assertFalse(breaker.allow(99))
        self.assertTrue(breaker.allow(100))
        self.assertEqual(breaker.state, HALF_OPEN)
        self.assertEqual(breaker.probes_in_flight, 1)

    def test_half_open_admits_only_the_probe_quota(self):
        """半开态同时在途的探测不得超过配额，报告结果之后配额才归还。"""
        breaker = self._breaker(failure_threshold=1, open_ticks=50,
                                half_open_limit=2)
        breaker.allow(0)
        breaker.on_failure(0)
        self.assertEqual(breaker.state, OPEN)
        self.assertTrue(breaker.allow(60))
        self.assertTrue(breaker.allow(60))
        self.assertEqual(breaker.probes_in_flight, 2)
        self.assertFalse(breaker.allow(60))
        breaker.on_success(60)
        self.assertEqual(breaker.probes_in_flight, 1)
        self.assertTrue(breaker.allow(60))
        self.assertEqual(breaker.probes_in_flight, 2)

    def test_half_open_successes_must_be_consecutive_to_reset(self):
        """半开态要连续成功到阈值才复位，中途失败过就从零重新数。"""
        breaker = self._breaker(failure_threshold=2, open_ticks=100,
                                success_threshold=2)
        breaker.allow(0)
        breaker.on_failure(0)
        breaker.allow(0)
        breaker.on_failure(0)
        self.assertEqual(breaker.state, OPEN)
        self.assertTrue(breaker.allow(100))
        breaker.on_success(100)
        self.assertEqual(breaker.state, HALF_OPEN)
        self.assertTrue(breaker.allow(100))
        breaker.on_failure(100)
        self.assertEqual(breaker.state, OPEN)
        self.assertTrue(breaker.allow(200))
        breaker.on_success(200)
        self.assertEqual(breaker.state, HALF_OPEN)
        self.assertTrue(breaker.allow(200))
        breaker.on_success(201)
        self.assertEqual(breaker.state, CLOSED)

    def test_success_in_closed_state_resets_the_failure_streak(self):
        """闭合态成功一次就把连续失败清零，之后的失败得重新数满阈值。"""
        breaker = self._breaker(failure_threshold=3, open_ticks=1000)
        self.assertTrue(breaker.allow(0))
        breaker.on_failure(0)
        self.assertTrue(breaker.allow(1))
        breaker.on_failure(1)
        self.assertEqual(breaker.consecutive_failures, 2)
        self.assertTrue(breaker.allow(2))
        breaker.on_success(2)
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertTrue(breaker.allow(3))
        breaker.on_failure(3)
        self.assertTrue(breaker.allow(4))
        breaker.on_failure(4)
        self.assertEqual(breaker.state, CLOSED)
        self.assertTrue(breaker.allow(5))
        breaker.on_failure(5)
        self.assertEqual(breaker.state, OPEN)
        self.assertFalse(breaker.allow(5))


class RetryKernelTest(unittest.TestCase):
    """重试链：最大尝试次数、等待序列、熔断拒绝与统计计数。"""

    def test_retry_loop_gives_up_after_max_attempts(self):
        """尝试次数到上限就停，发起次数、等待序列与结束刻度都对得上。"""
        kernel = RetryKernel(BackoffPolicy(base_ticks=100, multiplier=3,
                                           max_ticks=250), max_attempts=3)
        result = kernel.run(0, ["fail"] * 5)
        self.assertEqual(result.reason, "exhausted")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.delays, [100, 250])
        self.assertEqual(result.finished_at, 350)
        self.assertEqual(kernel.attempts, 3)
        self.assertEqual(kernel.retries, 2)
        self.assertEqual(kernel.total_delay, 350)

    def test_breaker_rejection_spends_no_attempt(self):
        """被熔断拒绝的那一步不算一次尝试，也不消耗注入的结果。"""
        breaker = CircuitBreaker(failure_threshold=2, open_ticks=1000,
                                 success_threshold=1, half_open_limit=1)
        kernel = RetryKernel(BackoffPolicy(base_ticks=50, multiplier=2),
                             max_attempts=5, breaker=breaker)
        result = kernel.run(0, ["fail", "fail", "fail", "fail", "ok"])
        self.assertEqual(result.reason, "rejected")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.state, OPEN)
        self.assertEqual(kernel.attempts, 2)
        self.assertEqual(kernel.rejected, 1)
        self.assertEqual(kernel.successes, 0)
        self.assertEqual(kernel.failures, 2)
        self.assertEqual(breaker.trips, 1)

    def test_timeout_counts_as_a_failure_everywhere(self):
        """超时按失败计：既进失败统计，也推高熔断器的连续失败。"""
        breaker = CircuitBreaker(failure_threshold=2, open_ticks=1000,
                                 success_threshold=1, half_open_limit=1)
        kernel = RetryKernel(BackoffPolicy(base_ticks=50, multiplier=2),
                             max_attempts=4, breaker=breaker)
        result = kernel.run(0, ["timeout", "timeout", "ok", "ok"])
        self.assertEqual(kernel.timeouts, 2)
        self.assertEqual(kernel.failures, 2)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.reason, "rejected")
        self.assertEqual(kernel.rejected, 1)
        self.assertEqual(kernel.successes, 0)
        self.assertEqual(breaker.trips, 1)


if __name__ == "__main__":
    unittest.main()
