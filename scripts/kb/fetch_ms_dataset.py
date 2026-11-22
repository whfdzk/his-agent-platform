# -*- coding: utf-8 -*-
"""下载中文医疗问答语料到本地。"""
import os
from modelscope import snapshot_download

BASE = r"D:\github_xiangmu\his-agent-platform-main\his-agent-platform-main\_msdata"
p = snapshot_download(
    repo_id="xiaofengalg/Chinese-medical-dialogue",
    repo_type="dataset",
    cache_dir=BASE,
    allow_patterns=["data/train_0001_of_0001.json", "README.md"],
)
print("OK ->", p)
for root, _, files in os.walk(p):
    for f in files:
        fp = os.path.join(root, f)
        print(f"{round(os.path.getsize(fp)/1048576,2)}MB  {fp}")
