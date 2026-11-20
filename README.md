# 剪映九宫格开场

提交一段或多段视频，自动选取每段约 2 秒，生成剪映专业版可编辑草稿。拼片从黑底依次展开，最后合拢成全屏。仓库内含原创电子节拍，提供视频即可自动配音轨。

## 安装

需要 **Windows 版剪映专业版**、Python 3.10+，以及支持 `scale.reset_sar` 的 FFmpeg 和 FFprobe。先启动一次剪映并确认草稿保存位置。公开版使用 [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)，Python 依赖见 `requirements.txt`。

在 PowerShell 中运行以下命令，把完整仓库装到 [Codex 个人技能目录](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

```powershell
$skill = Join-Path $env:USERPROFILE '.agents/skills/nine-grid-opening'
git clone https://github.com/zhengtangbo/nine-grid-opening.git $skill
python -m pip install -r "$skill/requirements.txt"
```

如果从 GitHub 下载 ZIP，将整个仓库解压到同一技能目录，确保解压后的 `SKILL.md` 和 `assets`、`scripts` 同级。在有本地文件和剪映访问权限的 Codex 中上传素材，输入「用 `$nine-grid-opening` 做九宫格开场」。

也可以直接运行脚本。以下示例会自动选段、添加内置音频并写入剪映默认草稿目录。

```powershell
python "$skill/scripts/make_opening.py" --name '旅行九宫格' 'C:/素材/一.mp4' 'C:/素材/二.mp4'
```

- 改过剪映的草稿目录时，用 `--drafts-root` 指定实际位置。
- 自带音源时可传 `--audio 'C:/素材/音频.wav'`。明确需要静音时可用 `--no-audio`。
- 想先预览每段的候选画面时，按 [技能说明](SKILL.md) 分两步执行。

## 画面与音频

| 项目 | 默认行为 |
| --- | --- |
| 单素材 | 同一区间用四层 |
| 多素材 | 每段各用一层，选视觉效果好且能连续播放的一段收尾 |
| 入场 | 原生「九宫格」动画，每层 2 秒，起点错开 0.20 秒 |
| 结束 | 最后一镜全屏继续播放约 1.50 秒 |
| 音轨 | 从内置 20 秒原创音频的 0 秒截到画面末端，0.0 dB，淡入 0.5 秒，淡出 1 秒。各素材原声静音 |

首次打开草稿时，剪映会尝试加载原生动画。如果动画加载失败，可在剪映里选中视频，进入「动画 → 入场」并点一次「九宫格」让剪映加载该资源，再重新打开草稿。

技能会先确认剪映会员状态。有会员时，通过剪映检查并原生导出；无会员时，交付可编辑草稿，说明该预设的导出限制。在终端运行上面的命令会生成草稿，MP4 由剪映的导出面板生成。

## 许可

本仓库的原创代码、文档和合成音频采用 [MIT 许可](LICENSE)。`scripts/synthesize_audio.py` 可以重新生成附带的 WAV。剪映动画资源由本机剪映加载，pyJianYingDraft 按其上游 Apache-2.0 许可提供。
