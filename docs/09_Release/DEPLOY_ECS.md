# 生产部署（阿里云 ECS）— 密钥注入与硬性配置

> 基础部署步骤（装 Python、拉代码、建 venv、起进程）见仓库根 `HOW_TO_DEPLOY.md`
> §1 方案 C。**本文只讲一件事**：生产环境的密钥与安全配置怎么落到服务器上，
> 以及为什么不能像开发那样放在 `.env` 里。

决策依据：`TODO.md` R0-2 / R0-3（2026-10-06 发布基线）。部署目标：阿里云 ECS（D121）。

---

## 1. 四条硬性配置

`ENV=production` 时，下面四项**缺任何一项进程都拒绝启动**。这是刻意设计的：
每一项缺失都对应一个「服务能跑、但等同于没有防护」的形态，而那种形态会在
日志里安静地跑上几个月没人发现。

| 变量 | 生产取值 | 缺失的后果 |
|---|---|---|
| `ENV` | `production` | 不触发任何检查（**最危险的一项**） |
| `AUTH_MODE` | `formal` | 生产保留手机号直登 = 知道手机号即可接管账号 |
| `JWT_SECRET_KEY` | ≥32 字符随机串 | 用默认值 = 任何人可伪造任意账号的 token |
| `CORS_ALLOW_ORIGINS` | 逗号分隔来源 | 回退 `["*"]` = 任何站点可从用户浏览器调用本 API |

前三项的检查在 `app/core/auth_mode.py` 与 `app/core/security.py` 的**导入期**，
第四项在 `app/api/server.py`。异常类型统一由 `app/core/config_guard.py` 定义。

**切 `AUTH_MODE=formal` 之前必须先清空历史无密码账号**，否则那些账号的数据
会留在一个谁都无法再认证、也不会被任何注销流程触发的身份下：

```bash
python scripts/purge_passwordless_accounts.py            # 预演
python scripts/purge_passwordless_accounts.py --apply    # 执行（先做备份）
```

生成密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 2. 密钥不进 `.env` 文件，走 systemd `EnvironmentFile=`

开发时 `.env` 很方便，生产上它有三个问题：

1. 文件权限通常跟着仓库权限走（`644`），同机器上任何用户都能读；
2. 它和代码在同一个目录树里，`git add -A` 的一次失误就会把密钥带进历史；
3. 没有版本化的权限审计 —— 谁改过、什么时候改的，查不到。

改用 systemd 的 `EnvironmentFile=`：文件在仓库之外、权限 `600`、只有 root 能读，
且随服务生命周期一起被 systemd 管理。

```bash
sudo install -m 600 -o root -g root /dev/null /etc/careerly/backend.env
sudo nano /etc/careerly/backend.env
```

`/etc/careerly/backend.env` 内容（**不要**提交到任何仓库）：

```ini
ENV=production
AUTH_MODE=formal
JWT_SECRET_KEY=<用上面的命令生成，48 字符>
CORS_ALLOW_ORIGINS=https://<你的域名>
DASHSCOPE_API_KEY=<百炼控制台获取>
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

`/etc/systemd/system/careerly-api.service`：

```ini
[Unit]
Description=Careerly API
After=network-online.target

[Service]
Type=simple
User=careerly
WorkingDirectory=/opt/careerly/work_agent
EnvironmentFile=/etc/careerly/backend.env
ExecStart=/opt/careerly/work_agent/venv/bin/uvicorn app.api.server:app \
    --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now careerly-api
sudo systemctl status careerly-api
journalctl -u careerly-api -n 50 --no-pager
```

### 绑定 127.0.0.1，不要绑 0.0.0.0

`--host 127.0.0.1` 是刻意的：让 Nginx 反代成为唯一的对外入口。直接绑
`0.0.0.0` 会让 8000 端口对整个公网开放，绕过 HTTPS 与任何反代层的限制。
安全组里也**不要**开 8000。

---

## 3. 必须持久化的目录

这些目录不在 git 里，但删掉就等于用户数据没了。ECS 上用**独立数据盘**挂载，
不要放在系统盘 —— 系统盘随实例重建一起消失：

```text
app/data/careerly.db        用户的职业资产（简历版本、项目、成长记录）
app/data/auth.db            账号与凭据
app/data/graph_state.db     引擎会话轨迹
data/graph_state.db         DemoGuard 试用计数
app/data/outputs/           分析产物（报告 / 匹配结果；含 resume_parsed_*.json）
app/data/uploads/           上传原件
data/resumes/  data/uploads/
```

⚠️ **`app/data/outputs/` 下的产物无法按用户定位**：文件名只带 `session-<hex>`，
而 `graph_state.db` 的 `sessions.user_id` 实测全为 `NULL`，且历史 session 行已被
清理。后果是「删除账号」**删不到这些文件**（`app/core/account_purge.py` 会如实
报告数量）。上线前需要产品侧定一个口径：要么今后的产物文件名带上 `user_id`，
要么给产物定一个保留期。已记入 `TODO.md`。

---

## 4. 备份

```bash
# 手动跑一次
scripts/backup_sqlite.sh

# 每天 03:17（用 systemd timer 或 crontab）
17 3 * * * /opt/careerly/work_agent/scripts/backup_sqlite.sh >> /var/log/careerly-backup.log 2>&1
```

⚠️ 备份必须用 `sqlite3 .backup`，**不能 `cp`** —— 库跑在 WAL 模式下，
`cp` 拿到的是缺了 `-wal` 的损坏快照（能打开、但数据是旧的或残缺的）。
详见 `scripts/backup_sqlite.sh` 的说明与 `RESTORE_DRILL.md`。

---

## 5. 上线后自检

```bash
# 服务确实起来了（而不是被 fail-fast 拦住）
systemctl is-active careerly-api
journalctl -u careerly-api | grep "Auth boundary"
#   → 应看到 AUTH MODE = FORMAL | ENV = production

# 缺配置确实起不来（在**测试机**上验证，别在生产上试）
sudo systemctl stop careerly-api
sudo sed -i '/JWT_SECRET_KEY/d' /etc/careerly/backend.env
sudo systemctl start careerly-api   # 期望：启动失败
journalctl -u careerly-api -n 20    # 期望：InsecureSecretConfiguration
sudo systemctl stop careerly-api
sudo sed -i '1i JWT_SECRET_KEY=<原值>' /etc/careerly/backend.env
sudo systemctl start careerly-api
```

> 上面这段「把密钥删掉看它是不是真的会挂」是**唯一**能证明 fail-fast 生效的方式。
> 只读代码不算验证 —— 检查可能被人挪走、可能因为环境变量名拼错而永不触发。

---

## 6. 仍未确定的部分

* **域名**：`CORS_ALLOW_ORIGINS` 与 HTTPS 证书依赖它，未定（D120 占位口径）。
* **HTTPS**：由 Nginx + 证书承担，本文未展开；App 侧不接受明文 HTTP
  （`AndroidManifest.xml` 未开 `usesCleartextTraffic`）。
* **真实 ECS 部署**：尚未执行 —— 本文是操作说明，不是部署完成的记录。
