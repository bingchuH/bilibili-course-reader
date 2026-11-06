#!/usr/bin/env python3
"""Validate reviewed course.json and build a self-contained, course-named reader.

Python 3.9+ standard library only; no network, package installation, or credentials.
The script validates structure and evidence locations, not summary factual accuracy.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit


class ValidationError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise ValidationError(message)


def text(value, label):
    need(isinstance(value, str) and bool(value.strip()), f'{label} 必须是非空文本')
    return value.strip()


def strings(value, label):
    need(isinstance(value, list), f'{label} 必须是列表')
    return [text(v, f'{label}[{i}]') for i, v in enumerate(value)]


def integer(value, label, minimum=0):
    need(type(value) is int and value >= minimum, f'{label} 必须是 >= {minimum} 的整数秒/编号')
    return value


def web_url(value, label):
    value = text(value, label)
    p = urlsplit(value)
    need(p.scheme in ('https', 'http') and p.hostname and not p.username and not p.password,
         f'{label} 必须是无凭据的 HTTP(S) 链接')
    return value


def video_url(value, part, label):
    value = web_url(value, label)
    parsed = urlsplit(value)
    need(parsed.hostname in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com'),
         f'{label} 必须是B站已解析的完整分集链接，不接受短链')
    match = re.fullmatch(r'/video/(BV[0-9A-Za-z]+)(?:/)?', parsed.path)
    need(bool(match), f'{label} 必须指向 /video/BV.../')
    query = parse_qs(parsed.query)
    try:
        actual = int(query.get('p', ['1'])[0])
    except (ValueError, TypeError):
        raise ValidationError(f'{label} 的 p 参数无效')
    need(len(query.get('p', ['1'])) == 1 and actual == part,
         f'{label} 的 p={actual} 与声明分P号 {part} 不符')
    # Keep only identity. Tracking IDs and signed query tokens do not enter artifacts.
    canonical = f'https://www.bilibili.com/video/{match.group(1)}/?p={part}'
    return canonical, (match.group(1), part)


def at_url(base, seconds):
    parsed = urlsplit(base)
    query = parse_qs(parsed.query)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                      urlencode({'p': query['p'][0], 't': seconds}), ''))


def safe_name(title):
    # Preserve the complete human title; replace only portable-filename hazards.
    mapping = str.maketrans({'/':'／', '\\':'＼', ':':'：', '*':'＊', '?':'？',
                             '"':'＂', '<':'＜', '>':'＞', '|':'｜'})
    result = title.translate(mapping)
    result = ''.join('_' if ord(c) < 32 or ord(c) == 127 else c for c in result)
    result = re.sub(r'[. ]+$', lambda m: '_' * len(m.group()), result)
    if result in ('', '.', '..'):
        result = '_' * max(1, len(result))
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', result):
        result = '_' + result
    need(len(result.encode('utf-8')) <= 180,
         '课程名转换后超过180字节；请明确提供更短的课程标题，不会自动截断名称')
    return result


def stamp(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'
    return f'{seconds//60:02d}:{seconds%60:02d}'


TIME = re.compile(r'^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?$')


def parse_srt(raw, label):
    try:
        body = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').strip()
    except UnicodeDecodeError:
        raise ValidationError(f'{label} 必须为UTF-8字幕；保留原文件后另做明确编码转换')
    need(bool(body), f'{label} 是空字幕')
    cues = []
    for n, block in enumerate(re.split(r'\n\s*\n', body), 1):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        need(bool(lines), f'{label} 第{n}条缺少时间轴')
        match = TIME.fullmatch(lines[0].strip())
        need(bool(match), f'{label} 第{n}条时间轴无效')
        vals = [int(v) for v in match.groups()]
        need(vals[1] < 60 and vals[2] < 60 and vals[5] < 60 and vals[6] < 60,
             f'{label} 第{n}条时间字段超界')
        start = vals[0]*3600 + vals[1]*60 + vals[2] + vals[3]/1000
        end = vals[4]*3600 + vals[5]*60 + vals[6] + vals[7]/1000
        content = '\n'.join(lines[1:]).strip()
        need(end >= start and bool(content), f'{label} 第{n}条倒序或没有文本')
        need(not cues or start >= cues[-1]['start'], f'{label} 第{n}条开始时间倒序')
        cues.append({'start':start, 'end':end, 'text':content})
    return cues


def relative_input(path_value, root, label):
    raw_path = Path(text(path_value, label))
    need(not raw_path.is_absolute() and '..' not in raw_path.parts,
         f'{label} 必须是输入JSON目录内的相对路径，不能包含 ..')
    path = (root / raw_path).resolve()
    need(path.is_relative_to(root), f'{label} 的符号链接逃逸输入目录')
    need(path.is_file(), f'{label} 文件不存在：{raw_path}')
    return path


def interval(start, end, duration, label):
    start = integer(start, label+' start')
    end = integer(end, label+' end')
    need(start < end <= duration, f'{label} 必须满足 0 <= start < end <= 分集时长')
    return start, end


def validate(data, input_root, allow_incomplete):
    need(isinstance(data, dict) and type(data.get('schema_version')) is int and data.get('schema_version') == 2,
         'schema_version 必须为整数2；旧版摘要数据须补充教学正文、知识覆盖表和自测，不能直接冒称完整阅读版')
    course = data.get('course', {})
    title = text(course.get('title'), 'course.title')
    normalized = {'title':title, 'source_url':web_url(course.get('source_url'), 'course.source_url'),
                  'description':text(course.get('description'), 'course.description'),
                  'language':text(course.get('language'), 'course.language'),
                  'scope_note':course.get('scope_note', ''), 'overview':[]}
    need(isinstance(normalized['scope_note'], str), 'course.scope_note 必须是文本')
    overview = course.get('overview', [])
    need(isinstance(overview, list) and len(overview) > 0,
         'course.overview 必须至少有一个完成内容分析的模块；不能直接生成准备阶段的空骨架')
    for i, item in enumerate(overview):
        normalized['overview'].append({'title':text(item.get('title'), f'overview[{i}].title'),
                                       'body':text(item.get('body'), f'overview[{i}].body')})
    source_eps = data.get('episodes')
    need(isinstance(source_eps, list) and len(source_eps) > 0, 'episodes 必须是非空列表')
    episodes, identities, ids, warnings = [], set(), set(), []
    all_lesson_ids=set()
    for n, item in enumerate(source_eps, 1):
        label = f'episodes[{n-1}]'
        eid = text(item.get('id'), label+'.id')
        need(eid not in ids, f'分集id重复：{eid}')
        ids.add(eid)
        part = integer(item.get('p'), label+'.p', 1)
        episode_url, identity = video_url(item.get('url'), part, label+'.url')
        need(identity not in identities, f'同一视频分P重复：{identity}')
        identities.add(identity)
        duration = integer(item.get('duration_seconds'), label+'.duration_seconds', 1)
        status = item.get('status')
        need(status in ('available','missing','restricted','failed'), f'{label}.status 无效')
        need(status == 'available' or allow_incomplete,
             f'{eid} 字幕状态为 {status}；补齐后生成，或用 --allow-incomplete 明确输出缺失版本')
        e = {'id':eid, 'p':part, 'title':text(item.get('title'), label+'.title'),
             'lecture_label':item.get('lecture_label',''), 'url':episode_url,
             'duration_seconds':duration, 'status':status, 'summary':'', 'methods':[],
             'watch_windows':[], 'lessons':[], 'self_check':[], 'coverage':[], 'required_visual':[],
             'subtitle_origin':item.get('subtitle_origin','platform'),
             'subtitle_file':None, 'cue_count':0, 'sha256':None, 'raw':None,
             'acquisition_status':item.get('acquisition_status',status),
             'acquisition_notes':strings(item.get('acquisition_notes',[]),label+'.acquisition_notes'),
             'duplicate_subtitle_review':item.get('duplicate_subtitle_review')}
        need(isinstance(e['lecture_label'],str), f'{label}.lecture_label 必须是文本')
        need(isinstance(e['acquisition_status'],str), f'{label}.acquisition_status 必须是文本')
        need(e['subtitle_origin'] in ('platform','asr','provided'), f'{label}.subtitle_origin 无效')
        if status != 'available':
            need(not any(item.get(k) for k in ('watch_windows','summary','methods','lessons','self_check','coverage','required_visual')),
                 f'{eid} 缺少字幕时不能带有假定已完成的摘要、方法或回看窗口；请改为独立来源的后续整理')
            need(not item.get('subtitle_file'), f'{eid} 非available状态不应指定字幕文件')
            episodes.append(e)
            continue
        e['summary'] = text(item.get('summary'), label+'.summary')
        e['methods'] = strings(item.get('methods'), label+'.methods')
        path = relative_input(item.get('subtitle_file'), input_root, label+'.subtitle_file')
        need(path.suffix.lower() == '.srt', f'{eid} 生成器只接受已转换并保留来源记录的SRT')
        e['raw'] = path.read_bytes()
        cues = parse_srt(e['raw'], str(path.relative_to(input_root)))
        e.update(sha256=hashlib.sha256(e['raw']).hexdigest(), cue_count=len(cues),
                 first_seconds=cues[0]['start'], last_seconds=cues[-1]['end'])
        need(cues[-1]['end'] <= duration + 2, f'{eid} 字幕时间超过视频时长2秒，可能串集或时长错误')
        if duration-cues[-1]['end'] > max(20,duration*0.05):
            warnings.append(f'{eid} 最后字幕距结尾 {duration-cues[-1]["end"]:.1f} 秒，需核对无声画面或缺字幕')
        e['subtitle_file'] = f'subtitles/{n:03d}.srt'
        lessons=item.get('lessons')
        need(isinstance(lessons,list) and lessons, f'{eid} 必须有可独立学习的lessons正文，summary不能代替正文')
        lesson_ids=set()
        for j,lesson in enumerate(lessons):
            ll=f'{eid}.lessons[{j}]'
            lid=text(lesson.get('id'),ll+'.id')
            need(re.fullmatch(r'[A-Za-z0-9_-]+',lid) and lid not in all_lesson_ids, f'{ll} id无效或在全课程重复')
            lesson_ids.add(lid)
            all_lesson_ids.add(lid)
            start,end=interval(lesson.get('start'),lesson.get('end'),duration,ll)
            paragraphs=strings(lesson.get('paragraphs'),ll+'.paragraphs')
            need(paragraphs, f'{ll} 不能只有标题或要点，必须有解释正文')
            l={'id':lid,'title':text(lesson.get('title'),ll+'.title'),'start':start,'end':end,
               'paragraphs':paragraphs,'bullets':strings(lesson.get('bullets',[]),ll+'.bullets')}
            if lesson.get('table'):
                t=lesson['table'];headers=strings(t.get('headers'),ll+'.table.headers')
                need(headers and isinstance(t.get('rows'),list) and t['rows'],f'{ll} 表格须有表头与数据')
                rows=[strings(row,ll+'.table.rows') for row in t['rows']]
                need(all(len(row)==len(headers) for row in rows),f'{ll} 表格列数不一致')
                l['table']={'headers':headers,'rows':rows}
            e['lessons'].append(l)
        coverage=item.get('coverage')
        need(isinstance(coverage,list) and coverage,f'{eid} 缺少知识覆盖对照表')
        cursor=0;used=set()
        for j,segment in enumerate(coverage):
            cl=f'{eid}.coverage[{j}]';start,end=interval(segment.get('start'),segment.get('end'),duration,cl)
            need(start==cursor,f'{cl} 覆盖区间有遗漏或重叠：应从{cursor}开始')
            cursor=end;disposition=segment.get('disposition')
            need(disposition in ('taught','visual_only','non_instructional'),f'{cl} disposition无效')
            refs=strings(segment.get('lesson_ids',[]),cl+'.lesson_ids')
            need(all(ref in lesson_ids for ref in refs),f'{cl} 引用了不存在的正文')
            need(disposition!='taught' or refs,f'{cl} 已讲解的知识须指向正文')
            topics=strings(segment.get('topics'),cl+'.topics');need(topics,f'{cl} 须列出实际知识内容')
            note=text(segment.get('note'),cl+'.note')
            used.update(refs)
            e['coverage'].append({'start':start,'end':end,'topics':topics,'lesson_ids':refs,'disposition':disposition,'note':note})
        need(cursor==duration,f'{eid} 知识覆盖对照表未到分集结尾')
        need(used==lesson_ids,f'{eid} 有正文未关联课程来源区间')
        checks=item.get('self_check')
        need(isinstance(checks,list) and checks,f'{eid} 需提供有参考答案的自测，以检查文字是否讲清')
        for j,check in enumerate(checks):
            e['self_check'].append({k:text(check.get(k),f'{eid}.self_check[{j}].{k}') for k in ('question','answer')})
        for j,visual in enumerate(item.get('required_visual',[])):
            vl=f'{eid}.required_visual[{j}]';start,end=interval(visual.get('start'),visual.get('end'),duration,vl)
            v={k:text(visual.get(k),vl+'.'+k) for k in ('title','reason','watch_for')}
            v.update(start=start,end=end,url=at_url(episode_url,start),episode_id=eid)
            e['required_visual'].append(v)
        for segment in e['coverage']:
            if segment['disposition']=='visual_only':
                need(any(v['start']<segment['end'] and v['end']>segment['start'] for v in e['required_visual']),
                     f'{eid} 只能看画面的覆盖区间需要对应回看任务')
        source_windows = item.get('watch_windows', [])
        need(isinstance(source_windows,list), f'{label}.watch_windows 必须是列表')
        for j, window in enumerate(source_windows):
            wl=f'{eid}.watch_windows[{j}]'
            start,end=interval(window.get('start'),window.get('end'),duration,wl)
            need(window.get('level') in ('essential','suggested'), f'{wl}.level 必须为essential或suggested')
            w={k:text(window.get(k),wl+'.'+k) for k in ('title','reason','watch_for')}
            w.update(start=start,end=end,level=window['level'],url=at_url(episode_url,start),episode_id=eid,evidence=[],visual_checks=[])
            evs=window.get('evidence')
            need(isinstance(evs,list) and len(evs)>0, f'{wl} 必须有至少一项字幕证据')
            for ev in evs:
                s=integer(ev.get('start'),wl+'.evidence.start')
                t=ev.get('end',s)
                integer(t,wl+'.evidence.end')
                need(start<=s<=t<=end, f'{wl} 证据时间必须位于当前窗口内')
                need(any(c['start']-2 <= s <= c['end']+2 for c in cues), f'{wl} 证据起点不在任何字幕附近')
                w['evidence'].append({'start':s,'end':t,'text':text(ev.get('text'),wl+'.evidence.text')})
            for check in window.get('visual_checks',[]):
                s=integer(check.get('at'),wl+'.visual_checks.at')
                need(start<=s<=end, f'{wl} 画面抽查时间不在窗口内')
                w['visual_checks'].append({'at':s,'observation':text(check.get('observation'),wl+'.visual_checks.observation')})
            # An input URL is not trusted as identity; reject conflicts before rebuilding.
            if window.get('url'):
                checked,ident=video_url(window['url'],part,wl+'.url')
                need(ident==identity, f'{wl} 指向了另一分集')
                times=parse_qs(urlsplit(window['url']).query).get('t')
                need(not times or times==[str(start)], f'{wl}.url 的 t 与起点不一致')
            e['watch_windows'].append(w)
        episodes.append(e)
    hashes={}
    for e in episodes:
        if e['sha256']:
            prior=hashes.get(e['sha256'])
            if prior:
                review=e['duplicate_subtitle_review']
                need(isinstance(review,dict) and review.get('same_as')==prior,
                     f'{e["id"]} 与 {prior} 字幕字节相同，需核对串集；确为重复片段时，记录 duplicate_subtitle_review.same_as 与 reason 后重试')
                e['duplicate_subtitle_review']={'same_as':prior,
                    'reason':text(review.get('reason'),e['id']+'.duplicate_subtitle_review.reason')}
                warnings.append(f'{e["id"]} 与 {prior} 字幕相同，已记录复核依据：{e["duplicate_subtitle_review"]["reason"]}')
            else:
                need(e['duplicate_subtitle_review'] is None,
                     f'{e["id"]} 的重复字幕复核标记没有对应的已出现相同字幕，请核对输入')
                hashes[e['sha256']]=e['id']
    by_id={e['id']:e for e in episodes}
    route=[]
    need(isinstance(data.get('learning_route',[]),list), 'learning_route 必须是列表')
    for i,r in enumerate(data.get('learning_route',[])):
        label=f'learning_route[{i}]'
        e=by_id.get(r.get('episode_id'))
        need(bool(e) and e['status']=='available',f'{label} 指向不存在或缺字幕的分集')
        start,end=interval(r.get('start'),r.get('end'),e['duration_seconds'],label)
        need(any(w['start']<end and w['end']>start for w in e['required_visual']),
             f'{label} 应关联已标注的回看窗口')
        route.append({'episode_id':e['id'],'start':start,'end':end,'title':text(r.get('title'),label+'.title'),
                      'focus':text(r.get('focus'),label+'.focus'),'url':at_url(e['url'],start)})
    sources=[]
    for i,s in enumerate(data.get('sources',[])):
        sources.append({'title':text(s.get('title'),f'sources[{i}].title'),
                        'url':web_url(s.get('url'),f'sources[{i}].url')})
    limits=strings(data.get('limitations',[]),'limitations')
    return normalized,episodes,route,sources,limits,warnings


def esc(value):
    return html.escape(str(value),quote=True)


def a(label,url):
    return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(label)}</a>'


def csv_bytes(rows):
    stream=io.StringIO(newline='')
    csv.writer(stream).writerows(rows)
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def unique_route_seconds(route):
    """Measure the union of selected intervals separately for each episode."""
    by_episode={}
    for segment in route:
        by_episode.setdefault(segment['episode_id'],[]).append((segment['start'],segment['end']))
    total=0
    for intervals in by_episode.values():
        current_start,current_end=sorted(intervals)[0]
        for start,end in sorted(intervals)[1:]:
            if start<=current_end:
                current_end=max(current_end,end)
            else:
                total+=current_end-current_start
                current_start,current_end=start,end
        total+=current_end-current_start
    return total


def render(course,episodes,route,sources,limits,warnings,base):
    windows=[w for e in episodes for w in e['watch_windows']]
    available=sum(e['status']=='available' for e in episodes)
    count_essential=sum(w['level']=='essential' for w in windows)
    subtitle_dir=base+'_字幕'
    for e in episodes:
        if e['subtitle_file']:
            e['subtitle_file']=e['subtitle_file'].replace('subtitles/',subtitle_dir+'/',1)
    display={'essential':'涉及观看任务','suggested':'按需核对'}
    status_text={'available':'教学正文已整理','missing':'未取得字幕','restricted':'访问受限','failed':'提取失败'}
    origin_text={'platform':'平台字幕','asr':'语音识别转写','provided':'提供的字幕'}
    artifacts={}
    cards=[]
    coverage_rows=[['分集','起始','结束','知识点','正文位置','处理方式','核对说明']]
    md=[f'# {course["title"]}',course['description'],f'课程来源：[打开课程]({course["source_url"]})',
        f'分集覆盖：{available}/{len(episodes)}；字幕语言：{course["language"]}；总页面时长：{stamp(sum(e["duration_seconds"] for e in episodes))}。',
        course['scope_note']]
    for item in course['overview']:
        md.extend([f'## {item["title"]}',item['body']])
    md.append('## 课程正文')
    for e in episodes:
        meta=' · '.join(x for x in (e['lecture_label'],'原片 '+stamp(e['duration_seconds']),status_text[e['status']]) if x)
        methods=''.join('<li>'+esc(x)+'</li>' for x in e['methods'])
        recs=''.join('<li>'+a(stamp(w['start'])+'–'+stamp(w['end'])+' '+w['title'],w['url'])+' <small>'+display[w['level']]+'</small></li>' for w in e['watch_windows'])
        if e['status']=='available':
            recs=recs or '<li>本次未标出需要单独回看的窗口；不等于本集没有课件或画面。</li>'
            review=e['duplicate_subtitle_review']
            review_note=f'<p class="notice">与 {esc(review["same_as"])} 字幕相同，复核说明：{esc(review["reason"])}</p>' if review else ''
            lesson_html=[]
            for lesson in e['lessons']:
                table_html=''
                if lesson.get('table'):
                    table_html='<div class="table-scroll"><table><thead><tr>'+''.join('<th>'+esc(x)+'</th>' for x in lesson['table']['headers'])+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in row)+'</tr>' for row in lesson['table']['rows'])+'</tbody></table></div>'
                bullets='<ul>'+''.join('<li>'+esc(x)+'</li>' for x in lesson['bullets'])+'</ul>' if lesson['bullets'] else ''
                lesson_html.append(f'<section class="lesson" id="{esc(lesson["id"])}"><h3>{esc(lesson["title"])}</h3>'+''.join('<p>'+esc(p)+'</p>' for p in lesson['paragraphs'])+bullets+table_html+f'<small class="source-note">课程依据：{esc(e["id"])} {stamp(lesson["start"])}–{stamp(lesson["end"])}；需要核对时{a("定位原片",at_url(e["url"],lesson["start"]))}</small></section>')
            questions=''.join(f'<div class="question"><p><strong>{i}. {esc(c["question"])}</strong></p><details><summary>查看参考答案</summary><p>{esc(c["answer"])}</p></details></div>' for i,c in enumerate(e['self_check'],1))
            required='<p class="text-ready">本章知识可先通过正文学习；原片用于核对或按需体验。</p>'
            if e['required_visual']:
                required='<aside class="visual-task"><h4>文字之外的观看任务</h4>'+''.join('<p>'+a(stamp(v['start'])+'–'+stamp(v['end'])+' '+v['title'],v['url'])+'：'+esc(v['reason'])+'</p><p><strong>要完成的观察：</strong>'+esc(v['watch_for'])+'</p>' for v in e['required_visual'])+'</aside>'
            coverage_html=''.join('<li>'+stamp(c['start'])+'–'+stamp(c['end'])+'：'+esc('、'.join(c['topics']))+'。'+esc(c['note'])+'</li>' for c in e['coverage'])
            body=f'<p class="chapter-intro">{esc(e["summary"])}</p>{review_note}'+''.join(lesson_html)+f'<section class="self-check"><h3>读后自测 · 整理者编制</h3>{questions}</section>{required}<details class="reference"><summary>本章来源、覆盖核对与补充回看</summary><ul>{coverage_html}</ul><h4>方法回顾与练习</h4><ul>{methods}</ul><h4>补充原片索引（按需）</h4><ul>{recs}</ul><p>{a("原始SRT",quote(e["subtitle_file"]))} · {origin_text[e["subtitle_origin"]]}</p></details>'
            artifacts[e['subtitle_file']]=e['raw']
        else:
            notes=''.join('<li>'+esc(x)+'</li>' for x in e['acquisition_notes'])
            reason=f'<p>原采集状态：{esc(e["acquisition_status"])}</p>'
            reason+=('<ul>'+notes+'</ul>') if notes else '<p>未记录更具体的失败原因，不能据此判断视频本身没有字幕。</p>'
            body=f'<p class="notice">{status_text[e["status"]]}。本集尚未编写教学正文；补齐材料后继续整理。</p>{reason}'
        cards.append(f'<details open id="chapter-{esc(e["id"])}" class="episode searchable" data-episode="{esc(e["id"])}"><summary><span class="pnum">{esc(e["id"])}</span><span>{esc(e["title"])}<small>{esc(meta)}</small></span><span class="chevron">＋</span></summary><div class="detail">{body}</div></details>')
        md.extend([f'### {e["id"]} · {e["title"]}',f'{meta} ｜ [观看本集]({e["url"]})'])
        if e['status']=='available':
            md.append(e['summary'])
            for lesson in e['lessons']:
                md.extend([f'#### {lesson["title"]}',*lesson['paragraphs']])
                if lesson['bullets']:md.append('\n'.join('- '+x for x in lesson['bullets']))
                if lesson.get('table'):
                    rows=lesson['table'];md.append('\n'.join(['|'+'|'.join(rows['headers'])+'|','|'+'|'.join(['---']*len(rows['headers']))+'|']+['|'+'|'.join(r)+'|' for r in rows['rows']]))
                md.append(f'课程依据：{e["id"]} {stamp(lesson["start"])}–{stamp(lesson["end"])}。')
            md.append('#### 读后自测（整理者编制）')
            for c in e['self_check']:md.extend([c['question'],'参考答案：'+c['answer']])
            for v in e['required_visual']:md.append(f'需看画面：[{stamp(v["start"])}–{stamp(v["end"])} {v["title"]}]({v["url"]})。{v["reason"]} 观察：{v["watch_for"]}')
            for c in e['coverage']:coverage_rows.append([e['id'],stamp(c['start']),stamp(c['end']),'；'.join(c['topics']),'；'.join(c['lesson_ids']),c['disposition'],c['note']])
            md.extend(['**方法回顾与练习**\n\n'+'\n'.join('- '+x for x in e['methods']),
                       f'[原始SRT]({quote(e["subtitle_file"])}) · {origin_text[e["subtitle_origin"]]}'])
            for w in e['watch_windows']:
                md.append(f'- [{stamp(w["start"])}–{stamp(w["end"])} {w["title"]}]({w["url"]}) · {display[w["level"]]}：{w["reason"]}')
        else:
            md.append(f'本集未取得字幕，尚未编写教学正文。原采集状态：{e["acquisition_status"]}。')
            md.extend('- '+x for x in e['acquisition_notes'])
            if not e['acquisition_notes']:
                md.append('未记录更具体的失败原因，不能据此判断视频本身没有字幕。')
    watch_cards=[]
    watch_rows=[['分集','主题','起始','结束','标注','为什么看','观察重点','链接','字幕依据','画面抽查']]
    for w in windows:
        evidence='；'.join(stamp(x['start'])+' '+x['text'] for x in w['evidence'])
        checks='；'.join(stamp(x['at'])+' '+x['observation'] for x in w['visual_checks'])
        check_html='<p>画面抽查：'+esc(checks)+'。仅确认所列时点。</p>' if checks else '<p>此窗口依据字幕定位，未记录画面抽查。</p>'
        watch_cards.append(f'<article class="watch searchable" data-episode="{esc(w["episode_id"])}" data-level="{w["level"]}"><div class="watch-meta"><span>{esc(w["episode_id"])} · {stamp(w["start"])}–{stamp(w["end"])}</span><b class="badge {w["level"]}">{display[w["level"]]}</b></div><h3>{a(w["title"],w["url"])}</h3><p>{esc(w["reason"])}</p><p><strong>重点观察：</strong>{esc(w["watch_for"])}</p><details class="basis"><summary>查看定位依据</summary><p>{esc(evidence)}</p>{check_html}</details></article>')
        watch_rows.append([w['episode_id'],w['title'],stamp(w['start']),stamp(w['end']),display[w['level']],w['reason'],w['watch_for'],w['url'],evidence,checks])
    route_cards=''.join(f'<article class="route searchable" data-episode="{esc(r["episode_id"])}"><span class="route-number">{i:02d}</span><div><h3>{a(r["title"],r["url"])}</h3><small>{esc(r["episode_id"])} · {stamp(r["start"])}–{stamp(r["end"])}</small><p>{esc(r["focus"])}</p></div></article>' for i,r in enumerate(route,1))
    if not route:
        route_cards='<p class="notice">本次未列出必须补充的画面任务，可直接学习正文；原片索引仅供核对。</p>'
    md.append('## 文字之外的观看任务')
    route_playback=sum(r['end']-r['start'] for r in route)
    route_unique=unique_route_seconds(route)
    md.append(f'{len(route)}段，按同一分集内的区间去重后为 {stamp(route_unique)}；逐段完整播放合计 {stamp(route_playback)}。暂停思考与练习时间另计。')
    for r in route:
        md.append(f'- [{r["episode_id"]} {stamp(r["start"])}–{stamp(r["end"])} {r["title"]}]({r["url"]})：{r["focus"]}')
    if limits or warnings:
        md.append('## 边界与待核对项')
        md.extend('- '+x for x in limits+warnings)
    md.append('## 辅助来源')
    md.extend(f'- [{s["title"]}]({s["url"]})' for s in sources)
    artifacts[base+'_课程正文.md']=('\n\n'.join(x for x in md if x)+'\n').encode('utf-8')
    artifacts[base+'_知识覆盖表.csv']=csv_bytes(coverage_rows)
    artifacts[base+'_观看索引.csv']=csv_bytes(watch_rows)
    manifest_episodes=[]
    for e in episodes:
        manifest_episodes.append({k:v for k,v in e.items() if k!='raw'})
    stats={'episodes':len(episodes),'available_subtitles':available,'incomplete':available<len(episodes),
           'subtitle_cues':sum(e['cue_count'] for e in episodes),'duration_seconds':sum(e['duration_seconds'] for e in episodes),
           'watch_windows':len(windows),'essential_windows':count_essential,'suggested_windows':len(windows)-count_essential,
           'route_segments':len(route),'route_seconds':route_unique,'route_playback_seconds':route_playback,
           'route_overlap_seconds':route_playback-route_unique,
           'visual_spot_checks':sum(len(w['visual_checks']) for w in windows)}
    stats.update(lesson_sections=sum(len(e['lessons']) for e in episodes),self_checks=sum(len(e['self_check']) for e in episodes),
                 teaching_characters=sum(sum(len(p) for p in l['paragraphs'])+sum(len(b) for b in l['bullets'])+sum(len(x) for row in l.get('table',{}).get('rows',[]) for x in row) for e in episodes for l in e['lessons']))
    manifest={'schema_version':2,'course':course,'stats':stats,'episodes':manifest_episodes,'learning_route':route,
              'sources':sources,'limitations':limits,'validation_warnings':warnings,
              'validation_boundary':'验证结构、时间覆盖、正文映射与原字节保留；知识是否完整讲清仍需对照字幕审阅。未声称完整观看所有画面。'}
    artifacts[base+'_manifest.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    table=[['分集','标题','讲次','视频链接','时长秒','字幕状态','字幕来源','字幕文件','字幕条数','SHA256','首字幕秒','末字幕秒','原采集状态','采集说明','重复字幕复核']]
    table.extend([[e['id'],e['title'],e['lecture_label'],e['url'],e['duration_seconds'],e['status'],e['subtitle_origin'],e['subtitle_file'] or '',e['cue_count'],e['sha256'] or '',e.get('first_seconds',''),e.get('last_seconds',''),e['acquisition_status'],'；'.join(e['acquisition_notes']),json.dumps(e['duplicate_subtitle_review'],ensure_ascii=False) if e['duplicate_subtitle_review'] else ''] for e in episodes])
    artifacts[base+'_字幕清单.csv']=csv_bytes(table)
    intro=''.join('<article><h3>'+esc(x['title'])+'</h3><p>'+esc(x['body'])+'</p></article>' for x in course['overview'])
    if not intro:
        intro='<article><h3>从逐集内容开始</h3><p>展开每集阅读摘要、方法与对应视频片段。</p></article>'
    options='<option value="all">全部分集</option>'+''.join(f'<option value="{esc(e["id"])}">{esc(e["id"]+" · "+e["title"])}</option>' for e in episodes)
    files=''.join(a(label,quote(base+suffix)) for suffix,label in [('_课程正文.md','完整课程正文 · Markdown'),('_知识覆盖表.csv','知识覆盖对照 · CSV'),('_观看索引.csv','原片索引 · CSV'),('_字幕清单.csv','字幕清单 · CSV'),('_manifest.json','内容与校验记录 · JSON')])
    replacements={'TITLE':esc(course['title']),'DESCRIPTION':esc(course['description']),
        'EPISODE_COUNT':str(len(episodes)),'COVERAGE':f'{available}/{len(episodes)}',
        'DURATION':stamp(stats['duration_seconds']),'WINDOW_COUNT':str(len(windows)),
        'CUES':f'{stats["subtitle_cues"]:,}','OVERVIEW':intro,'EPISODES':''.join(cards),
        'LESSON_COUNT':str(stats['lesson_sections']),'SELF_CHECK_COUNT':str(stats['self_checks']),
        'TEACHING_CHARACTERS':f'{stats["teaching_characters"]:,}',
        'CONTENTS':'<ol>'+''.join('<li><a class="chapter-link" href="#chapter-'+esc(e['id'])+'">'+esc(e['id']+' '+e['title'])+'</a></li>' for e in episodes)+'</ol>',
        'ROUTE':route_cards,'ROUTE_COUNT':str(len(route)),'ROUTE_DURATION':stamp(stats['route_seconds']),
        'ROUTE_TIMING_NOTE':f'若逐段完整播放，含重叠部分合计 {stamp(route_playback)}。' if route_playback!=route_unique else '所选片段没有相互重复覆盖。',
        'WINDOWS':''.join(watch_cards),'ESSENTIAL':str(count_essential),'SUGGESTED':str(len(windows)-count_essential),
        'EPISODE_OPTIONS':options,'FILES':files,'LANGUAGE':esc(course['language']),
        'SCOPE':esc(course['scope_note']) or '编号按课程清单展示，讲次与分P号分别保留；以各集链接为准。',
        'LIMITS':'<ul>'+''.join('<li>'+esc(x)+'</li>' for x in limits+warnings)+'</ul>',
        'SOURCES':' · '.join([a('课程原页面',course['source_url'])]+[a(s['title'],s['url']) for s in sources]),
        'INCOMPLETE':'<p class="notice incomplete">这是一份明确标记缺失分集的版本，字幕尚未全部取得。缺失集保留在目录中，未用其他集内容填补。</p>' if stats['incomplete'] else ''}
    template=Path(__file__).resolve().parent.parent/'assets/reader.html'
    page=template.read_text(encoding='utf-8')
    placeholder=re.compile(r'\{\{([A-Z_]+)\}\}')
    need(set(placeholder.findall(page)) <= replacements.keys(),'HTML模板含未定义占位符')
    # One substitution pass: text containing template-like braces remains literal data.
    page=placeholder.sub(lambda match:replacements[match.group(1)],page)
    artifacts[base+'.html']=page.encode('utf-8')
    # Archive only this build's explicit artifacts. Fixed timestamp makes repeat builds idempotent.
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for path,payload in sorted(artifacts.items()):
            info=zipfile.ZipInfo(base+'/'+path,(2020,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o644<<16
            z.writestr(info,payload)
    artifacts[base+'_资料包.zip']=archive.getvalue()
    return artifacts,stats,warnings


def write_artifacts(artifacts,output,overwrite):
    output=output.resolve()
    # Preflight every destination before changing any of them.
    conflicts=[]
    for rel,payload in artifacts.items():
        target=output/rel
        need(target.resolve().is_relative_to(output),f'输出路径越界：{rel}')
        need(not target.exists() or target.is_file(),f'输出路径不是普通文件：{rel}')
        if target.exists() and target.read_bytes()!=payload and not overwrite:
            conflicts.append(rel)
    need(not conflicts,'输出已有不同内容，未覆盖任何文件。换输出目录或明确使用 --overwrite：'+', '.join(conflicts[:4]))
    output.mkdir(parents=True,exist_ok=True)
    changed=0
    for rel,payload in artifacts.items():
        target=output/rel
        if target.exists() and target.read_bytes()==payload:
            continue
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False,prefix='.reader-') as f:
            temporary=Path(f.name)
            f.write(payload)
        temporary.replace(target)
        changed+=1
    return changed


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path,help='已完成阅读与标注的 course.json')
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--allow-incomplete',action='store_true',help='缺字幕集显式保留为空，不冒充完整')
    parser.add_argument('--overwrite',action='store_true',help='允许替换本次生成的同名不同内容文件')
    args=parser.parse_args()
    try:
        source=args.input.resolve()
        data=json.loads(source.read_text(encoding='utf-8'))
        course,episodes,route,sources,limits,warnings=validate(data,source.parent,args.allow_incomplete)
        base=safe_name(course['title'])
        artifacts,stats,warnings=render(course,episodes,route,sources,limits,warnings,base)
        changed=write_artifacts(artifacts,args.output_dir,args.overwrite)
        print(json.dumps({'ok':True,'input':str(source),'html':str(args.output_dir.resolve()/(base+'.html')),
                          'package':str(args.output_dir.resolve()/(base+'_资料包.zip')),'filename_changed':base!=course['title'],
                          'stats':stats,'files':list(artifacts),'changed_files':changed,'warnings':warnings},ensure_ascii=False,indent=2))
    except (ValueError,OSError,TypeError,AttributeError,KeyError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
