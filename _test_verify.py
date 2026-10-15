#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单测修复后的 pre_deploy_verify：直接跑一次复测门，确认节点能被正确判活。"""
import os, sys
BASE = r"C:\Users\zxf72\proxy_collector"
os.chdir(BASE)
sys.path.insert(0, BASE)
import run_pipeline
ok = run_pipeline.pre_deploy_verify(top_n=30, min_alive=15)
print("PRE_DEPLOY_VERIFY_RESULT =", ok)
