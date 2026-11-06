# 课程阅读版的数据契约

`course.json` 是教学编写与页面生成的交接文件。当前为schema 2；旧版只有summary的schema 1不满足教材要求，不能直接生成完整阅读版。采集器仍写采集契约的`acquisition.json`（其version 1无需改变），准备器生成空教材骨架。

## 最小结构

```json
{
  "schema_version": 2,
  "course": {
    "title": "课程的完整名称",
    "source_url": "https://www.bilibili.com/video/BV1p741167sz/",
    "description": "一段解释本课程主线的导读",
    "language": "zh-CN",
    "overview": [
      {"title": "一个学习模块", "body": "涉及哪些分集、解决什么问题、与其他模块的关系"}
    ],
    "scope_note": "范围和字幕覆盖说明"
  },
  "episodes": [
    {
      "id": "P01",
      "p": 1,
      "title": "分集原题或经澄清的标题",
      "lecture_label": "第1讲",
      "url": "https://www.bilibili.com/video/BV1p741167sz/?p=1",
      "duration_seconds": 259,
      "status": "available",
      "subtitle_file": "subtitles/P01.zh-CN.srt",
      "subtitle_origin": "platform",
      "summary": "仅作本章概览，不能代替下面的教学正文",
      "lessons": [{
        "id": "P01-S01", "title": "具体知识小节", "start": 0, "end": 259,
        "paragraphs": ["完整解释概念、机制、方法步骤、案例推理和适用条件；这里的示例文字必须替换为真实正文。"],
        "bullets": ["确有顺序或平行关系时列具体步骤"],
        "table": {"headers": ["比较维度", "方案A", "方案B"], "rows": [["示例维度", "实际差异", "实际差异"]]}
      }],
      "self_check": [{"question": "能检验解释或应用能力的问题", "answer": "正文已提供依据的参考答案"}],
      "coverage": [{"start": 0, "end": 259, "topics": ["此区间所有实质知识与案例"], "lesson_ids": ["P01-S01"], "disposition": "taught", "note": "知识如何进入正文；重复或无法复原处分别说明"}],
      "required_visual": [],
      "methods": ["一项可落实的方法"],
      "watch_windows": [
        {
          "start": 20,
          "end": 50,
          "title": "该段实际展示的内容",
          "reason": "文字不能传达什么",
          "watch_for": "观看时观察或动手尝试什么",
          "level": "essential",
          "evidence": [{"start": 20, "end": 28, "text": "来自本集字幕的定位依据，尽量简短"}],
          "visual_checks": [{"at": 31, "observation": "只有真正抽查过画面才填；记录看到了什么"}]
        }
      ]
    }
  ],
  "learning_route": [
    {"episode_id": "P01", "start": 20, "end": 50, "title": "精选片段名称", "focus": "学习任务"}
  ],
  "sources": [{"title": "用于纠正术语的原始课程资料", "url": "https://example.org/course"}],
  "limitations": ["尚未验证、字幕歧义或来源局限，按实际填写"]
}
```

上面的内容仅演示字段，不能原样当作真实课程分析。没有必须回看的片段就用空数组，不编造窗口以满足示例。

## 编号、时间与来源

- `episodes` 数组顺序就是课程顺序；`id` 在课程内唯一。`p` 是URL对应BV里的分P号，不能从中文“第几讲”推断；跨BV合集可有多个 `p=1`，但 `(BV,p)` 不可重复。
- `lecture_label` 可空，例如附加实验录像、导言、补充案例。不要强行编号成讲次。
- `duration_seconds` 是已核实的正整数秒视频时长，所有窗口都满足 `0 <= start < end <= duration_seconds`；窗口和路线时间用秒，不传格式化字符串。时长未知时先补查目录，不猜值以通过构建。
- 窗口的播放链接由生成器根据该集URL和起始秒生成，不接受分析者另写的错分集URL。
- `subtitle_file` 相对于 `course.json` 所在目录，必须处于该目录或其子目录内。生成器不会跟随路径去读取任意其他文件。
- 原SRT复制时保留字节。平台JSON转SRT属于派生产物，原JSON仍应在任务工作目录保留。ASR需用 `subtitle_origin: "asr"` 并在局限中说明模型／语言及校验程度；用户提供为 `provided`。
- `status` 允许 `available`、`missing`、`restricted`、`failed`。采集层的细状态保存在附加的 `acquisition_status`／`acquisition_notes` 中供分析，不把“未登录时没返回轨道”认定为“视频无字幕”。
- 重复字幕指纹默认阻断。经原视频和字幕核对，确认是重复上传等真实重复内容后，在后出现分集上记录 `duplicate_subtitle_review: {"same_as": "首次出现的分集ID", "reason": "具体复核依据"}`。可先写入采集目录，再由准备器传递到 `course.json`。没有相同指纹或指向错误分集的标记不能通过；说明会保留在阅读版和清单中。

## 分析字段的质量要求

- `course.description` 解释主线，`overview` 表达模块关系，不只重复分集标题。
- `summary` 只用于本章概览；完整知识必须写入`lessons`。每小节用`paragraphs`讲解，`bullets`和`table`可选，不接受仅有标题的正文；课程案例、普适原则、补充说明和整理者例题须分清。篇幅随信息量，不设每集固定字数。
- `lessons[].id`须全课唯一，建议使用`P01-S01`；`start/end`对应其课程来源。表格每行列数与表头一致。文字表格均按纯文本转义，不允许塞入任意HTML。
- `coverage`按时间顺序连续覆盖0到视频时长，列出每个区间的实际知识、对应正文ID和说明。`taught`必须关联正文；`visual_only`为现有材料无法文字复原的片段，必须有重叠的`required_visual`；`non_instructional`只用于寒暄、无信息重复或片头片尾等，不得用来丢弃实质内容。字幕不能揭示的无声区间明确标记，不假装已看过画面。时间覆盖不是语义完整的自动证明，必须对照原字幕复审。
- `self_check`至少有一题及参考答案。答案所需知识应由正文提供，优先解释与应用题，短引子不强行扩展。
- `required_visual`每项包含整数`start/end`及`title/reason/watch_for`。只保留确需动态、感知体验或有真实文字证据缺口的任务；可用文字和表格重建的内容应写正文。允许空列表。
- `methods` 用可执行动作，如“给每个方案写同一个任务，让参与者边操作边说出判断”，少用“了解、掌握、提升”。
- `evidence` 记录字幕内真实时间与简短线索；可以概括，但不能捏造台词。缺乏定位证据的候选先放工作笔记，不发布为确定窗口。
- `visual_checks` 只承载真实画面抽查，没有抽查就省略；绝不能为了让页面显得更可信而补写。
- `learning_route`组织`required_visual`中的观看任务；每段需与本集任务重叠。不是逐集观看清单。生成器分别计算逐段累计、去重净时长和重叠时长；不得把片段长度当实际学习耗时。
- `sources` 可列公开原始课件、论文和官方资料，但外部材料不替代当前视频。已知字幕无法确定的数字、术语、图表应保留未核验说明。

## 部分完成

默认完整构建要求所有集字幕与教学正文齐全。`--allow-incomplete`用于明确的阶段稿：缺失集保留目录、链接、状态与原因，生成器仍检查已取得分集的正文、知识映射、自测、证据和时间，不能用它跳过内容编写。
