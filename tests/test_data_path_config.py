"""``DATA_DIR`` 与 ``app/utils/paths.py`` 的行为契约。

三件事必须锁住：

1. **未设 ``DATA_DIR`` 时与今天逐字一致** —— 两棵树仍在仓库内。这是「生产上
   不设这个变量就等于回滚」这条回滚保证的依据。
2. **设了则两棵树原样搬迁**，且 ``tessdata`` **不跟随**（跟随会让中文 OCR
   静默退化成默认语言包，见 ``paths.TESSDATA_DIR`` 的注释）。
3. **``label_for`` 不抛异常** —— ``account_purge`` 删账号时用它渲染目录标签，
   数据根在仓库外时旧写法 ``relative_to`` 会 ``ValueError`` → 用户删账号 500。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import app.utils.paths as paths

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _reload_paths(monkeypatch: pytest.MonkeyPatch, data_dir: str | None):
    """在指定的 ``DATA_DIR`` 环境下重新求值 ``app.utils.paths`` 的模块级常量。"""
    if data_dir is None:
        monkeypatch.delenv("DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("DATA_DIR", data_dir)
    # 兜底变量会盖过「未配置」这一分支，必须一并摘掉。
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    return importlib.reload(paths)


def test_unset_keeps_repo_layout(monkeypatch):
    """未配置 → 两棵树留在仓库内（生产上「不设即回滚」）。"""
    p = _reload_paths(monkeypatch, None)

    assert p.DATA_ROOT is None
    assert p.APP_DATA_DIR == _REPO_ROOT / "app" / "data"
    assert p.ROOT_DATA_DIR == _REPO_ROOT / "data"
    assert p.TESSDATA_DIR == _REPO_ROOT / "app" / "data" / "tessdata"


def test_configured_relocates_both_trees(monkeypatch, tmp_path):
    """设了 DATA_DIR → 两棵树原样搬到该根下，子目录名不变。"""
    p = _reload_paths(monkeypatch, str(tmp_path))

    assert p.DATA_ROOT == tmp_path
    assert p.APP_DATA_DIR == tmp_path / "app"
    assert p.ROOT_DATA_DIR == tmp_path / "root"


def test_tessdata_never_follows_data_dir(monkeypatch, tmp_path):
    """tessdata 是随代码仓发布的只读资源，**绝不**跟随数据根。

    跟了的话 _apply_tessdata() 的存在性检查会静默失败、不设 TESSDATA_PREFIX，
    OCR 悄悄退回默认语言包 —— 只在部署后才暴露。
    """
    p = _reload_paths(monkeypatch, str(tmp_path))

    assert p.TESSDATA_DIR == _REPO_ROOT / "app" / "data" / "tessdata"
    assert not str(p.TESSDATA_DIR).startswith(str(tmp_path))


def test_railway_volume_used_as_fallback(monkeypatch, tmp_path):
    """DATA_DIR 未设时，平台注入的卷挂载路径作为兜底。"""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    p = importlib.reload(paths)

    assert p.DATA_ROOT == tmp_path
    assert p.APP_DATA_DIR == tmp_path / "app"


def test_label_for_never_raises(monkeypatch, tmp_path):
    """数据根在仓库树之外时，label_for 必须仍能渲染标签而非抛 ValueError。"""
    p = _reload_paths(monkeypatch, str(tmp_path))

    # 仓库外的目录 —— 旧写法 directory.relative_to(REPO_ROOT) 会在这里炸。
    outside = tmp_path / "app" / "outputs"
    outside.mkdir(parents=True)

    label = p.label_for(outside)
    assert label == "app/outputs"          # 相对 DATA_ROOT
    assert not Path(label).is_absolute()

    # 仓库内的目录 → 相对 REPO_ROOT，与历史输出逐字一致。
    assert p.label_for(_REPO_ROOT / "app" / "data" / "outputs") == "app/data/outputs"


def test_account_purge_constants_resolve_under_data_dir(monkeypatch, tmp_path):
    """在配置了数据根的环境下重算 account_purge 的常量：两个同名 graph_state.db
    必须继续分离到两棵树，且全部落在数据根之内。
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    # paths 必须先重载，account_purge 才会在重载时拿到新常量。
    importlib.reload(paths)
    purge = importlib.reload(importlib.import_module("app.core.account_purge"))
    try:
        assert purge.AUTH_DB == tmp_path / "app" / "auth.db"
        assert purge.CAREERLY_DB == tmp_path / "app" / "careerly.db"
        assert purge.ENGINE_DB == tmp_path / "app" / "graph_state.db"
        assert purge.DEMO_DB == tmp_path / "root" / "graph_state.db"
        assert purge.ENGINE_DB != purge.DEMO_DB
    finally:
        # 复原全局状态，免得污染同进程内的其他用例。
        monkeypatch.undo()
        importlib.reload(paths)
        importlib.reload(purge)


def test_agents_first_import_has_no_cycle():
    """回归：以 app.agents 为首个 app 导入的入口不得触发导入环。

    app/core/__init__.py → orchestrator → 模块级 ``from app.agents import ...``；
    若 paths 放在 app/core/ 下，agents 反向 import 它就会成环。
    必须在**全新解释器**里验证 —— 本进程早已导入过 app.core，测不出来。
    """
    code = "import app.agents.resume_agent as m; print('OK', m._LOCAL_TESSDATA)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"导入环复活：\n{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout
