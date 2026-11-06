# 无插件字幕获取与导入

在需要取得字幕、处理缺集或导入用户已有字幕时读取。脚本只抓取字幕和目录，不下载音视频、不读取浏览器Cookie或密码、不安装扩展。

## 路线选择

1. **先试公开字幕接口**：使用本技能内的Python 3.9+标准库脚本。无需安装字幕插件或第三方Python包。先只列目录，再小批检查字幕；成功后续跑。
2. **公开接口要求登录或受限**：停止接口重试，改用正常浏览器里的播放器。若环境提供已观察页面资源的导出工具，先读取其文档，确认它支持导出字幕JSON；能列出URL不等于能导出响应正文。不要编造工具调用，不从隐藏页面状态或Cookie库提取登录凭据。
3. **浏览器工具不能导出字幕资源**：由具备原生UI控制能力的代理或用户，用Chrome自带的开发者工具复制当前集字幕JSON响应，再用`import_subtitle.py`导入；不需要安装字幕扩展。先验证一集的映射和内容，再继续其余分集。若用户已有SRT，也可直接导入。
4. **已有可用字幕插件**：可以继续使用用户指定的插件，但不要求首次使用者安装。每集切换后完整重新加载，核对当前分集、下载文件的首尾内容和时长，防止插件取到上集历史资源。
5. **确实无可用字幕**：先把缺集状态保留在目录和最终阅读版，不能用简介、推荐内容或课程名补造摘要。只有用户授权并提供可访问的音视频，且环境具备转写工具时，才启动转写；明确标记`asr`，不冒充平台字幕。

无插件不等于无需登录、无需字幕轨。API受限与“没有字幕”是两回事。

## 公共接口命令

在**本次任务的工作目录**执行；将`<skill>`替换为本技能的绝对路径。不要把课程下载、工作文件或输出写进技能安装目录。先预检目录：

```bash
python3 "<skill>/scripts/fetch_subtitles.py" fetch 'https://www.bilibili.com/video/BV1p741167sz/' --out work/acquisition --list-only
```

先测试第一集，再采集全部分集。默认优先中文，不会悄悄回退为英文：

```bash
python3 "<skill>/scripts/fetch_subtitles.py" fetch BV1p741167sz --out work/acquisition --limit 1
python3 "<skill>/scripts/fetch_subtitles.py" fetch BV1p741167sz --out work/acquisition
```

脚本默认间隔1秒，网络错误/服务器5xx至多重试1次。HTTP 401、403、412、429或明确登录/访问限制不重试；当前批次中其余分集记为`not_attempted`，不推定无字幕。必要时根据报错切换浏览器路线，不加入代理、挑战绕过、Cookie抓取或签名绕过。

目录以BV的`pages`为范围，每集用`cid`定位，`p`用于可读编号与链接。**课程讲次不从P号推断**，`lecture`初始为null；额外插入案例可能导致讲次错位。合集中的多个独立BV需先从可见目录明确收集，再分别采集并合并，脚本不会把推荐视频当作课程。

续跑时必须同时匹配`source_id`、时长、所选语言，以及原始字幕和SRT的SHA256；通过后才跳过。若文件被改动，重新检查该集。仍需人工核对字幕主题/首尾与该分集一致，校验值不能证明语义归属。

### 输出契约：acquisition.json

```json
{
  "schema_version": 1,
  "course": {"title": "课程名", "source_url": "https://www.bilibili.com/video/BV.../", "bvid": "BV..."},
  "episodes": [{
    "id": "P01", "source_id": "BV...:123", "p": 1, "cid": 123,
    "lecture": null, "title": "分集原题", "duration_seconds": 300,
    "url": "https://www.bilibili.com/video/BV.../?p=1",
    "subtitle": {
      "status": "downloaded", "language": "zh-CN", "kind": "platform_unspecified",
      "origin": "platform", "raw_path": "raw/P01_title.zh-CN.json",
      "srt_path": "subtitles/P01_title.zh-CN.srt", "sha256": "...", "raw_sha256": "...",
      "cue_count": 60, "first_seconds": 0.5, "last_seconds": 295,
      "source_url": "https://aisubtitle.hdslb.com/bfs/subtitle/filename.json",
      "notes": []
    }
  }],
  "acquisition": {"catalog_complete": true, "status": "complete", "counts": {"downloaded": 1}}
}
```

路径相对`acquisition.json`所在目录。JSON原始响应字节保存在`raw/`；SRT仅作格式转换，不改写字幕。字幕CDN签名URL只用于当前请求，输出时去掉查询参数；不把短期签名参数放入可分享资料。原始播放器元数据不写入交付包。

状态含义：

