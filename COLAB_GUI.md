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

用户已授权任务视频、阶段音频和字幕传入自己的 Colab并回传结果。仅 YouTube 下载阶段会发送 GUI 保存的 YouTube Cookie；本机 API 密钥不传输。
YouTube 视频由 Colab 下载，Cookie 在 GUI 设置中粘贴更新即可；不能使用本机代理端口。
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

## 在 GUI 更新 Chrome Cookie

使用 yt-dlp FAQ 链接的 Get cookies.txt LOCALLY 扩展导出 youtube.com 的 Netscape 格式内容，粘贴到 GUI 设置的 YouTube Cookie 并保存。每次领取 YouTube 下载阶段时，Colab 获得最新 Cookie（优先于 Secret）；不会进入任务 ZIP 或回传结果，其他域条目过滤。过期后只需在 GUI 更新并恢复失败任务，无需重启 Colab。此前用户选择不传本机 Cookie，现已明确改为允许 GUI 管理并传至 Colab。视频仍完全由 Colab 下载。


## Colab 性能和日志

Worker 默认将 Demucs 外层音频分块设为 180 秒（原为 60 秒），模型内部的分段和重叠算法不变；可用 `DEMUCS_CHUNK_SECONDS` 环境变量覆盖。较长外层分块会增加一些内存占用。

合成视频默认尝试 NVIDIA NVENC：先进行一次实际编码探测，失败则使用 CPU libx264 / veryfast；实际视频编码若失败也会退回 CPU。字幕仍在 CPU 上绘制。可用 `DUBBING_VIDEO_ENCODER=cpu` 恢复原来的 libx264 / fast。NVENC CQ 23 与 x264 CRF 23 并非相同的画质尺度，需用实际视频比较画质和文件体积。已混合成 AAC 的音轨直接复用，避免二次有损编码。

VoxCPM 现默认使用固定参考缓存、8 步推理和温和响度匹配；仍关闭首次编译预热。

Notebook 只显示阶段消息、警告和错误，不再显示模型的逐帧进度条。完整子进程日志位于打印出的 Colab `remote-runs/<lease>/worker.log`，随 Colab 运行时销毁，不写入 GitHub。失败时会显示最后 60 行。GUI 仍通过心跳更新阶段进度。

每个阶段现在分别显示输入传输、子进程运行、检查点打包回传的耗时与传输大小；GUI 中的阶段计时不含全部这些开销。当前仍采用逐阶段完整检查点传输，这些数字可帮助判断后续是否需要增量传输。


可选：在 Colab 左侧 Secrets 新建 `HF_TOKEN`，填入 Hugging Face 的 Read token，并开启该启动 Notebook 的访问权限。工作 Notebook 自动读取并通过环境变量传给 Worker 及模型子进程；不需要更换启动壳。未配置时继续匿名访问，已配置但未授权时提示开启权限。此 token 用于 Hugging Face 下载认证，不会加速 GPU 推理，也不用于 ModelScope 下载。


## YouTube 封面和 CC

新下载的任务会尝试保存视频封面及当前源语言的 CC（YouTube 任务当前配置为英语）。优先人工字幕，其次原语言自动字幕；排除带 `tlang` 的机器翻译字幕。无可用字幕、字幕下载失败或无法解析时继续 SenseVoice。素材下载复用 yt-dlp 的 Cookie、代理与网络会话配置。

支持 JSON3 / VTT / SRT，自动字幕按重叠时间去除滚动重复文本，整理为不重叠的时间段，后续仍进行 MiniMax 翻译、配音及背景音分离。原文 CC 不走“用户上传的已翻译 SRT”路径。封面和清理后的原文 SRT 随任务检查点回传，在任务详情“原始素材”中预览或下载。

已完成下载阶段的旧任务不会自动补抓；新建任务或从下载阶段重跑才启用素材获取。更新后需重启本机 GUI（运行 `scripts/start_colab_gui.ps1`），并重新运行启动 Notebook，才能同时使用界面与 Colab 的新功能。


## 配音一致性与速度

默认 `VOXCPM_REFERENCE_MODE=fixed`：按已有 speaker 标签选择一段音量相对平稳、有效时长较长的参考，最多读取/保留 6 秒，排除静音与原声保留片段。选择依据是时长、有效帧比例、音量变化与削波比例，不是语义或口音识别。每个标签仅构建一次 prompt cache，并用于该标签的所有新配音。

当前 SenseVoice/CC 没有可靠的多人分离，往往统一标为 speaker=1，因此本模式主要适合单人讲述；多人视频仍需要可靠说话人标签才能分别固定声音。缺少某标签的可用参考时明确报错，不借用其他人的参考。

`VOXCPM_INFERENCE_TIMESTEPS=8` 为新默认，`VOXCPM_CFG_VALUE=2.0` 保持不变。`VOXCPM_MATCH_LOUDNESS=true` 对新生成音频进行有效帧 RMS 匹配（目标约 -20 dBFS，常规增益限制 ±4 dB，峰值限制 -1 dBFS），不修改时长，跳过近乎静音内容，不处理原声保留片段。这不是 LUFS 标准化，也不能保证消除全部听感或口音差异。

参考文件在 `tmp/tts_references`，随从配音阶段重跑一起清理。已有配音缓存不会自动覆写或重复调整音量；比较新旧效果应从“生成配音”重跑及其后续阶段。全部配音缓存存在时无需重新加载模型。若需旧模式，可设 `VOXCPM_REFERENCE_MODE=segment`、`VOXCPM_INFERENCE_TIMESTEPS=10`、`VOXCPM_MATCH_LOUDNESS=false`。环境变量显式配置优先于默认值。


Colab 每阶段的 `workfolder/.../metadata/` 保留字幕产物：下载原始 CC 位于 `raw_subtitles/`（包括取回后解析失败的文件），清理后 CC 为 `source_subtitles.json/.srt`，识别/分句为 `asr.json`、`asr_fixed.json`。Worker 额外导出 `transcript.txt/.srt`，优先采用分句结果，并打印目录。这些文件随检查点回传本机，翻译失败也保留。Colab 运行时删除后 `/content` 文件会消失，尚未自动挂载 Google Drive。
