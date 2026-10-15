"""桌面端测试的公共设置。"""

import os

# 必须在 QApplication 建起来之前设置：无显示环境下 Qt 也要能建窗口。
# 用 setdefault，本机想真跑界面时设 QT_QPA_PLATFORM 即可覆盖。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
