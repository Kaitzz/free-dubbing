# 原 GUI + Colab worker

## 本机

运行 `powershell -ExecutionPolicy Bypass -File scripts/start_colab_gui.ps1`。
它会启动 127.0.0.1:3000 的原 GUI、8000 后端、8011 专用网关，以及 Cloudflare Tunnel。
仅 `/api/colab-worker/*` 通过网关暴露，所有操作需要 GUI 生成的专用 Bearer 密钥。
连接地址在 `data/gui/connection.json`。本机 GUI 直接打开，无需登录密码；启动脚本启用 YOUDUB_LOCAL_GUI=true，后台保留本机来源检查和自动 CSRF 保护。公网 worker 仍使用独立连接密钥。
关闭启动进程会关闭它启动的服务。日志在 data/gui。存在 data/gui/cloudflare-token.txt 时使用固定 Tunnel，默认地址 https://dubbing.corneliazhang.me，可用 YOUDUB_TUNNEL_URL 覆盖。token 文件只保存在本机，不提交 Git。首次迁移时关闭之前单独启动的 cloudflared 窗口，随后只运行 GUI 启动脚本。没有 token 文件时才回退到临时 Tunnel，地址会随重启改变。

## Colab

1. [在 Colab 打开 Notebook](https://colab.research.google.com/github/Kaitzz/free-dubbing/blob/main/notebooks/YouDub_GUI_Colab.ipynb)，选择 GPU 运行时。首个代码单元自动从 Kaitzz/free-dubbing 获取固定提交，无需下载或上传源码包。可以保存到 Drive 后重复使用。
2. 安装与预检沿用已验证的配置。MiniMax 密钥通过 Colab Secrets 提供，不传输本机 API 密钥。
3. 首次在本机 GUI 的「Colab 连接」生成连接密钥，保存到 Colab Secrets 的 DUBBING_WORKER_TOKEN 并开启 Notebook 访问权限。Notebook 自动读取密钥，固定地址 https://dubbing.corneliazhang.me 已内置；无需每次输入或重新生成密钥。若主动轮换密钥，再更新 Secret。
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

## GitHub 源码版本

Notebook 当前固定 SOURCE_COMMIT=b8a3611a0fa815429f5cf353c9ebf34cd018377b，与已推送的本机流水线匹配。打开链接从 main 获取 Notebook，但执行代码来自指定提交，不会自动升级。新运行时需重新获取源码和安装依赖；同一运行时复用检出的版本，版本之间共用 model-cache。之前 /content/youdub-pipeline/model-cache 存在时也会复用。下载模型仍可能在首次运行时发生。

升级时先停止 worker，更新本机和 SOURCE_COMMIT 到相同的已验证版本，再重新运行安装及预检单元。固定 Tunnel 路由须在 Cloudflare 配置为 HTTP → 127.0.0.1:8011。仓库需要能被 Colab 读取；当前使用公开 HTTPS 读取，不需要 GitHub token，也不向 GitHub 上传任务素材。
