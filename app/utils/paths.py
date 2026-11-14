"""运行期数据路径的**唯一真相**（``DATA_DIR`` / 持久卷）。

仓库里有**两棵**数据树，是历史遗留、且**必须保持分离**：

```text
<root>/app/data/   引擎与账号数据：auth.db · careerly.db · graph_state.db
                   outputs/ · uploads/ · resumes/ · tessdata/
<root>/data/       遗留路径：DemoGuard 的 graph_state.db、
                   router_app 的上传件与简历 JSON
```

两棵树下**同名不同文件**（``app/data/graph_state.db`` 是引擎轨迹、
``data/graph_state.db`` 是 DemoGuard 试用计数），合并会串数据 ——
因此本模块只搬**基准**，不改任何文件名与子目录名。

这套基准此前由 15 处 ``Path(__file__)`` 各自算一遍（8 处 ``parent.parent``、
7 处 ``parent.parent.parent``），改一处漏一处。本次引入 ``DATA_DIR`` 时若不
收敛，就会出现「库搬到了数据盘、某个产物还写在仓库里」这种半迁移状态，
而且只在部署之后才被发现。

配置与回退
----------
```text
DATA_DIR                      显式指定数据根（最高优先级）
RAILWAY_VOLUME_MOUNT_PATH     容器场景的兜底（挂卷后由平台自动注入）
```

未设任何一项 → **逐字保持今天的行为**：两棵树仍在仓库内。ECS 是持久 VM，
仓库内落盘本来就不会丢（见 ``docs/09_Release/DEPLOY_ECS.md`` §3），因此
「未设」是**正常默认**，本模块刻意**不打任何 warning**。

设了 → 两棵树**原样**搬到该根下：

```text
<DATA_ROOT>/app/     ← 原 <root>/app/data/
<DATA_ROOT>/root/    ← 原 <root>/data/
```

为什么 ``REPO_ROOT`` 自己算、不从 ``app.core.config`` import
----------------------------------------------------------
``app/core/config.py`` 在**导入期**会 ``load_dotenv()`` 并构造 ``AppConfig``
单例（读 config.yaml、建 pydantic 模型）。路径是启动期最底层的依赖，不该被
一个重量级单例的导入副作用绑架；而且 config.py 反过来要引用本模块
（``data_dir`` / ``output_dir`` 两个字段改为指向此处），若本模块再 import
config 就成环。故此处自己算 ``REPO_ROOT``。

⚠️ 为什么本模块**不在** ``app/core/`` 下
----------------------------------------
``app/core/__init__.py`` 会 import ``app.core.orchestrator``，而
``orchestrator.py`` 在**模块级** ``from app.agents import CrawlAgent, ...``。
一旦 agent / service 模块反向 import ``app.core.*``，就形成

```text
app.agents(执行 __init__ 中) → app.core/__init__ → orchestrator → app.agents(半初始化)
```

的循环导入：此时 ``CrawlAgent`` 尚未绑定，抛 ``ImportError``。触发条件是
「任何以 ``app.agents`` / ``app.careerly`` 为首个 app 导入的入口」—— 例如
``pytest tests/test_p4_resume_asset.py`` 单跑（该文件第 38 行先
``from app.agents import resume_agent``）。

放在 ``app/utils/`` 天然避开这条链：``app/utils/**`` 无任何 ``app.core``
引用，``app/utils/__init__.py`` 只导出 logger / helpers / redact。于是
``orchestrator.py`` 一行都不用动。

**请勿**把本模块「整理」回 ``app/core/`` —— 那会让上面那条环复活。
``orchestrator.py`` 的模块级 agents 导入是既有的设计隐患，修它要动引擎的
导入时序，属独立改动。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 自己加载 .env（幂等；默认不覆盖已存在的进程环境变量）。
#
# 为什么必须在这里读一遍：config.py 直到**模块体中部**才 load_dotenv()，
# 而本模块可能在它之前就被导入（例如某入口先 import app.agents）。
# 若不自读，写在 .env 里的 DATA_DIR 会按导入顺序时而生效、时而被忽略 ——
# 这种「取决于谁先 import」的行为极难排查。本仓 LLMService / EmbeddingService /
# JobAdvisor 也都各自调用 load_dotenv()，属既有惯例。
load_dotenv()

__all__ = [
    "REPO_ROOT",
    "DATA_ROOT",
    "APP_DATA_DIR",
    "ROOT_DATA_DIR",
    "TESSDATA_DIR",
    "label_for",
]

#: 仓库根（容器内即 WORKDIR）。**不受 DATA_DIR 影响** —— config.yaml、tessdata、
#: 代码等随仓库走的东西仍按它定位。
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_ENV_DATA_DIR = "DATA_DIR"
_ENV_RAILWAY_VOLUME = "RAILWAY_VOLUME_MOUNT_PATH"


def _configured_data_root() -> Path | None:
    """解析数据根：``DATA_DIR`` 优先，其次 Railway 卷挂载点，都没有则 ``None``。"""
    raw = (os.getenv(_ENV_DATA_DIR) or os.getenv(_ENV_RAILWAY_VOLUME) or "").strip()
    if not raw:
        return None
    # 支持 ~ 与相对路径（相对路径按进程 cwd 解析；systemd 下即 WorkingDirectory）。
    # 生产环境请一律写绝对路径。
    return Path(raw).expanduser()


#: 配置的数据根；``None`` = 未配置，沿用仓库内两棵树（ECS 上的正常默认）。
DATA_ROOT: Path | None = _configured_data_root()

if DATA_ROOT is None:
    # 与今天逐字一致：各模块原先 parent.parent / parent.parent.parent 的结果。
    APP_DATA_DIR = REPO_ROOT / "app" / "data"
    ROOT_DATA_DIR = REPO_ROOT / "data"
else:
    APP_DATA_DIR = DATA_ROOT / "app"
    ROOT_DATA_DIR = DATA_ROOT / "root"

#: OCR 语言包目录（``chi_sim.traineddata``，~2.5MB）—— **固定锚定仓库路径**。
#:
#: ⚠️ 绝不跟随 DATA_DIR：它是随代码仓（以及容器镜像分层）携带的**只读资源**，
#: 而数据盘/卷在运行时才挂载、初始为空。一旦把它重定向到数据根，
#: ``app/agents/resume_agent.py::_apply_tessdata()`` 会因文件不存在而**放弃
#: 设置** ``TESSDATA_PREFIX``，``_ocr_language()`` 也会退回 ``eng``（stock
#: 安装包没有 chi_sim）—— 中文简历 OCR **静默**产出乱码/空文本，且只在部署后
#: 暴露。这正是 ``resume_agent.py`` 顶部注释明令禁止的「失败无痕」。
TESSDATA_DIR = REPO_ROOT / "app" / "data" / "tessdata"


def label_for(path: Path) -> str:
    """把数据目录渲染成报告用的相对标签（**不抛异常**）。

    ``account_purge`` 用 ``"files:" + 相对路径`` 汇报删除命中。未配置数据根时
    相对 ``REPO_ROOT``（与历史输出逐字一致）；配置到数据盘后，该目录已不在
    仓库树下，直接 ``Path.relative_to(REPO_ROOT)`` 会抛 ``ValueError`` ——
    此处依次尝试 ``DATA_ROOT`` 与 ``REPO_ROOT``，都失败则用绝对路径兜底。
    """
    for base in (DATA_ROOT, REPO_ROOT):
        if base is None:
            continue
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()
