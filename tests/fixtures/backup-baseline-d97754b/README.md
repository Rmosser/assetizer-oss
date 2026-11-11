`tests/test_backup_rules.py` 用的旧版实现：提交 d97754b（专题原片和底稿备份到自己的 Google 网盘）时的 `src/assetizer/db.py` 与 `src/assetizer/topics/backup.py`，原样保存。测试在子进程里加载它们，造出旧库再验证迁移。

这个提交在 squash 合并后不在任何远端分支上，新克隆用 `git show` 取不到，所以放在这里。不要改这两个文件。
