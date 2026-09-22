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

## 致谢

协议还原过程参考了 [Fly143/xiaomi-mimo-desktop-api](https://github.com/Fly143/xiaomi-mimo-desktop-api) 的公开文档,其 SSO 时序与本机逆向实测完全一致。本项目为其超轻量 Python 单文件实现(实时读 cookie 库,免导入、免退出 Desktop)。

## License

MIT
