# retry

一个纯内存、确定性的重试与熔断策略内核：指数退避的倍增、封顶与注入式抖动，
三态熔断器的连续失败开闸、打开时长、半开探测配额与连续成功复位，以及把两者
拼起来、带最大尝试次数与统计计数的重试链。时间由调用方以整数刻度注入，抖动
由调用方注入千分比序列，内核不读真实时钟、不做随机、不起线程，也不做任何 I/O。

## 目录

- retry/core.py：内核实现（退避策略、熔断器、重试链与其结果快照）
- tests/test_core.py：内核的行为测试

## 怎么跑测试

在项目根目录执行：

    python3 -m unittest discover -s tests -v

Windows 上把 python3 换成你的解释器路径，例如：

    C:/Users/<你>/AppData/Local/Programs/Python/Python313/python.exe -m unittest discover -s tests -v
