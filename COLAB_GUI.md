# 原 GUI + Colab worker

## 本机

运行 `powershell -ExecutionPolicy Bypass -File scripts/start_colab_gui.ps1`。
它会启动 127.0.0.1:3000 的原 GUI、8000 后端、8011 专用网关，以及 Cloudflare Tunnel。
仅 `/api/colab-worker/*` 通过网关暴露，所有操作需要 GUI 生成的专用 Bearer 密钥。
连接地址在 `data/gui/connection.json`。本机 GUI 直接打开，无需登录密码；启动脚本启用 YOUDUB_LOCAL_GUI=true，后台保留本机来源检查和自动 CSRF 保护。公网 worker 仍使用独立连接密钥。
关闭启动进程会关闭它启动的服务。日志在 data/gui。存在 data/gui/cloudflare-token.txt 时使用固定 Tunnel，默认地址 https://dubbing.corneliazhang.me，可用 YOUDUB_TUNNEL_URL 覆盖。token 文件只保存在本机，不提交 Git。首次迁移时关闭之前单独启动的 cloudflared 窗口，随后只运行 GUI 启动脚本。没有 token 文件时才回退到临时 Tunnel，地址会随重启改变。

## Colab

1. 首次将 data/gui/Dubbing_Launcher.ipynb 上传到 Colab 并保存为私有 Drive 副本。选择 GPU，运行全部单元。以后复用这一个启动壳，它自动获取 GitHub 最新工作 Notebook 和匹配源码。
2. 安装与预检沿用已验证的配置。MiniMax 密钥通过 Colab Secrets 提供，不传输本机 API 密钥。
3. 连接密码为固定 8 位数字，仅在本机 GUI 中主动保存新密码时才改变。私有启动壳的 DUBBING_PASSWORD 填相同值，无需 DUBBING_WORKER_TOKEN Secret。MiniMax API 仍使用 MINIMAX_API_KEY Secret。
4. 运行领取任务单元，回到 GUI 创建任务、查看进度、继续/重做、播放/下载视频。

用户已授权任务视频、阶段音频和字幕传入自己的 Colab并回传结果。不发送 Cookie、本机 API 密钥。
需要 Cookie 的网页视频建议先下载成文件再上传；公开链接由 Colab 下载，不能使用本机代理端口。
GUI 中翻译地址/模型/并发会传给 worker；GUI 密钥只用于本机功能，worker 使用自己的 Colab Secret。

## 运行机制与边界

一次领取一个阶段，完成后回传整份阶段检查点，再领取下一阶段。自动模式连续执行，手动模式等待 GUI 继续。
临时 Tunnel 不支持 SSE，这里使用轮询和心跳；上传按 8 MiB 分块。
断线约 3 分钟后任务标记失败；已收到的检查点保留，可通过 GUI 恢复，迟到的 lease 不能覆盖新任务。
当前每个阶段会传输检查点音视频文件，并在 Colab 启动新进程，因此比单机 notebook 多网络/磁盘耗时。
本机和 Colab 会保留历史检查点用于排查；长视频需要足够磁盘空间，当前归档上限 16 GiB。
原先手工 notebook 任务不会自动出现在本机数据库中；GUI 任务需要从网页创建。
Colab 计算单元需保持运行。本机需保持联网；本实现不会规避 Colab 的使用限制。

原 GUI 的模型处理逻辑沿用已有适配器：SenseVoice、批量 MiniMax-M3、VoxCPM2 低内存初始化且关闭编译。

## 稳定启动壳

个人启动文件位于 data/gui/Dubbing_Launcher.ipynb（Git 忽略，含固定连接密码）。首次上传到 Colab 并保存为私有 Drive 副本，以后运行同一个副本即可。notebooks/Dubbing_Launcher.ipynb 是无密码的公开模板。
启动壳先将 main 解析为提交 SHA，再获取该提交的工作 Notebook；工作 Notebook 同时检出这一提交的源码。DUBBING_VERSION 可设为完整 SHA 回退。运行中的任务不会热更新。
私有启动壳只执行代码单元，输出包含工作单元编号与提交版本，错误立即停止。工作 Notebook 可以修改安装/预检/领取任务逻辑，无需更换私有壳。
修改固定密码时，在 GUI 保存新值并同步私有壳的 DUBBING_PASSWORD。本地数据库只保存哈希，GUI 和 Tunnel 重启不轮换密码。连续认证失败会短暂限制重试。

## YouTube Cookie（Colab 专用）

启动壳可从 Colab Secrets 的 YOUTUBE_COOKIES 读取完整 Netscape cookies.txt 内容（非 API token、非 Cookie 请求头）。需开启 Notebook access。没有此 Secret 时继续匿名下载，本地视频不受影响；已有 Secret 但未授权时会提示开启权限。旧启动壳也可由更新后的工作 Notebook 读取。

只有 YouTube 任务会将 Cookie 写入其 Colab data/cookies/youtube.txt，文件权限私有，子进程不继承 Cookie 内容环境变量。文件位于 session 检查点以外，阶段结束删除，不上传 GitHub、不回传本机，也不读取本机浏览器 Cookie。非 YouTube 域的条目会过滤。Cookie 可能过期，也不保证能通过 YouTube 的机器人检查。
