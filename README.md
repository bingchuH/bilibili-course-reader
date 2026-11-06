# B站课程快速学习

将B站课程整理成**可以直接学习的完整文字教材**，生成以课程名命名的离线HTML。概念讲原理，方法讲步骤，案例讲过程，公式提供演算；只有文字无法替代的体验或材料缺口才回到视频。

技能名：`bilibili-course-reader`。生成端需要支持本地Skill及文件、命令操作的Agent与Python 3.9或以上；Python脚本只使用标准库。通过Git安装和更新时需要Git，也可以用ZIP安装。阅读最终HTML只需要浏览器。**不依赖B站字幕提取插件。**

## 一句话交给Agent安装

复制下面这句话，发给你使用的Agent：

```text
请帮我安装这个技能：https://github.com/bingchuH/bilibili-course-reader 。先读取仓库的 README.md，按照其中“Agent安装流程”安装到当前Agent的用户级技能目录并完成验证；如果已经安装，请安全更新并保留本地修改，最后告诉我如何调用。
```

安装后，在新聊天中这样使用（替换课程链接）：

```text
用 $bilibili-course-reader 帮我学习这套B站课程：[课程链接]。
```

## Agent安装流程

以下步骤供收到安装请求的Agent执行。仓库根目录就是技能目录，`SKILL.md`应直接位于安装目录内，不能再多嵌套一层。

1. **确定位置。** 遵从当前Agent已有的技能目录约定。Codex使用`$CODEX_HOME/skills/bilibili-course-reader`；未设置`CODEX_HOME`时使用`~/.codex/skills/bilibili-course-reader`。其他Agent使用其实际支持的用户级目录，不能把Codex路径当作通用路径。若当前环境不支持本地Skill安装，应说明能力缺项。
2. **检查环境。** 检查Git及Python版本。Python需要3.9或以上，没有额外的pip依赖；不需要为安装本技能配置B站插件、登录账号或下载课程。安装成功与可获取某门课的字幕是两件事。
3. **安装或安全更新。** 目标不存在时克隆本仓库到目标目录。已存在时先检查目录来源和本地修改：同一仓库且工作区干净时，用`git pull --ff-only`更新；有本地修改时先保留并比较，不做强制重置。若是旧ZIP安装或其他来源，先保留原目录备份，再安装到正确位置，不能直接覆盖或混入旧文件。
4. **验证文件和入口。** 确认`SKILL.md`、`README.md`、`agents/openai.yaml`、`assets/reader.html`、`references/`和下列四个脚本存在。读取`SKILL.md`，确认技能名为`bilibili-course-reader`；运行四个`--help`命令，均应成功退出。命令中的`<安装目录>`替换成真实绝对路径：

   ```bash
   python3 "<安装目录>/scripts/fetch_subtitles.py" --help
   python3 "<安装目录>/scripts/import_subtitle.py" --help
   python3 "<安装目录>/scripts/prepare_course.py" --help
   python3 "<安装目录>/scripts/build_reader.py" --help
   ```

5. **报告结果。** 告诉用户实际安装路径、版本（Git提交号）、验证结果与调用方法。Codex首次安装后从新聊天调用；如果当前会话尚未发现技能，重新开启会话或重新加载技能。不要把`--help`通过说成整套课程采集已经验证。

Codex首次安装的手动命令（目标目录不存在时使用）：

```bash
course_reader_dir="${CODEX_HOME:-$HOME/.codex}/skills/bilibili-course-reader"
git clone https://github.com/bingchuH/bilibili-course-reader.git "$course_reader_dir"
```

没有Git时也可以下载仓库ZIP，解压后把包含`SKILL.md`的文件夹放到技能目录并命名为`bilibili-course-reader`，再执行上面的验证。保留完整目录，不要只复制`SKILL.md`。

## 能得到什么

- **`课程名.html`**：完整教学正文、知识目录、表格、自测答案、搜索与必要观看任务，可离线阅读。
- **`课程名_课程正文.md`**：便于复制、笔记和继续整理的文字教材。
- **知识覆盖表**：把原课各时间段的知识对应到正文，便于检查遗漏。
- **原字幕、字幕清单与原片索引**：保留来源、语言、分集和时间信息。
- **`课程名_资料包.zip`**：保持相对路径，解压后可独立使用。

正文按全部已取得字幕编写，去除口头重复，保留实质知识。摘要只作导航；不能用“建议看本集”替代本应写清的解释。字幕缺失、机翻歧义或未复原画面会明确标出。

## 没有字幕插件怎么办

先用随包采集器尝试取得平台可用字幕；遇到登录或访问限制时停止该路线，转到正常浏览器已加载的字幕响应，或导入已有JSON／SRT。Chrome内置开发者工具可以复制字幕请求的Response正文，具体操作见[字幕采集说明](references/subtitle-acquisition.md)。

浏览器备用路线需要Agent具备相应UI能力，或者用户按说明导出字幕。它不要求发送Cookie、密码或包含请求头的HAR，也不承诺所有课程都能匿名自动提取。确实没有字幕时保留缺口；有合法可访问音视频和转写能力时，可另走ASR并标明来源。

## 工作流程与质量要求

核对完整目录 → 取得字幕 → 验收语料 → 逐段列知识清单 → 编写教学正文与自测 → 标注必要画面 → 生成HTML → 对账和阅读验收。

生成器检查编号、时间、文件映射、正文结构和覆盖记录；Agent负责知识解释与语义完整性。只有摘要、缺正文、缺自测或覆盖映射不完整的数据不能生成完整阅读版。结构检查通过不能替代“只读文字是否能学会”的内容审查。

- [技能入口](SKILL.md)
- [学习SOP](references/learning-sop.md)
- [教材编写标准](references/textbook-writing.md)
- [质量门槛](references/quality-gates.md)
- [数据契约](references/course-schema.md)

## 仓库范围

仓库只保留技能说明、运行脚本、页面模板和必要参考文档。课程字幕、生成的HTML／ZIP、调试记录、缓存和凭据不属于技能源码，不应提交。运行时将课程工作文件与输出放在**当前任务工作区**，不要放进技能安装目录。

```text
bilibili-course-reader/
├── README.md
├── SKILL.md
├── agents/openai.yaml
├── assets/reader.html
├── references/
└── scripts/
```
