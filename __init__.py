"""表情包管理器的运行环境要求。"""

import sys

if sys.version_info < (3, 12):
    raise RuntimeError("Meme manager requires Python 3.12 or newer.")
