#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""续跑脚本：复用 run_pipeline.py 的步骤与函数，从 http_test 起接上本轮已成功的采集结果。
不修改 run_pipeline.py 与任何订阅源。"""
import run_pipeline as rp

def main():
    rp.log("==== 代理刷新流水线(续跑)启动 ====")
    rp.run_step("http_test.py", "output.yaml", rp.HTTP_CAND)
    rp.run_step("speed_test.py", rp.HTTP_CAND, rp.FAST, "--top", "100",
                "--bytes", "2000000", "--timeout", "10")
    rp.apply_stability(rp.FAST, rp.STABLE_HISTORY, min_stable=2, strict=False)
    rp.pre_deploy_verify(top_n=30, min_alive=15)
    rp.run_step("make_mobile.py", rp.FAST, "output-filtered-fast-mobile.yaml")
    rp.run_step("deploy_to_clash.py")
    rp.log("==== 流水线(续跑)完成 ====")

if __name__ == "__main__":
    main()
