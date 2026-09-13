# firefox-auto-turnstile

用一个**干净浏览器**替高风险浏览器通过 Cloudflare Turnstile 验证，并把 Token 回传给后者提交。

```
高风险浏览器(已屏蔽 challenges.cloudflare.com)          本容器（干净环境）
──────────────────────────────────          ──────────────────────────────────
POST /solve {url, sitekey}  ──────────────▶  API :8081
                                             │ 写 task.json
                                             │ xdotool 驱动 Firefox 打开 url
                                             ▼
                              Firefox ──(代理 127.0.0.1:8080)──▶ mitmproxy
                                │                                   ├─ 目标站文档请求 → 返回注入页
                                │                                   │   (地址栏仍是真实域名)
                                │                                   └─ 其他流量照常转发
                                └─(代理例外,直连)──▶ challenges.cloudflare.com
                                     Turnstile 渲染 → 人工/自动完成验证
                                     页面回调 fetch('/.relay-token/?t=...')
                                             │ mitmproxy 捕获 → result.json
token  ◀────────── {ok:true, token} ─────────┘ API 轮询到结果,返回调用方
填入表单 → 提交(无需访问 Cloudflare) → 目标站 siteverify 验证通过
```

原理：Turnstile 的 Token 与 sitekey 绑定的 hostname 校验，与提交它的浏览器无关。mitmproxy 只替换目标站的**文档响应**（让地址栏停留在真实域名上），而 Turnstile 自身的流量走 Firefox 直连（TLS 指纹干净、不经 MITM），因此验证体验与真实访问一致。高风险浏览器提交 Token 时不需要访问 Cloudflare，所以屏蔽不影响最终提交。

> ⚠️ 仅用于你拥有/已获授权的网站的验证调试与自动化研究。Token 单次有效、5 分钟过期，请勿用于绕过他人的访问控制。

## 快速开始

```bash
docker run -d --name turnstile-relay \
  --shm-size 2g \
  -p 8081:8081 \
  -p 127.0.0.1:5800:5800 \
  -v $(pwd)/data:/config \
  ghcr.io/<owner>/firefox-auto-turnstile   # 替换为你的仓库小写全名
```

或 `docker compose up -d`。

镜像为多架构（`linux/amd64`、`linux/arm64`），由 GitHub Actions 原生 runner 构建并推送到 ghcr.io。

## 使用

```bash
# 长轮询直到 Token 产出（默认 180s，可传 timeout）
curl -X POST http://127.0.0.1:8081/solve \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://target-site.example/login","sitekey":"0x4AAAAAAA...","timeout":120}'
# → {"ok": true, "task_id": "...", "hostname": "target-site.example",
#    "token": "XXXX...", "elapsed": 42.1}

# 状态/健康检查
curl http://127.0.0.1:8081/status
curl http://127.0.0.1:8081/healthz
```

拿到 Token 后在高风险浏览器里填入隐藏域并提交表单：

```js
// 高风险浏览器侧（如经 userscript/自动化脚本）
const inp = document.querySelector('input[name="cf-turnstile-response"]');
inp.value = TOKEN;
inp.form.submit();  // 或触发站点自身的提交逻辑
```

需要人工点击时，用浏览器打开 `http://<host>:5800`（noVNC）操作验证组件。

## 点击校准（复选框位置记忆）

第一次对某个站点过验证时，你在 noVNC 里点击复选框的那一下会被系统**记录为该站点的复选框坐标**：

- 容器内的 `xinput` 监听器捕获点击的屏幕坐标（X server 层面，跨域 iframe 不影响）；
- Token 返回后，注入页会显示 `Click position recorded: x=…, y=…`，API 响应里也带 `click: {x, y}` 与 `calibrated: true`；
- 坐标按 hostname 持久化到 `/config/relay/coords.json`，之后可查询：

```bash
curl http://127.0.0.1:8081/coords
# {"ok": true, "coords": {"target.example": {"x": 640, "y": 312, ...}}}
```

