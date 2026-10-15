# -*- coding: utf-8 -*-
"""示例：付费订阅源模板（机场订阅，aggregator collect.py 风格）。

本质就是封装 subscription_parser.parse_subscription()。
把它改名为 my_airport.py 并填入你的真实订阅 URL 即可被自动注册。
"""

__crawler__ = {"name": "my-airport", "source": "paid", "enabled": True}

from subscription_parser import parse_subscription

SUBSCRIPTION_URL = "https://your-airport.example.com/link/XXXXXXXX?sub=1"


def crawl() -> list[str]:
    try:
        return parse_subscription(SUBSCRIPTION_URL)
    except Exception as e:
        print(f"[my-airport] fetch failed: {e}")
        return []
