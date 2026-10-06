"""retry：确定性重试与熔断策略内核（指数退避 + 三态熔断 + 重试链）。

组件：
    BackoffPolicy   指数退避：基准刻度、倍增系数、封顶与注入式抖动
    CircuitBreaker  三态熔断器：连续失败开闸、打开时长、半开探测配额、连续成功复位
    RetryKernel     重试链：最大尝试次数、超时判定与统计计数
    RetryResult     一次重试链的结果快照

约定：
    * 时间一律由调用方以整数刻度注入，内核不读真实时钟、不起线程、不做 I/O；
    * 抖动不做随机：调用方把一串千分比注入进来，内核按顺序消费、用完从头循环；
    * 同一串调用必然得到同一个结果，不依赖任何外部状态。
"""

MILLI = 1000

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

OK = "ok"
FAIL = "fail"
TIMEOUT = "timeout"

OUTCOMES = (OK, FAIL, TIMEOUT)


def _check_int(value, name, minimum):
    """整数入参校验：不是整数或小于下界就直接报错。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("%s 必须是整数" % name)
    if value < minimum:
        raise ValueError("%s 不能小于 %d" % (name, minimum))
    return value


class BackoffPolicy:
    """指数退避：第 attempt 次尝试失败之后要等的刻度数。

    参数：
        base_ticks  第一次重试的等待刻度，必须为正
        multiplier  每次失败后的倍增系数，必须不小于 1
        max_ticks   等待刻度的封顶值；None 表示不封顶
        jitter      抖动千分比序列（0..1000）：按顺序消费、用完从头循环，
                    每个值削掉当次等待的这个千分比，所以只会更短、不会更长

    第 attempt 次尝试失败后的等待是 base_ticks * multiplier ** (attempt - 1)，
    先按 max_ticks 截断，再削掉当次注入的抖动。
    """

    def __init__(self, base_ticks, multiplier=2, max_ticks=None, jitter=None):
        self.base_ticks = _check_int(base_ticks, "base_ticks", 1)
        self.multiplier = _check_int(multiplier, "multiplier", 1)
        if max_ticks is not None:
            max_ticks = _check_int(max_ticks, "max_ticks", 1)
            if max_ticks < self.base_ticks:
                raise ValueError("封顶不能小于基准等待")
        self.max_ticks = max_ticks
        values = []
        for value in (jitter or ()):
            value = _check_int(value, "jitter", 0)
            if value > MILLI:
                raise ValueError("抖动千分比必须落在 0..1000 之间")
            values.append(value)
        self.jitter = tuple(values)
        self._cursor = 0

    def envelope(self, attempt):
        """第 attempt 次尝试失败后的等待刻度：逐次倍增，按封顶值截断。"""
        attempt = _check_int(attempt, "attempt", 1)
        delay = self.base_ticks
        for _ in range(attempt - 1):
            if self.max_ticks is not None and delay >= self.max_ticks:
                return delay
            delay *= self.multiplier
        return delay

    def delay(self, attempt):
        """第 attempt 次尝试失败后真正等待的刻度：封顶值再削掉当次抖动。"""
        raw = self.envelope(attempt)
        if not self.jitter:
            return raw
        permille = self.jitter[self._cursor % len(self.jitter)]
        return raw - (raw * permille) // MILLI


class CircuitBreaker:
    """三态熔断器：closed → open → half_open → closed / open。

    参数：
        failure_threshold  闭合态连续失败到多少次就开闸
        open_ticks         开闸后至少保持打开的刻度数，走满之后转入半开
        success_threshold  半开态连续成功到多少次就复位
        half_open_limit    半开态同时在途的探测上限

    半开态每个被放行的探测在 on_success / on_failure 报告结果时归还配额：
    探测失败立刻把熔断器打回打开，连续成功到阈值则整条链复位。
    """

    def __init__(self, failure_threshold, open_ticks, success_threshold=1,
                 half_open_limit=1):
        self.failure_threshold = _check_int(failure_threshold, "failure_threshold", 1)
        self.open_ticks = _check_int(open_ticks, "open_ticks", 1)
        self.success_threshold = _check_int(success_threshold, "success_threshold", 1)
        self.half_open_limit = _check_int(half_open_limit, "half_open_limit", 1)
        self.allowed = 0
        self.rejected = 0
        self.trips = 0
        self._state = CLOSED
        self._opened_at = 0
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._probes = 0

    # ------------------------------------------------------------------ 状态

    @property
    def state(self):
        """最近一次推进之后的状态。"""
        return self._state

    @property
    def consecutive_failures(self):
        """闭合态里累计的连续失败次数。"""
        return self._consecutive_failures

    @property
    def consecutive_successes(self):
        """半开态里累计的连续成功次数。"""
        return self._consecutive_successes

    @property
    def probes_in_flight(self):
        """半开态当前占着配额的探测数。"""
        return self._probes

    # ------------------------------------------------------------------ 判定

    def allow(self, now):
        """询问 now 这一刻能不能发起调用；放行时会占掉一个探测配额。"""
        self._advance(int(now))
        if self._state == OPEN:
            self.rejected += 1
            return False
        if self._state == HALF_OPEN and self._probes > self.half_open_limit:
            self.rejected += 1
            return False
        self._probes += 1
        self.allowed += 1
        return True

    def on_success(self, now):
        """报告一次成功：半开态累计连续成功，闭合态清零连续失败。"""
        self._advance(int(now))
        if self._state == HALF_OPEN:
            self._release()
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.success_threshold:
                self._consecutive_successes = 0
                self._reset(CLOSED)

    def on_failure(self, now):
        """报告一次失败：半开态立刻打回打开，闭合态累计连续失败。"""
        now = int(now)
        self._advance(now)
        if self._state == HALF_OPEN:
            self._release()
            self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._trip(now)

    # ------------------------------------------------------------------ 内部

    def _advance(self, now):
        """按注入刻度推进时间：打开时长走满就转入半开。"""
        if self._state == OPEN and now - self._opened_at > self.open_ticks:
            self._reset(HALF_OPEN)

    def _release(self):
        """归还一个探测配额。"""
        if self._probes > 0:
            self._probes -= 1

    def _trip(self, now):
        """打开熔断器：重新计时，并清掉连续失败与被占住的配额。"""
        self._reset(OPEN)
        self._opened_at = now
        self.trips += 1

    def _reset(self, state):
        """切换状态：连续失败与在途探测一起归零。"""
        self._state = state
        self._consecutive_failures = 0
        self._probes = 0


class RetryResult:
    """一次重试链的结果快照。

    reason       停止原因：ok / exhausted / rejected
    attempts     本次真正发起的尝试次数
    delays       每次失败之后等待的刻度序列
    finished_at  链停下的刻度
    state        链停下时熔断器的状态；没有熔断器时是 None
    """

    __slots__ = ("reason", "attempts", "delays", "finished_at", "state")

    def __init__(self, reason, attempts, delays, finished_at, state=None):
        self.reason = reason
        self.attempts = attempts
        self.delays = list(delays)
        self.finished_at = finished_at
        self.state = state

    def __repr__(self):
        return ("RetryResult(reason=%r, attempts=%r, delays=%r, finished_at=%r, state=%r)"
                % (self.reason, self.attempts, self.delays, self.finished_at, self.state))


class RetryKernel:
    """重试链：把退避策略与熔断器拼成一条确定性的重试过程。

    参数：
        policy       退避策略
        max_attempts 一条链里最多发起的尝试次数（含第一次）
        breaker      可选的熔断器；为 None 时不做熔断判定

    统计跨链累计：attempts / successes / failures / timeouts / retries / rejected /
    total_delay。超时既进 timeouts，也按失败计入 failures，并推高熔断器的连续失败。
    """

    def __init__(self, policy, max_attempts, breaker=None):
        if not isinstance(policy, BackoffPolicy):
            raise ValueError("policy 必须是 BackoffPolicy")
        self.policy = policy
        self.max_attempts = _check_int(max_attempts, "max_attempts", 1)
        self.breaker = breaker
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.timeouts = 0
        self.retries = 0
        self.rejected = 0
        self.total_delay = 0

    def stats(self):
        """累计统计的一份副本。"""
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "retries": self.retries,
            "rejected": self.rejected,
            "total_delay": self.total_delay,
        }

    def run(self, now, outcomes):
        """按注入的结果序列跑一条重试链。

        outcomes 里每一项是一次尝试的结果：ok / fail / timeout。
        最多发起 max_attempts 次尝试；被熔断拒绝的那一步不算一次尝试，
        也不消耗注入的结果；只有还会再试一次时才结算退避。
        """
        now = int(now)
        queue = list(outcomes)
        if not queue:
            raise ValueError("结果序列不能为空")
        made = 0
        delays = []
        reason = "exhausted"
        while True:
            if self.breaker is not None and not self.breaker.allow(now):
                self.rejected += 1
                reason = "rejected"
                break
            if made >= len(queue):
                raise ValueError("注入的结果不够本次重试链使用")
            outcome = queue[made]
            if outcome not in OUTCOMES:
                raise ValueError("未知结果：%r" % (outcome,))
            made += 1
            self.attempts += 1
            if outcome == OK:
                self.successes += 1
                if self.breaker is not None:
                    self.breaker.on_success(now)
                reason = "ok"
                break
            if outcome == TIMEOUT:
                self.timeouts += 1
            else:
                self.failures += 1
                if self.breaker is not None:
                    self.breaker.on_failure(now)
            if made > self.max_attempts:
                reason = "exhausted"
                break
            delay = self.policy.delay(made)
            delays.append(delay)
            self.retries += 1
            self.total_delay += delay
            now += delay
        state = None if self.breaker is None else self.breaker.state
        return RetryResult(reason, made, delays, now, state)
