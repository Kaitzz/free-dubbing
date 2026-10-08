# 本地设计与翻译配置

## STT 实现

Python FunASR + Google Colab。默认 STT 已改为 SenseVoiceSmall。独立 FSMN-VAD 在 CPU 分段，每次识别最多 30 秒；SenseVoice 在配置的设备上做 CTC 对齐，时间戳加回原音频偏移。汇总所有 VAD 窗口的词级时间戳后统一分段，VAD 边界不强制切字幕；按句末标点、1 秒停顿及 4.5 秒/65 字符上限分段；缺失/非法时间戳明确失败，不伪造均分时间。禁用 ITN 以减少数字等文本变换对对齐的干扰。

参见 COLAB_STT.md 和 notebooks/SenseVoice_Colab.ipynb。历史任务需重做 ASR 阶段以清除旧缓存。旧 Whisper 适配器留作参考，但不再参与默认处理或设备检查。

## 已实现的翻译修改

- 默认使用 MiniMax-M3，OpenAI 兼容地址 https://api.minimaxi.com/v1。
- 在被 Git 忽略的 `.env` 设置 OPENAI_BASE_URL、OPENAI_API_KEY、OPENAI_MODEL。
  密钥不可提交。Colab 部署时使用 Colab Secrets 注入，不分享本机 `.env`。
- M3 请求附带 `thinking: {type: disabled}`，与 MilkyAi 一致；输出预算为 16384 tokens。
- 保留全文预处理，生成摘要、术语和 ASR 纠错建议。
- 翻译按原始顺序组成批次，每批最多 100 个片段、12000 个序列化字符的预算。
  这是字符预算而非精确 token 估算。单个片段超预算时明确报错，不静默裁剪。
- 默认并行 2 批，OPENAI_TRANSLATE_CONCURRENCY 或网页设置可修改。
  原有数据库中的自定义并发数会保留；从旧版本升级时应主动调低旧的 50。
- 请求 `items: [{id, text}]`，返回 `translations: [{id, dst, audio_mode}]`。
  根据整数 ID 恢复顺序；不让模型生成时间戳。
- 校验缺失、重复、未知 ID、空 TTS 译文和输出模式；仅补译未通过校验的片段。
  格式错误或输出截断最多尝试两次，随后对未解决的多片段批次二分重试。
  单片段仍失败就停止任务，不生成部分成功的最终翻译文件。
- 连接/鉴权/限流等服务错误交给 OpenAI SDK 默认重试处理，不因服务错误递归拆批。
- 已存在的 translation 文件和预处理缓存继续复用；需要换模型重译时用现有阶段重做功能。
- 原有已保存数据库配置优先于环境默认值，不静默覆盖已有密钥或供应商。

## 测试

新增 test_translation_batches.py：覆盖 100 句单请求、输出错序、漏项、重复 ID、无效
结构、截断拆批、长度限制、非语言人声模式以及 MiniMax 请求参数。
真实接口冒烟测试只使用人工编写的短句，不上传私人音频或字幕。

本机只安装轻量测试依赖；真实模型/GPU 推理须在 Colab 验证。

字幕密度以短语阅读为目标：用户提供的英文 SRT 片段仅作保序与粗粒度校准，15–18 条是参考密度，并非强制条数。实际处理使用词级时间戳，可进一步切开样例中的长片段，最终条数可能不同。翻译继续批量携带上下文，字幕不需要合并成长句才能翻译。

分段在句末标点或至少 1 秒停顿之间整体选择切分点，保持 4.5 秒/65 字符上限（单个输入词本身超限时保留它）。优先较明显的停顿与逗号，惩罚过短字幕、英文一两个词的尾段及冠词/介词结尾；必要时将前一条的词移到尾段。保留独立短答句和长静音边界，不保证语法分析级的完整短语。新增测试使用合成词级时间戳验证孤立尾词场景，不将 SRT 内均分时间冒充真实对齐。

## 全流程 Colab

新增 YouDub_Pipeline_Colab.ipynb，复用原 PipelineRunner，从本地视频跑完整 Demucs → SenseVoice → MiniMax-M3 → VoxCPM2 → FFmpeg。提供实时阶段日志、同运行时失败续跑、成品白名单导出。独立 notebook 已验证全流程；原 GUI 远程执行接入见 COLAB_GUI.md。详见 COLAB_PIPELINE.md。


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

## Original GUI with Colab worker (2026-10-08)

Implemented a local authenticated coordinator, restricted Cloudflare Tunnel gateway, and notebook-driven pull worker. User explicitly approved transport of task video/audio/subtitles and returned results; cookies and local API keys are excluded. MiniMax credentials remain in Colab Secrets. Checkpoints are returned after each stage; original GUI manual/automatic modes and retry/redo controls remain available. Local transport tests pass and live GUI login/download plus public-route isolation have been checked. A real Colab-to-local video job still needs user validation.
