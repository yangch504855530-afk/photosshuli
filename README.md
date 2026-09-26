# photosshuli

本地照片/视频整理工具:内容识别、精确重复清理(带文件夹保留规则)、相似照片合并择优、按"地点/时间/事物"自动归档,三无文件按年份兜底。全程本地运行,任何"删除"都是先移入可找回的回收站目录。

A local-first photo/video organizer (Chinese UI): content recognition, exact-duplicate cleanup with folder retention rules, similar-photo merging with auto best-pick, and place/time/thing archiving with year fallback. Nothing is permanently deleted — files are moved to a recoverable staging folder.

## 功能

| 功能 | 说明 |
|---|---|
| 内容识别 | 截图 / 聊天图片 / 疑似缓存缩略图 / 相机照片 / 无元数据图片 / 实况照片(livp)/ 行车记录仪 / 视频 / 文档 / 无法读取 |
| 精确重复 | 大小分桶 + MD5 精确比对;组内按"文件夹优先级 → 质量分 → 修改时间"自动选保留者,可手动改,确认后其余进回收站 |
| 相似合并 | dHash 感知哈希分组(阈值可调);组内按技术质量评分(清晰度 50% + 曝光 25% + 亮度居中 15% + 分辨率 10%)自动预选最佳,同排展示人工确认 |
| 分类归档 | GPS→地点簇(可命名,归入 `旅行\地点\月 地点\`);拍摄时间→年份;行车记录仪→专项目录;**三无文件(无地点无时间)按文件年份兜底归档** |
| 回收站执行 | 所有删除 = 移入扫描根目录下 `_PhotosShuli回收站\`(同盘秒移、保留原相对路径、可随时找回);执行前强制预览,全量 CSV 日志 |

## 安装

要求:Python 3.10+(推荐 3.12+)

```bash
pip install pillow pillow-heif
# 可选:安装 ffmpeg/ffprobe 后可读取视频拍摄时间/GPS、生成视频缩略图
#   Windows: 下载 ffmpeg essentials build 解压到 C:\ffmpeg(工具会自动探测)
#   macOS:   brew install ffmpeg
```

## 使用

```bash
# Web 界面(推荐)
python -m photosshuli server --port 8630
# 打开 http://127.0.0.1:8630 → 总览与设置里填入扫描目录 → 开始扫描

# 或命令行
python -m photosshuli scan "D:\照片库"          # 建立索引
python -m photosshuli plan                      # 预览将执行的移动
python -m photosshuli apply --execute           # 执行(不加 --execute 只预览)
```

数据(索引/设置/决策/日志)保存在工作目录 `.photosshuli\` 下,可用 `--home` 或环境变量 `PHOTOSHULI_HOME` 改位置。

### 网页界面六个页签

1. **总览与设置**:扫描目录、深度视频元数据开关、相似阈值、重复保留优先级(靠前的目录优先保留)
2. **内容识别**:类别统计 + 按类别浏览,一键整类标记
3. **精确重复**:组内同排展示、绿框保留者、一键改选、确认后其余进回收站决策
4. **相似合并**:自动预选"最符合技术审美"的一张(推荐徽标),其余同排展示,确认后进回收站决策
5. **分类归档**:建议汇总、地点簇命名、一键采纳全部建议、逐张微调
6. **执行**:全量预览 → 确认执行 → 结果与日志

## 安全模型

- 扫描/缩略图/识别**只读**原文件;缩略图缓存在数据目录
- "删除"永远 = 移入 `_PhotosShuli回收站\<原相对路径>`,不做真删除;确认无误后自行清空该目录
- 所有移动写入 `applied_log.csv`(时间/源/目标/结果),可按日志反向恢复
- 执行脚本默认 dry-run,`--execute` 才动手

## 已知限制(v0.1.1)

- "最符合人工审美"采用**技术质量评分**近似(清晰度/曝光/分辨率),非 AI 审美模型;最终以人工确认为准
- 视频相似比较基于抽帧 dHash;无 ffmpeg 时跳过视频元数据
- 大库(>10 万张)相似分组为 O(n²),后续版本将加签名桶索引
- 路线图:AI 审美评分、EXIF 写回、按事件(时间+地点联合)细分、人脸辅助去重

## 开发

```bash
python -m unittest discover -s tests   # 单元+集成测试(合成数据,无需真实照片)
```

## License

MIT