这些坐标是为后续自动点击（`xdotool mousemove x y click`）准备的示教数据。注意坐标与分辨率绑定——改 `DISPLAY_WIDTH/HEIGHT` 后需重新校准。

### 参数

| 字段 | 说明 |
|---|---|
| `url` | 目标站点上**带 Turnstile 的页面的完整 https 地址**，决定地址栏 hostname 与拦截路径 |
| `sitekey` | 目标页面 Turnstile 组件的 sitekey（`0x4…` 开头，从页面 HTML 源码可查） |
| `timeout` | 可选，5–300 秒，默认 180 |

### API 错误

| HTTP | 场景 |
|---|---|
| 400 | 参数缺失/格式错误（url 必须是 https、sitekey 格式非法、timeout 越界） |
| 409 | 已有任务进行中（单任务模型） |
| 503 | xdotool 无法驱动 Firefox（窗口未就绪） |
| 504 | 超时未取到 Token |

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `VNC_PASSWORD` | （无） | 强烈建议在暴露 5800/5900 前设置 |
| `DISPLAY_WIDTH/HEIGHT` | 1920×1080 | 分辨率，建议 1280×800 起步 |
| `TZ` | UTC | 时区 |

mitmproxy 的 CA 证书在容器**首次启动时生成**到 `/config/mitmproxy/`（持久卷），不打进镜像——公开镜像的任何人都不应能 MITM 你的部署。策略文件通过 Firefox 企业策略（`policies.json`）注入信任与代理配置，代理仅监听容器内 `127.0.0.1:8080`。

## 测试

官方提供跨域名可用的测试 sitekey（见 [Turnstile Testing 文档](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)）：

| Sitekey | 行为 |
|---|---|
| `1x00000000000000000000AA` | 总是通过（可见） |
| `1x00000000000000000000BB` | 总是通过（不可见，全自动） |
| `3x00000000000000000000FF` | 强制交互挑战 |

```bash
# 全链路冒烟（不可见测试键，无需点击），url 可随便给——文档会被替换
curl -X POST http://127.0.0.1:8081/solve \
  -d '{"url":"https://example.com/login","sitekey":"1x00000000000000000000BB"}'
```

CI（`.github/workflows/docker-build.yml`）在每次构建时在 arm64 runner 上跑同样的冒烟测试。推 PR 到仓库也会自动跑。

## 架构细节（仓库内实现）

- `src/relay_addon.py` — mitmproxy addon：拦截目标站文档请求返回注入页；捕获 `/.relay-token/` 回调写 `result.json`；任务 6 分钟后过期。
- `src/api_server.py` — 标准库 HTTP API（8081）：校验参数、写 task.json、驱动 Firefox、长轮询结果。
- `src/nav.sh` — xdotool 导航（聚焦窗口 → ctrl+l → 输入 URL → 回车）。
- `rootfs/usr/lib/firefox/distribution/policies.json` — 信任 mitmproxy CA、强制代理（`challenges.cloudflare.com` 直连例外）。
- `rootfs/etc/services.d/{mitm,api}/` — jlesage 服务定义（跟随容器以 `USER_ID` 运行）。
- `rootfs/etc/cont-init.d/99-relay-init` — 首启生成 per-container CA。

## 局限与路线

- v1 单任务串行；首次过验证需人工点击（noVNC），点击坐标已被记录校准——自动点击（用记录的坐标 `xdotool click`）、并发队列、pre-clearance 模式在 TODO。
- 部署 IP 的信誉影响 Turnstile 难度；本方案已保证 Turnstile 流量直连（不走 MITM、指纹干净），但仍建议部署在干净网络。
- 返回 `token` 后请尽快提交（5 分钟/单次）。

## 安全须知

- 不要把 5800/5900 暴露到公网（或至少设置 `VNC_PASSWORD`）。API 8081 同样建议只在内网/加防火墙。
- mitmproxy 只在容器内回环地址监听，不会出现在端口映射里。
