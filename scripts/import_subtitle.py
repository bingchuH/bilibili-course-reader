#!/usr/bin/env python3
"""Import one explicitly mapped browser Response JSON or SRT; no network or cookies."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit, parse_qs
from fetch_subtitles import AcquisitionError, Client, parse_body, parse_srt, save_subtitle, sha


def validated_catalog(path):
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data.get('course'),dict) or not data['course'].get('title'):
        raise ValueError('目录缺少 course.title。')
    if not isinstance(data.get('episodes'),list) or not data['episodes']:
        raise ValueError('目录需有完整、非空的 episodes 列表。')
    seen=set()
    for e in data['episodes']:
        ident=e.get('id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident) or ident in seen:
            raise ValueError('每集 id 必须唯一且仅含字母、数字、连字符或下划线。')
        seen.add(ident)
        if not e.get('title') or not isinstance(e.get('p'),int) or isinstance(e['p'],bool) or e['p']<1:
            raise ValueError(f'{ident}: 缺少有效 title/p。')
        duration=e.get('duration_seconds')
        if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<1 or int(duration)!=duration:
            raise ValueError(f'{ident}: 缺少已核实的正整数秒时长。')
        e['duration_seconds']=int(duration)
        u=urlsplit(e.get('url',''))
        if u.scheme!='https' or u.hostname not in ('www.bilibili.com','bilibili.com') or not re.search(r'/video/BV[A-Za-z0-9]{10}/?$',u.path):
            raise ValueError(f'{ident}: URL不是确切B站视频页。')
        if int(parse_qs(u.query).get('p',['1'])[0])!=e['p']:
            raise ValueError(f'{ident}: URL分P号与目录不一致。')
    return data


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('file',type=Path,help='仅字幕Response JSON正文或SRT，不接收HAR')
    ap.add_argument('--catalog',type=Path,required=True,help='acquisition.json，可由可见目录整理；未知cid可省略')
    ap.add_argument('--episode',required=True)
    ap.add_argument('--language',required=True)
    ap.add_argument('--origin',choices=['platform','provided','asr'],default='provided')
    ap.add_argument('--mapping-note',required=True,help='记录当前分集以及首尾/主题核对依据，不填凭据')
    ap.add_argument('--overwrite',action='store_true',help='显式替换同集不同内容的已导入字幕')
    args=ap.parse_args()
    try:
        catalog_path=args.catalog.resolve();base=catalog_path.parent
        data=validated_catalog(catalog_path)
        e=next((x for x in data['episodes'] if x['id']==args.episode),None)
        if not e:raise ValueError('--episode不在目录中。')
        raw=args.file.read_bytes();is_srt=args.file.suffix.lower()=='.srt'
        if len(raw)>20_000_000:raise ValueError('单文件超过20MB，请核对是否只导出了字幕正文。')
        if not is_srt:
            payload=Client.decode(raw)
            if 'log' in payload or 'request' in payload or 'headers' in payload:
                raise ValueError('只接受字幕Response正文，不接受HAR或请求信息。')
            cues=parse_body(payload)
        else:cues=parse_srt(raw)
        if cues[-1][1]>float(e['duration_seconds'])+2:
            raise ValueError('字幕结束时间超过视频时长2秒，请核对错集或页面时长。')
        old=e.get('subtitle',{})
        if old.get('status')=='downloaded' and old.get('raw_sha256')!=sha(raw) and not args.overwrite:
            raise ValueError('此集已有不同内容字幕；核对后再显式 --overwrite。')
        # Build in a temporary directory, then copy only the expected files.
        with tempfile.TemporaryDirectory(prefix='course-import-') as tmp:
            staged=dict(e)
            save_subtitle(Path(tmp),staged,raw,cues,args.language,'imported_response' if not is_srt else 'original_srt',is_srt=is_srt,imported=True)
            sub=staged['subtitle'];sub['origin']=args.origin
            sub['notes'].append(args.mapping_note)
            for key in ['raw_path','srt_path']:
                target=base/sub[key]
                if target.resolve().is_relative_to(base) is False:
                    raise ValueError('输出路径越界。')
                contents=(Path(tmp)/sub[key]).read_bytes()
                if target.exists() and target.read_bytes()!=contents and not args.overwrite:
                    raise ValueError('目标字幕文件已存在且内容不同，需要 --overwrite。')
            for key in ['raw_path','srt_path']:
                target=base/sub[key];target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((Path(tmp)/sub[key]).read_bytes())
            e['subtitle']=sub
        data.setdefault('acquisition',{}).update(mode='browser_response_or_file_import',status='complete' if all(x.get('subtitle',{}).get('status')=='downloaded' for x in data['episodes']) else 'partial')
        # Keep the source's catalogue-completeness statement; never infer it from import success.
        temporary=catalog_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(catalog_path)
        print(json.dumps({'episode':e['id'],'cue_count':len(cues),'first_seconds':cues[0][0],'last_seconds':cues[-1][1],'sha256':e['subtitle']['sha256'],'catalog':str(catalog_path)},ensure_ascii=False))
        return 0
    except (ValueError,KeyError,TypeError,OSError,AcquisitionError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
