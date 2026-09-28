# mimo2api bridge

超级轻量的 **Xiaomi MiMo Desktop → OpenAI 兼容 API** 网关。

- **单文件、零第三方依赖**(纯 Python stdlib,可选 `certifi` 增强 TLS 校验),~470 行
- **自动凭证**:直接读取 MiMo Desktop 的 cookie 库拿 `passToken`,无需手动导入、无需退出 Desktop
- **全链路自动续期**:passToken → 小米 SSO(5 步)→ `serviceToken`,30 分钟缓存,401 自动刷新重试
- **开着代理也能用**(Clash/Surge TUN 均可):SSO 链中云端偶发的灰度回调会被自动钉回生产网关,不受代理分流影响
- 标准接口:`GET /v1/models`、`POST /v1/chat/completions`(流式 + 非流式)、`GET /health`
- 原生支持 `tool_calls`(function calling)与 `reasoning_content`(思考链)透传

## 支持的模型

| 模型 | 说明 |
|---|---|
| `mimo-v2.6-pro` | 新一代 Pro(推荐,带思考链) |
| `mimo-v2.6-flash` | 新一代 Flash(推荐,速度快,默认模型) |
| `mimo-v2.6-pro-ultraspeed` | Pro 极速版 |
| `mimo-x-pro-preview` / `mimo-x-flash-preview` | 旧 Preview 档(上游兼容期,随时可能下线) |

模型列表随上游目录维护,旧模型在云端下线后从 `/v1/models` 移除即可。

## 快速开始

```bash
# 1. 安装并登录一次 MiMo Desktop(拿到 passToken 即可,之后 Desktop 开不开都行)
# 2. 启动
API_KEY=sk-your-key python3 mimo_bridge.py
# 默认监听 127.0.0.1:4500

# 3. 任何 OpenAI 客户端
#    base_url = http://127.0.0.1:4500/v1
#    api_key  = sk-your-key
```

### 配置(环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `4500` | 监听端口 |
| `HOST` | `127.0.0.1` | 监听地址 |
| `API_KEY` | `sk-change-me` | 对外鉴权 key,**务必修改** |
| `MIMO_PASS_TOKEN` | 自动读取 | 手动指定 passToken(优先级最高) |

### 凭证来源(三级 fallback)

1. 环境变量 `MIMO_PASS_TOKEN`
2. 脚本同目录 `mimo_pass_token.json`(`{"passToken": "...", "userId": "...", "cUserId": "..."}`)
3. MiMo Desktop cookie 库自动读取(推荐,零配置)

## 验收速查

```bash
curl http://127.0.0.1:4500/v1/models -H "Authorization: Bearer sk-your-key"

curl http://127.0.0.1:4500/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

## 已知边界

- 后端无 models 列表接口,`/v1/models` 为本地静态表
- `reasoning_content` 原样透传(标准 OpenAI 扩展字段,主流客户端兼容)
- `passToken` 长期有效;若彻底过期,打开 MiMo Desktop 重新登录一次即可(bridge 会自动读到新值)
- 工具结果残留清洗(MiMoML 文本解析 fallback)未实现:正常传 `tools` 时上游返回结构化 `tool_calls`,不经过该降级路径
- `mimo-v2.6-pro-ultraspeed` 在 `/v1/models` 静态表里,但需付费套餐:邀测账号调用返回 400 `chat_model_not_for_plan_tier`(biz_code 41106)

## 常驻运行(launchd 自启 + 崩溃自愈)

手动 `python3 mimo_bridge.py` 在终端关闭/休眠后会挂。推荐注册为 macOS 用户服务:

```bash
# 1. 部署运行文件(注意: launchd 无 ~/Documents 访问权,须放 ~/.local)
mkdir -p ~/.local/share/mimo2api
cp mimo_bridge.py run_bridge.sh ~/.local/share/mimo2api/
xattr -c ~/.local/share/mimo2api/*

# 2. run_bridge.sh 内容
cat > ~/.local/share/mimo2api/run_bridge.sh <<'SH'
#!/bin/zsh
export API_KEY="sk-your-key"
export PORT="4500"
exec /opt/homebrew/bin/python3 "$HOME/.local/share/mimo2api/mimo_bridge.py"
SH
chmod +x ~/.local/share/mimo2api/run_bridge.sh

# 3. 注册 launchd 服务(崩溃自动拉起,开机自启)
#    plist 示例: Label=com.mimo2api.bridge2
#    ProgramArguments = /bin/zsh ~/.local/share/mimo2api/run_bridge.sh
#    RunAtLoad + KeepAlive = true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mimo2api.bridge2.plist

# 4. 验收
curl http://127.0.0.1:4500/health
```

> 坑位记录:launchd 拉起的进程受 macOS TCC 管控,**没有 `~/Documents` 访问权限**,
> 直接指向 Documents 下的脚本会得到 `EX_CONFIG`/`can't open input file`。
> 部署到 `~/.local/share/` 即可绕开。

## Windows(本分支新增)

Windows 上 MiMo Desktop 的 cookie 库同样是**明文**的(`value` 列,`encrypted_value` 为空),不用 DPAPI 解密,
但有两点与 macOS 不同:

1. 路径为 `%APPDATA%\Xiaomi MiMo\Partitions\xiaomi-account\Network\Cookies`(上游脚本只查 macOS/Linux 路径)
2. **Desktop 运行时对该库加独占锁**(`ERROR_SHARING_VIOLATION`,连只读都打不开)→ 上游"无需退出 Desktop"的前提在 Windows 不成立

因此本分支补了两个文件:

| 文件 | 作用 |
|---|---|
| `dump_windows_token.py` | 退出 Desktop 后读一次 cookie 库,导出 `mimo_pass_token.json`(已在 `.gitignore` 中)。导出后 Desktop 开不开都不影响 bridge 启动 |
| `mimo-bridge.ps1` | 单文件启动器 + 计划任务注册(取代 bat/vbs 组合);`-Hidden` 模式输出写入 `bridge.log` |

```powershell
# 1. 首次:完全退出 MiMo Desktop(含托盘图标),导出凭证(可选,但之后就再也不需要退出 Desktop)
python dump_windows_token.py

# 2. 前台启动 / 隐藏启动 / 状态
powershell -NoProfile -File mimo-bridge.ps1
powershell -NoProfile -File mimo-bridge.ps1 -Hidden
powershell -NoProfile -File mimo-bridge.ps1 -Status

# 3. 注册计划任务(登录自启 + 隐藏窗口 + 崩溃自愈:RestartCount 3 / 每 1 分钟)
powershell -NoProfile -File mimo-bridge.ps1 -Install
schtasks /run /tn mimo2api-bridge

# 4. 验收
curl http://127.0.0.1:4500/health
```

默认 `API_KEY=no-key-required`(`-ApiKey` 可改):本机 loopback 客户端如果按"本地服务无需鉴权"的约定填占位符,可以零配置接上。

> 三个坑位记录:
> 1. 计划任务的动作**必须同步等待**子进程(PowerShell 天然如此,`wscript` 的异步 `Run` 不行),
>    否则任务被判定"已完成",`RestartCount`/`RestartInterval` 永远不会触发。
> 2. `schtasks /end` 不会杀掉 bridge 的 python 子进程,要真重启得按端口找 PID 停掉。
> 3. PowerShell 5.1 的 `*>>` 会把原生程序输出写成 UTF-16(日志里全是 `\0`),
>    交给 `cmd /c` 做字节级重定向(`mimo-bridge.ps1` 已处理)。

## License

MIT
