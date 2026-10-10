# YouDub 全流程 / Google Colab

项目确实带 GUI：`apps/web` 是 Next.js 网页，`backend/app/main.py` 是 FastAPI。
原版前后端在同一台计算主机上使用任务队列；现在另有本机网页到 Colab 的远程 worker，见 COLAB_GUI.md。
这个 notebook 从 Colab 直接调用相同的 PipelineRunner，先验证完整处理链，不启动网页服务器。

## 操作

1. 上传 `notebooks/YouDub_Pipeline_Colab.ipynb` 至 Colab，选择 GPU。
2. 第一个代码单元上传 **youdub-pipeline-colab.zip**（不是旧 STT 包）。
3. 安装单元建立专用 venv、保留 Colab CUDA torch/torchaudio，安装 FFmpeg 和中文字体。
4. 预检单元检查模型库导入、GPU、音频解码、字幕滤镜和字体，不下载模型权重。
5. 密钥单元读取 Colab Secrets 的 `MINIMAX_API_KEY`；没有时用隐藏输入框。地址为 `https://api.minimaxi.com/v1`，模型 `MiniMax-M3`。不上传本机 .env，不在 notebook 文本写密钥。
6. 上传短视频（先用 30–90 秒测试），默认 `en-zh`、`both`（中文配音和硬字幕）；也支持 `ja-zh`、`zh-en`。
7. 执行处理单元，实时显示阶段日志；完成后下载结果 ZIP，含最终 MP4、ASR、译文、配音时间轴和 SRT。

GPU：SenseVoiceSmall STT → VoxCPM2 配音；阶段串行并释放模型。不做人声分离，直接用原视频音轨，成品只有配音。
翻译：批量调用 MiniMax-M3，字幕文本会发送到配置的 MiniMax 服务。
音轨提取、视频编码和字幕烧录：Colab CPU / FFmpeg。本机只操作浏览器和下载文件。

## 重试与边界

失败后修复问题，再运行处理单元，会继续同一任务并复用成功阶段。不要重复运行视频上传单元（那会建立新任务）。
续跑依赖当前 Colab 虚拟机中的任务数据库和工作目录；整台运行时被回收后不能只靠 task-state.json 恢复。
若子进程被强制终止，处理单元会使用上一个已结束进程的 PID 请求恢复；CLI 确认该 PID 已不存在后，复用成功阶段并重置未完成阶段。若丢失 notebook 中的 process 对象，则保持拒绝恢复，需先核查进程。
结果 ZIP 不含 API Key、数据库、Cookie、模型权重或日志。它不是恢复整个运行时的备份。
模型首次需要下载；依赖导入测试不能替代真实 GPU 推理。当前机器没有 GPU，完整音视频效果须在 Colab 验收。
原来的原文 SRT 不要上传到网页的“译文字幕”入口，否则会跳过翻译。此 notebook 默认从视频重新执行全部阶段。

## 部署依据

- [Colab FAQ](https://research.google.com/colaboratory/faq.html)：免费运行时通过 Web UI 生成内容可能被终止，因此先使用 notebook 交互。
- [TorchCodec 兼容矩阵](https://github.com/pytorch/torchcodec#compatibility-with-torch-versions)：安装单元按 torch 版本选择；未知版本明确报错。
- [VoxCPM 官方项目](https://github.com/OpenBMB/VoxCPM)：使用 VoxCPM2；Colab 依赖固定 voxcpm 2.0.3。

## 创建 Python 环境失败

新版使用 virtualenv 自带 pip，避免依赖系统 ensurepip；它会检查 venv 中的 pip 和系统包继承配置，修复只残留 bin/python 的不完整环境。创建失败时完整显示子进程输出。遇到旧版 venv 报错，更新 Notebook 和源码包，然后重新运行源码上传、安装单元，不必重置整个运行时。

## 12 GB RAM / T4 的 VoxCPM 初始化

Colab 默认启用 VOXCPM_LOW_MEMORY_INIT=true：构建模型期间暂用 FP16 默认类型，加载结束或失败后恢复全局类型；模型最终精度和 AudioVAE FP32 加载仍由上游代码决定。此模式降低构建参数时的 CPU 内存峰值，不保证运行时不会超内存，须实际 GPU 验证。VOXCPM_OPTIMIZE=false 关闭首次 torch.compile/预热，可能牺牲后续推理速度。原有普通部署默认行为不变。


## Verified Colab run (2026-10-08)

User confirmed acceptable processing speed and final video quality on Tesla T4
(15 GiB VRAM), 12 GiB system RAM, Python 3.13. Source duration: 159.70 seconds.
Settings: VOXCPM_LOW_MEMORY_INIT=true, VOXCPM_OPTIMIZE=false, denoiser disabled.
The log confirms Task succeeded and 48 generated TTS clips.
- Resume start 20:18:18 UTC; success 20:24:47 UTC: 6 min 29 sec.
- TTS stage 20:18:23 to 20:23:56: 5 min 33 sec, including initialization.
- VoxCPM initialization: 49.7 sec; final model dtype remains bfloat16 on CUDA.
- Audio merge: 2 sec; video merge: 49 sec; final MP4 approximately 5.7 MB.
This was a resumed run: import, separation, ASR, sentence fixing, translation,
and reference splitting reused cached outputs. It is not a fresh end-to-end
latency measurement. Keep these settings as the accepted Colab baseline.
The earlier SIGKILL cause remains unproven; this success does not establish OOM.
