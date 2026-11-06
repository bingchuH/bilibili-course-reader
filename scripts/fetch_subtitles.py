#!/usr/bin/env python3
"""Read publicly available Bilibili subtitle tracks, or import observed JSON/SRT.

Python 3 standard library only. No browser credential access, video download,
captcha solving, or attempt to defeat a login/risk-control response.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bilibili.com"
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
UA = "Mozilla/5.0 (compatible; BilibiliCourseReader/1.0)"


class AcquisitionError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def safe_name(value, limit=72):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value)).strip(" .")
    text = re.sub(r"\s+", " ", text) or "untitled"
    return text[:limit].rstrip(" .")


def source_url_without_query(value):
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact_message(value):
    return re.sub(r"https?://[^\s]+", lambda m: source_url_without_query(m[0]), str(value))[:400]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bvid_from(value):
    match = BV_RE.search(value)
    if not match:
        raise AcquisitionError("invalid_source", "需要完整 BV 号或包含 BV 号的视频链接；短链接请先在正常浏览器中展开。")
    return match[0]


def api_data(payload):
    code = payload.get("code")
    if code == 0 and isinstance(payload.get("data"), dict):
        return payload["data"]
    message = redact_message(payload.get("message", "API response unavailable"))
    if code in (-101, -102):
        raise AcquisitionError("login_required", f"API {code}: {message}")
    if code in (-352, -412, -403, -404, 62002, 62012):
        raise AcquisitionError("access_restricted", f"API {code}: {message}")
    raise AcquisitionError("api_error", f"API {code}: {message}")


def subtitle_url(value):
    if value.startswith("//"):
        value = "https:" + value
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or not (
        host == "bilibili.com" or host.endswith(".bilibili.com") or
        host == "hdslb.com" or host.endswith(".hdslb.com")
    ):
        raise AcquisitionError("invalid_subtitle", "字幕链接不是预期的 HTTPS B站资源域名；没有访问该链接。")
    return value


class StrictRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        subtitle_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, bvid, interval=1.0, retries=1, fixture_dir=None):
        self.bvid = bvid
        self.interval = max(0.3, interval)
        self.retries = min(max(retries, 0), 2)
        self.last_request = 0.0
        self.fixture_dir = fixture_dir
        self.opener = urllib.request.build_opener(StrictRedirect())

    def get(self, url, fixture_name):
        if self.fixture_dir:
            try:
                raw = (self.fixture_dir / fixture_name).read_bytes()
            except OSError as exc:
                raise AcquisitionError("network_error", f"测试响应文件缺失：{fixture_name}") from exc
            return self.decode(raw), raw
        subtitle_url(url)
        for attempt in range(self.retries + 1):
            delay = self.interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            request = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json,text/plain,*/*",
                "Referer": f"https://www.bilibili.com/video/{self.bvid}/",
            })
            self.last_request = time.monotonic()
            try:
                with self.opener.open(request, timeout=20) as response:
                    raw = response.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise AcquisitionError("invalid_response", "响应超过20MB，已停止读取。")
                return self.decode(raw), raw
            except urllib.error.HTTPError as exc:
                if exc.code in (401,):
                    raise AcquisitionError("login_required", f"HTTP {exc.code}，需要正常登录。") from exc
                if exc.code in (403, 412, 429):
                    raise AcquisitionError("access_restricted", f"HTTP {exc.code}，停止此接口路线；请改用正常浏览器或已导出的字幕。") from exc
                if exc.code >= 500 and attempt < self.retries:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise AcquisitionError("network_error", f"HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise AcquisitionError("network_error", redact_message(exc)) from exc
        raise AcquisitionError("network_error", "请求未完成。")

    @staticmethod
    def decode(raw):
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise AcquisitionError("invalid_response", "响应不是有效JSON；没有将网页或错误页当作字幕。") from exc
        if not isinstance(data, dict):
            raise AcquisitionError("invalid_response", "需要JSON对象。")
        return data


def empty_subtitle(status="not_attempted", message=""):
    return {"status": status, "language": None, "kind": None, "raw_path": None,
            "srt_path": None, "sha256": None, "raw_sha256": None, "cue_count": 0,
            "first_seconds": None, "last_seconds": None, "source_url": None,
            "notes": [message] if message else []}


def catalog_from_api(payload, bvid):
    data = api_data(payload)
    if data.get("bvid") != bvid:
        raise AcquisitionError("invalid_source", "元数据返回的 BV 号与请求不一致。")
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        raise AcquisitionError("invalid_source", "未获得非空分集列表；不从推荐视频猜测课程范围。")
    episodes = []
    seen_pages, seen_cids = set(), set()
    for page in pages:
        try:
            p, cid = int(page["page"]), int(page["cid"])
            raw_duration = page["duration"]
            title = str(page["part"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionError("invalid_source", "分集元数据缺少 page/cid/duration/part。") from exc
        if (isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float))
                or not math.isfinite(raw_duration) or raw_duration <= 0
                or raw_duration != int(raw_duration)):
            raise AcquisitionError("invalid_source", "分集duration必须为正整数秒；不对非整数时长取整或猜测。")
        # Keep the schema's integer contract, including when reopening an older
        # manifest whose exact whole-second value was serialized as 24.0.
        duration = int(raw_duration)
        if p <= 0 or cid <= 0 or p in seen_pages or cid in seen_cids:
            raise AcquisitionError("invalid_source", "分集编号、CID或时长无效/重复，需人工核对目录。")
        seen_pages.add(p); seen_cids.add(cid)
        episodes.append({"id": f"P{p:02d}", "source_id": f"{bvid}:{cid}", "p": p,
                         "lecture": None, "title": title, "duration_seconds": duration,
                         "url": f"https://www.bilibili.com/video/{bvid}/?p={p}",
                         "cid": cid, "subtitle": empty_subtitle()})
    episodes.sort(key=lambda e: e["p"])
    if [e["p"] for e in episodes] != list(range(1, len(episodes) + 1)):
        raise AcquisitionError("invalid_source", "分集编号不连续，目录完整性需要人工核对。")
    return {"schema_version": 1, "course": {"title": str(data.get("title") or bvid),
            "source_url": f"https://www.bilibili.com/video/{bvid}/", "bvid": bvid},
            "episodes": episodes, "acquisition": {"mode": "public_api", "created_at": now(),
            "catalog_source": "x/web-interface/view", "status": "catalogued"}}


def load_catalog(path, bvid=None):
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if "code" in payload:
        return catalog_from_api(payload, bvid or payload.get("data", {}).get("bvid", ""))
    if not isinstance(payload.get("course"), dict) or not isinstance(payload.get("episodes"), list):
        raise AcquisitionError("invalid_source", "本地目录需为 acquisition.json 格式或原 view API JSON。")
    actual_bvid = payload["course"].get("bvid")
    if not actual_bvid or (bvid and actual_bvid != bvid):
        raise AcquisitionError("invalid_source", "本地目录 BV 号不匹配。")
    # Reuse the same page validation; do not trust a pasted catalog's arbitrary URLs.
    return catalog_from_api({"code": 0, "data": {"bvid": actual_bvid,
        "title": payload["course"].get("title"), "pages": [
            {"page": e["p"], "cid": e["cid"], "duration": e["duration_seconds"], "part": e["title"]}
            for e in payload["episodes"]]}}, actual_bvid)


def choose_track(tracks, languages):
    usable = [t for t in tracks if isinstance(t, dict) and t.get("lan") and t.get("subtitle_url")]
    for wanted in languages:
        matches = [t for t in usable if t["lan"] == wanted or (wanted in ("zh", "en") and t["lan"].startswith(wanted + "-"))]
        if matches:
            # Within a language, prefer a non-AI track if the API explicitly identifies it.
            return sorted(matches, key=lambda t: bool(t.get("ai_type") or str(t["lan"]).startswith("ai-")))[0]
    return None


def parse_body(data):
    body = data.get("body")
    if not isinstance(body, list) or not body:
        raise AcquisitionError("invalid_subtitle", "字幕JSON没有非空body数组。")
    cues = []
    for index, cue in enumerate(body, 1):
        try:
            start, end, text = float(cue["from"]), float(cue["to"]), cue["content"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionError("invalid_subtitle", f"第{index}条字幕缺少from/to/content。") from exc
        if not isinstance(text, str) or not text.strip() or not all(map(math.isfinite, (start, end))) or start < 0 or end < start:
            raise AcquisitionError("invalid_subtitle", f"第{index}条字幕文字或时间无效。")
        if cues and start < cues[-1][0]:
            raise AcquisitionError("invalid_subtitle", f"第{index}条字幕时间逆序。")
        cues.append((start, end, text))
    return cues


def stamp(seconds):
    ms = int(math.floor(seconds * 1000 + 0.5))
    return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"


def srt_bytes(cues):
    return "".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}\n\n" for i, (a, b, text) in enumerate(cues, 1)).encode("utf-8")


def parse_srt(raw):
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").strip()
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if not lines:
            continue
        match = re.fullmatch(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?", lines[0])
        if not match or len(lines) < 2:
            raise AcquisitionError("invalid_subtitle", "SRT缺少有效时间轴或文字。")
        def seconds(v):
            hh, mm, ss = v.replace(",", ".").split(":")
            return 3600 * int(hh) + 60 * int(mm) + float(ss)
        cues.append({"from": seconds(match[1]), "to": seconds(match[2]), "content": "\n".join(lines[1:])})
    return parse_body({"body": cues})


def save_subtitle(out, episode, raw, cues, language, kind, resource_url=None, is_srt=False, imported=False):
    if cues[-1][1] > episode["duration_seconds"] + 15:
        raise AcquisitionError("invalid_subtitle", "字幕结束超过本集时长15秒，可能串集；未接纳为成功。")
    stem = f'{episode["id"]}_{safe_name(episode["title"])}.{safe_name(language, 24)}'
    raw_rel = Path("raw") / (stem + (".original.srt" if is_srt else ".json"))
    srt_rel = Path("subtitles") / (stem + ".srt")
    for folder in (out/"raw", out/"subtitles"):
        folder.mkdir(parents=True, exist_ok=True)
    rendered = raw if is_srt else srt_bytes(cues)
    (out/raw_rel).write_bytes(raw)
    (out/srt_rel).write_bytes(rendered)
    notes = ["原始JSON完整保留；SRT仅转换格式，未改写字幕文本。"] if not is_srt else ["SRT原字节保留，未校改文字。"]
    tail = round(episode["duration_seconds"] - cues[-1][1], 3)
    if tail > 20:
        notes.append(f"末条字幕与页面时长相差{tail}秒，需核对片尾/无台词画面或缺漏。")
    if imported:
        notes.append("分集归属来自明确的导入参数；仍需以首尾内容/播放器当前分集人工核对，不仅凭文件名。")
    episode["subtitle"] = {"status": "downloaded", "language": language, "kind": kind,
        "raw_path": str(raw_rel), "srt_path": str(srt_rel), "raw_sha256": sha(raw),
        "sha256": sha(rendered), "cue_count": len(cues), "first_seconds": cues[0][0],
        "last_seconds": cues[-1][1], "source_url": source_url_without_query(resource_url) if resource_url else None,
        "notes": notes, "acquired_at": now(), "origin": "provided" if imported else "platform"}


def valid_cached(old, fresh, out, languages):
    try:
        sub = old["subtitle"]
        if old["source_id"] != fresh["source_id"] or old["duration_seconds"] != fresh["duration_seconds"] or sub["status"] != "downloaded":
            return False
        if not any(sub["language"] == x or (x in ("zh", "en") and sub["language"].startswith(x+'-')) for x in languages):
            return False
        files = [(sub["srt_path"], sub["sha256"]), (sub["raw_path"], sub["raw_sha256"])]
        for name, digest in files:
            path = (out/name).resolve()
            if not path.is_relative_to(out.resolve()) or sha(path.read_bytes()) != digest:
                return False
        parse_srt((out/sub["srt_path"]).read_bytes())
        return True
    except (KeyError, TypeError, OSError, ValueError, AcquisitionError):
        return False


def write_manifest(out, catalog):
    out.mkdir(parents=True, exist_ok=True)
    catalog["acquisition"]["updated_at"] = now()
    states = {}
    for e in catalog["episodes"]:
        state = e["subtitle"]["status"]
        states[state] = states.get(state, 0) + 1
    catalog["acquisition"]["counts"] = states
    target = out/"acquisition.json"
    temporary = out/".acquisition.tmp"
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(target)


def write_attempt(out, attempt):
    """Keep the latest request outcome separate from the reusable course manifest."""
    out.mkdir(parents=True, exist_ok=True)
    attempt.update(schema_version=1, attempted_at=now())
    temporary = out/".last_attempt.tmp"
    temporary.write_text(json.dumps(attempt, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(out/"last_attempt.json")


def run_fetch(args):
    bvid = bvid_from(args.source)
    languages = [x.strip() for x in args.languages.split(",") if x.strip()]
    if not languages:
        raise AcquisitionError("invalid_source", "至少指定一个语言。")
    client = Client(bvid, args.interval, args.retries, args.fixture_dir)
    prior_path = args.out/"acquisition.json"
    try:
        if args.catalog_json:
            catalog = load_catalog(args.catalog_json, bvid)
            catalog["acquisition"]["catalog_source"] = "provided_catalog"
        else:
            payload, _ = client.get(f"{API}/x/web-interface/view?"+urllib.parse.urlencode({"bvid": bvid}), "view.json")
            catalog = catalog_from_api(payload, bvid)
    except AcquisitionError as exc:
        attempt = {"operation": "catalog_fetch", "requested_bvid": bvid,
                   "status": exc.status, "message": str(exc), "catalog_complete": False,
                   "existing_manifest_preserved": prior_path.exists(),
                   "same_bv_catalog_available": False, "retained_episode_count": 0,
                   "verified_cached_subtitles": 0}
        if prior_path.exists():
            # A transient failure must never replace earlier mappings with episodes=[].
            # Even a malformed/mismatched old file is left untouched for recovery;
            # only a validated same-BV catalog is reported as reusable.
            try:
                retained = load_catalog(prior_path, bvid)
                old = json.loads(prior_path.read_text(encoding="utf-8-sig"))
                old_by_id = {e["id"]: e for e in old.get("episodes", [])}
                attempt["same_bv_catalog_available"] = True
                attempt["retained_episode_count"] = len(retained["episodes"])
                attempt["verified_cached_subtitles"] = sum(
                    e["id"] in old_by_id and valid_cached(old_by_id[e["id"]], e, args.out, languages)
                    for e in retained["episodes"])
            except (AcquisitionError, OSError, ValueError, KeyError, TypeError) as old_error:
                attempt["existing_manifest_note"] = "旧清单未通过同BV目录校验，仍保留原文件：" + redact_message(old_error)
        else:
            catalog = {"schema_version": 1, "course": {"title": bvid, "bvid": bvid,
                "source_url": f"https://www.bilibili.com/video/{bvid}/"}, "episodes": [],
                "acquisition": {"mode": "public_api", "status": exc.status, "message": str(exc),
                "catalog_complete": False}}
            write_manifest(args.out, catalog)
        write_attempt(args.out, attempt)
        print(json.dumps(attempt, ensure_ascii=False))
        return 2
    prior = {}
    if not args.no_resume and prior_path.exists():
        try:
            previous = json.loads(prior_path.read_text())
            if previous["course"]["bvid"] == bvid:
                prior = {e["id"]: e for e in previous["episodes"]}
        except (ValueError, KeyError, TypeError):
            pass
    catalog["acquisition"].update(catalog_complete=True, requested_languages=languages, mode="fixture" if args.fixture_dir else "public_api")
    stopped = None
    count = 0
    for e in catalog["episodes"]:
        if e["id"] in prior and valid_cached(prior[e["id"]], e, args.out, languages):
            e["subtitle"] = prior[e["id"]]["subtitle"]
            e["subtitle"]["resume_verified_at"] = now()
        elif args.list_only:
            e["subtitle"] = empty_subtitle("listed", "仅列目录，尚未检查字幕。")
        elif args.limit and count >= args.limit:
            e["subtitle"] = empty_subtitle("not_attempted", "达到本次 --limit 上限。")
        elif stopped:
            e["subtitle"] = empty_subtitle("not_attempted", f"之前遇到{stopped}，未继续请求；不推断本集无字幕。")
        else:
            count += 1
            try:
                payload, _ = client.get(f"{API}/x/player/wbi/v2?"+urllib.parse.urlencode({"bvid": bvid, "cid": e["cid"]}), f'player_{e["cid"]}.json')
                player = api_data(payload)
                if player.get("cid") is not None and int(player["cid"]) != e["cid"]:
                    raise AcquisitionError("invalid_subtitle", "播放器元数据 CID 与当前分集不一致。")
                tracks = (player.get("subtitle") or {}).get("subtitles") or []
                if player.get("need_login_subtitle") and not tracks:
                    raise AcquisitionError("login_required", "接口明确要求登录后才可获得字幕。")
                if not tracks:
                    raise AcquisitionError("no_tracks_reported", "公开接口未报告可用字幕；这不能证明播放器中没有字幕。")
                track = choose_track(tracks, languages)
                if not track:
                    available = [str(t.get("lan")) for t in tracks if isinstance(t, dict)]
                    raise AcquisitionError("language_unavailable", "未找到所选语言；接口报告语言："+", ".join(available))
                resource = subtitle_url(track["subtitle_url"])
                track_key = safe_name(track.get("id_str") or track.get("id") or track["lan"], 100)
                payload, raw = client.get(resource, f"subtitle_{track_key}.json")
                cues = parse_body(payload)
                kind = "ai" if track.get("ai_type") or track["lan"].startswith("ai-") else "platform_unspecified"
                save_subtitle(args.out, e, raw, cues, track["lan"], kind, resource)
            except AcquisitionError as exc:
                e["subtitle"] = empty_subtitle(exc.status, str(exc))
                if exc.status in ("login_required", "access_restricted"):
                    stopped = exc.status
        write_manifest(args.out, catalog)
        print(json.dumps({"episode": e["id"], "status": e["subtitle"]["status"], "cue_count": e["subtitle"]["cue_count"]}, ensure_ascii=False))
    catalog["acquisition"]["status"] = "listed" if args.list_only else ("complete" if all(e["subtitle"]["status"] == "downloaded" for e in catalog["episodes"]) else "partial")
    write_manifest(args.out, catalog)
    write_attempt(args.out, {"operation": "catalog_only" if args.list_only else "subtitle_fetch",
                            "requested_bvid": bvid, "status": catalog["acquisition"]["status"],
                            "catalog_complete": True, "counts": catalog["acquisition"]["counts"]})
    return 0 if catalog["acquisition"]["status"] in ("listed", "complete") else 2


def run_import(args):
    catalog = load_catalog(args.catalog)
    old = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    if "episodes" in old:
        old_by_id = {e["id"]: e for e in old["episodes"]}
        for e in catalog["episodes"]:
            if old_by_id.get(e["id"], {}).get("subtitle", {}).get("status") == "downloaded":
                # Paths are anchored to the catalog directory. Never pretend old files moved.
                if args.catalog.parent.resolve() != args.out.resolve():
                    continue
                e["subtitle"] = old_by_id[e["id"]]["subtitle"]
    selected = next((e for e in catalog["episodes"] if e["id"] == args.episode), None)
    if selected is None:
        raise AcquisitionError("invalid_source", "--episode必须精确匹配目录中的P号。")
    raw = args.file.read_bytes()
    is_srt = args.file.suffix.lower() == ".srt"
    cues = parse_srt(raw) if is_srt else parse_body(Client.decode(raw))
    save_subtitle(args.out, selected, raw, cues, args.language, "provided", args.source_url, is_srt, imported=True)
    catalog["acquisition"].update(mode="import", status="complete" if all(e["subtitle"]["status"] == "downloaded" for e in catalog["episodes"]) else "partial", catalog_complete=True)
    write_manifest(args.out, catalog)
    print(json.dumps({"episode": selected["id"], "status": "downloaded", "cue_count": len(cues)}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="目录与公开字幕；不使用登录凭据")
    fetch.add_argument("source", help="BV号或完整视频链接")
    fetch.add_argument("--out", type=Path, required=True)
    fetch.add_argument("--list-only", action="store_true", help="只核对目录，不请求各集字幕")
    fetch.add_argument("--catalog-json", type=Path, help="从正常浏览器获得的完整目录JSON或旧acquisition.json")
    fetch.add_argument("--languages", default="zh-CN,zh-Hans,zh,ai-zh", help="按优先级选择一条轨；不自动改用英语")
    fetch.add_argument("--limit", type=int, default=0, help="本次最多检查几集；0为全部")
    fetch.add_argument("--interval", type=float, default=1.0)
    fetch.add_argument("--retries", type=int, default=1, help="仅对网络/5xx重试，最多2次")
    fetch.add_argument("--no-resume", action="store_true")
    fetch.add_argument("--fixture-dir", type=Path, help="离线测试用，读取预存API响应，不访问网络")
    ingest = commands.add_parser("import", help="导入正常浏览器已导出的字幕JSON或SRT")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--catalog", type=Path, required=True)
    ingest.add_argument("--episode", required=True, help="目录中确切的P01/P02等")
    ingest.add_argument("--language", required=True)
    ingest.add_argument("--out", type=Path, required=True)
    ingest.add_argument("--source-url", help="可选字幕来源URL；写入时自动去掉查询参数")
    args = parser.parse_args()
    if args.command == "fetch" and args.limit < 0:
        parser.error("--limit不能为负数")
    try:
        return run_fetch(args) if args.command == "fetch" else run_import(args)
    except (AcquisitionError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": getattr(exc, "status", "invalid_input"), "message": redact_message(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
