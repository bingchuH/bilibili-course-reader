#!/usr/bin/env python3
"""Turn acquisition.json into an analysis skeleton and timestamped reading corpus."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from fetch_subtitles import parse_srt, stamp, AcquisitionError


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('acquisition',type=Path)
    ap.add_argument('--output',type=Path,help='course.json，必须与acquisition.json同目录')
    ap.add_argument('--overwrite',action='store_true',help='会重建分析骨架，不能用于保留手写分析的更新')
    args=ap.parse_args()
    try:
        src=args.acquisition.resolve();base=src.parent
        output=(args.output or base/'course.json').resolve()
        if output.parent!=base:raise ValueError('course.json必须与acquisition.json同目录；请一起移动整个课程工作目录。')
        if output==src:raise ValueError('输出不能覆盖采集元数据。')
        if output.exists() and not args.overwrite:raise ValueError('course.json已存在，未覆盖；续采后请只合并新分集，避免丢失已有分析。')
        data=json.loads(src.read_text(encoding='utf-8-sig'))
        if data.get('acquisition',{}).get('catalog_complete') is not True:
            raise ValueError('尚未确认目录完整性。先核对浏览器完整目录，再记录 catalog_complete=true 及scope_note。')
        if not data.get('episodes'):raise ValueError('没有非空分集目录。')
        course=data['course']
        result={'schema_version':2,'course':{'title':course['title'],'source_url':course['source_url'],'description':'','language':'','overview':[],'scope_note':course.get('scope_note','按已核对的目录整理；完成内容分析后补充实际覆盖说明。')},'episodes':[],'learning_route':[],'sources':[],'limitations':[]}
        analysis=base/'analysis';texts=[];corpus=[];languages=set();seen=set();digests={}
        for e in data['episodes']:
            ident=e['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident) or ident in seen:raise ValueError('分集id无效或重复。')
            seen.add(ident)
            duration=e.get('duration_seconds')
            if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<1 or int(duration)!=duration:
                raise ValueError(f'{ident}: 视频时长必须是已核实的正整数秒。')
            e['duration_seconds']=int(duration)
            sub=e.get('subtitle',{});state=sub.get('status','not_attempted')
            status='available' if state=='downloaded' else ('restricted' if state in ('login_required','access_restricted') else ('failed' if state in ('network_error','api_error','invalid_subtitle','invalid_response') else 'missing'))
            item={'id':ident,'p':e['p'],'title':e['title'],'lecture_label':e.get('lecture_label',''),'url':e['url'],'duration_seconds':e['duration_seconds'],'status':status,'subtitle_origin':sub.get('origin','platform'),'summary':'','methods':[],'watch_windows':[],'acquisition_status':state,'acquisition_notes':sub.get('notes',[])}
            item.update(lessons=[],self_check=[],coverage=[],required_visual=[])
            if status=='available':
                file=(base/sub['srt_path']).resolve()
                if not file.is_relative_to(base):raise ValueError(f'{ident}: 字幕路径越界。')
                raw=file.read_bytes();digest=hashlib.sha256(raw).hexdigest()
                if sub.get('sha256')!=digest:raise ValueError(f'{ident}: 字幕指纹不符。')
                review=e.get('duplicate_subtitle_review')
                if digest in digests:
                    if not isinstance(review,dict) or review.get('same_as')!=digests[digest] or not isinstance(review.get('reason'),str) or not review['reason'].strip():
                        raise ValueError(f'{ident}与{digests[digest]}字幕完全相同，请先核对串集；确为重复内容后记录duplicate_subtitle_review。')
                    item['duplicate_subtitle_review']={'same_as':digests[digest],'reason':review['reason'].strip()}
                else:
                    if review is not None:raise ValueError(f'{ident}: 重复字幕复核标记没有对应的已出现相同字幕。')
                    digests[digest]=ident
                cues=parse_srt(raw)
                if cues[-1][1]>float(e['duration_seconds'])+2:raise ValueError(f'{ident}: 字幕超过时长2秒，请核对分集或元数据。')
                item['subtitle_file']=file.relative_to(base).as_posix()
                languages.add(sub.get('language') or '未标明')
                texts.append((analysis/f'{ident}.txt',f'{ident} | {e["title"]}\n{e["url"]}\n时长 {e["duration_seconds"]} 秒\n\n'+'\n'.join(f'[{stamp(a)} --> {stamp(b)}] {text}' for a,b,text in cues)+'\n'))
                corpus.append({'id':ident,'title':e['title'],'url':e['url'],'cue_count':len(cues),'sha256':digest,'cues':[{'start':a,'end':b,'text':text} for a,b,text in cues]})
            else:
                result['limitations'].append(f'{ident} {e["title"]}：{state}。'+ '；'.join(sub.get('notes',[])))
            result['episodes'].append(item)
        result['course']['language']=', '.join(sorted(languages)) or '待确认'
        analysis.mkdir(exist_ok=True)
        for file,text in texts:file.write_text(text,encoding='utf-8')
        (analysis/'corpus.json').write_text(json.dumps({'episodes':corpus},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'output':str(output),'total_episodes':len(result['episodes']),'available':len(corpus),'analysis_required':True,'next':'完整阅读全部字幕，编写可独立学习的lessons正文、自测及coverage知识对照，摘要不能替代正文；完成后运行build_reader.py。'},ensure_ascii=False))
        return 0
    except (ValueError,KeyError,TypeError,OSError,AcquisitionError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