|状态|含义与处理|
|---|---|
|`downloaded`|文件已取得且结构/时间校验通过；仍需内容映射复核|
|`listed`|只列目录，未检查字幕|
|`not_attempted`|达到limit或前面受限后未尝试；不是无字幕|
|`login_required`|服务明确要求登录，转正常浏览器|
|`access_restricted`|HTTP/接口访问受限，停止此路线|
|`no_tracks_reported`|公开接口没有返回轨道；不能断言播放器里没有|
|`language_unavailable`|没有所选语言，报告已有语言后遵从用户选择|
|`network_error` / `api_error`|检索失败，保留失败状态|
|`invalid_subtitle` / `invalid_response`|格式、时间或分集映射异常；不能列为成功|

首次获取目录失败、又没有旧清单时，`episodes=[]`、`catalog_complete=false`，不能把0集误报为“全课程完成”。如果输出目录已有清单，当前目录请求失败**不覆盖旧文件**；此次失败写入独立的`last_attempt.json`，说明是否存在通过同BV校验的旧目录、保留多少集、多少字幕仍通过文件校验。旧清单可用不代表此次请求成功，失败仍返回退出码2。退出码0表示目录预检或全部采集成功，2表示部分缺失/受限/错误。后续成功运行会更新`last_attempt.json`。

## Chrome内置工具导出（无需字幕插件）

这是给用户或具备原生浏览器UI控制能力的代理的备用操作。遵从当前环境的浏览器工具约束；不要用执行任意JavaScript替代受限的浏览器操作。

1. 在正常Chrome中打开目标分集，确认页面显示的P号，播放几秒并开启需要的字幕语言。若有登录或验证码，让用户按正常流程处理。
2. 打开Chrome开发者工具的 **Network／网络** 面板，选 **Fetch/XHR**，清空本次请求列表，重新加载**当前分集**并重新开启字幕。按当前已观察到的字幕资源名或域名过滤，例如`aisubtitle.hdslb.com`，找到字幕CDN返回的JSON资源。
3. 逐一查看候选的 **Response／响应**：真正字幕含非空`body`数组，每条有`from`、`to`、`content`。播放器元数据、弹幕、AI摘要不是字幕。对照当前视频的首句和时长，确认不是预加载的其他集或另一种语言。
4. 使用网络面板“Copy response／复制响应”，把该JSON保存为本地文件（例如`P01.zh-CN.json`）。**不要复制请求头、Cookie、Copy as cURL或包含所有请求的HAR**；它们可能夹带登录凭据和无关浏览数据。
5. 从播放器**实际可见**的分集列表整理`acquisition.json`：记录课程名、`id`、`p`、分集原题、确切URL、页面标注时长。未知`cid`可以省略，不猜测。核对全部目录后才能标`acquisition.catalog_complete=true`；如果只取得部分目录，明确为false，并在范围说明里写出已核对的范围。脚本导入成功不会自动证明课程目录完整。
6. 导入时明确关联P号，并写出当前页面分集与字幕首尾／主题的实际核对依据：

```bash
python3 "<skill>/scripts/import_subtitle.py" work/P01.zh-CN.json --catalog work/acquisition/acquisition.json --episode P01 --language zh-CN --origin platform --mapping-note '<填写当前分集与字幕首尾、主题的实际核对依据>'
```

`import_subtitle.py`只接受已整理的`acquisition.json`目录，输出相对该目录所在位置；它允许未知CID，但要求明确`p/title/url/duration_seconds`和核对说明。将示例P01、文件路径、语言和说明替换成真实值，不保留占位文字。用户已有SRT时，将首个参数换为`.srt`，根据真实来源选择`--origin platform|provided|asr`，原字节保留。

如果已有包含CID的完整API目录，也可以使用`fetch_subtitles.py import`。该入口要求每集CID，用于API采集器的同一套目录结构；不要为满足该入口而编造CID：

```bash
python3 "<skill>/scripts/fetch_subtitles.py" import work/P01.zh-CN.json --catalog work/acquisition/acquisition.json --episode P01 --language zh-CN --out work/acquisition
```

只有view接口原响应`catalog.json`时，该入口首次可把`--catalog`指向它，之后应指向输出的`acquisition.json`以保留已导入记录。浏览器可见目录与未知CID的常规备用路线优先使用独立的`import_subtitle.py`。

导入不会仅凭长文件名猜分集。错误关联能生成结构合法的SRT，所以需要人工对照至少首尾/主题两个内容点。原始下载文件不删除。

## 能力边界与维护

公开接口和字幕资源受账号、字幕轨、网络及平台策略影响。单集成功不代表全课成功；结构校验不能代替首尾和主题核对。不要删除签名查询参数后假定资源仍可下载；已导出的响应正文可在本地导入。

脚本使用B站站点内部接口`x/player/wbi/v2`，并检查`need_login_subtitle`。接口与字幕JSON结构可能变化，可参考 [yt-dlp的B站提取器源码](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)及实际响应维护，不能把它当稳定公共API承诺。每个新环境都应先验证一集，准确报告采集、导入和内容映射分别成功到哪一步。
