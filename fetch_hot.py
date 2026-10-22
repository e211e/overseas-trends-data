# -*- coding: utf-8 -*-
"""
抓热点榜单 → data/hot.json

为什么要有这个脚本：
  这些源几乎都不发 CORS 头（掘金的 Access-Control-Allow-Origin 还写死成
  https://juejin.cn，微博直接 403，知乎要 UA），公共 CORS 代理
  （allorigins / codetabs / cors.lol / rss2json）现在全挂了。
  纯静态页面没有后端，所以只能在「构建 / 定时任务」这一侧抓，把结果落成
  data/hot.json，页面读同源文件 —— 零跨域、零代理、断网也能看。

  例外：index 页的「轻阅读」四源是浏览器直连（对方发 CORS 头），不走这里。

用法：
  python build/fetch_hot.py            # 抓一次，写 data/hot.json
  python build/fetch_hot.py --json     # 只打印，不落盘（调试用）
  python build/fetch_hot.py --only cn  # 只抓国内（或 os）

任何单个源失败都不影响其他源 —— 失败的榜单标 stale=true 并保留上一版内容。
"""
import json
import os
import re
import sys
import html
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'hot.json')
CST = timezone(timedelta(hours=8))

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')


# 海外源（Google Trends / Reddit / Wikipedia / Bluesky）在本机直连不通，要走代理。
# 国内源走代理反而更差，所以只有 use_proxy=True 的才走。
# 优先级：--proxy 参数 > 环境变量 HOT_PROXY > Windows 系统代理（客户端自己设的那个）。
PROXY = os.environ.get('HOT_PROXY', '')


def detect_proxy():
    """去问 Windows 系统代理。

    本地客户端（Clash / v2rayN / Surge）每次重启端口都可能变。之前把
    127.0.0.1:15236 写死在批处理和定时任务里，端口一换海外 4 个源就整片
    stale，而国内源全好、页面看起来「没坏」，很难发现。系统代理是客户端
    自己设的，跟着它走比自己猜端口靠谱。
    """
    if os.name != 'nt':
        return ''
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion'
                             r'\Internet Settings')
        on, _ = winreg.QueryValueEx(key, 'ProxyEnable')
        if not on:
            return ''
        srv, _ = winreg.QueryValueEx(key, 'ProxyServer')
    except Exception:
        return ''
    if not srv:
        return ''
    # 值可能是 "127.0.0.1:7890"，也可能是 "http=...;https=..."
    if '=' in srv:
        parts = dict(p.split('=', 1) for p in srv.split(';') if '=' in p)
        srv = parts.get('https') or parts.get('http') or ''
    if not srv:
        return ''
    return srv if srv.startswith(('http://', 'https://')) else 'http://' + srv


_OPENERS = {}


def opener_for(p):
    """按代理串缓存 opener。HN 一次要抓三十条，每次 build_opener 纯属浪费"""
    if p not in _OPENERS:
        # 空 ProxyHandler() 明确表示「不走代理」，否则 urllib 会捡系统环境变量
        _OPENERS[p] = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': p, 'https': p} if p else {}))
    return _OPENERS[p]


def get(url, referer=None, timeout=20, tries=3, use_proxy=False, cookie=None):
    """带重试的 GET。GitHub / 部分站点会偶发 502 或直接掐连接，退一次重来往往就好"""
    last = None
    opener = opener_for(PROXY if use_proxy else '')
    for i in range(tries):
        req = urllib.request.Request(url)
        req.add_header('User-Agent', UA)
        req.add_header('Accept', '*/*')
        req.add_header('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')
        if referer:
            req.add_header('Referer', referer)
        if cookie:
            req.add_header('Cookie', cookie)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'ignore')
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def clean(s):
    s = re.sub(r'<!\[CDATA\[|\]\]>', '', s or '')
    s = re.sub(r'<[^>]*>', '', s)
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def num(v):
    """把 '7904586' / '2,012' / 12000 统一成 int，失败给 0"""
    try:
        return int(re.sub(r'[^\d]', '', str(v)) or 0)
    except Exception:
        return 0


def cn_heat(v):
    """'903 万热度' → 9030000，'1.2 亿' → 120000000，认不出给 0。

    知乎热榜只给这种中文文案，没有数字字段。以前把 detail_text 原样塞进 d，
    而页面只渲染 n 不看 d —— 结果知乎那一列热度整列空白。这里翻译成数字。
    """
    m = re.search(r'([\d.]+)\s*(亿|万)?', str(v or '').replace(',', ''))
    if not m:
        return 0
    try:
        x = float(m.group(1))
    except ValueError:
        return 0
    return int(x * {'亿': 1e8, '万': 1e4}.get(m.group(2), 1))


def rss_items(xml, limit=20):
    """RSS 2.0：title / link / pubDate。拿不到 link 的条目直接丢"""
    out = []
    for block in re.findall(r'<item[\s>].*?</item>', xml, re.S):
        def g(tag):
            m = re.search(r'<%s[^>]*>(.*?)</%s>' % (tag, tag), block, re.S)
            return clean(m.group(1)) if m else ''
        title, link = g('title'), g('link')
        if title and link:
            out.append({'t': title, 'u': link, 'n': 0, 'd': g('pubDate')})
        if len(out) >= limit:
            break
    return out


def atom_items(xml, limit=20):
    """Atom：entry / title / link[rel=alternate] / content"""
    out = []
    for block in re.findall(r'<entry>(.*?)</entry>', xml, re.S):
        tm = re.search(r'<title[^>]*>(.*?)</title>', block, re.S)
        lm = re.search(r'<link[^>]*rel="alternate"[^>]*href="([^"]+)"', block)
        if not lm:
            lm = re.search(r'<link[^>]*href="([^"]+)"', block)
        cm = re.search(r'<content[^>]*>(.*?)</content>', block, re.S)
        title = clean(tm.group(1)) if tm else ''
        link = lm.group(1) if lm else ''
        desc = clean(cm.group(1)) if cm else ''
        desc = re.split(r'Discussion|Link', desc)[0].strip()
        if title and link:
            out.append({'t': title, 'u': link, 'n': 0, 'd': desc})
        if len(out) >= limit:
            break
    return out


# ───────────────────────────── 国内 ─────────────────────────────

def weibo_hot(limit=50):
    """微博热搜。weibo.com/ajax/statuses/hotSearch 现在 403，改用 hot_band + Referer"""
    raw = get('https://weibo.com/ajax/statuses/hot_band', referer='https://weibo.com/')
    j = json.loads(raw)
    out = []
    for row in (j.get('data') or {}).get('band_list') or []:
        word = row.get('word') or row.get('note')
        if not word:
            continue
        q = row.get('word_scheme') or word
        out.append({
            't': clean(word),
            'u': 'https://s.weibo.com/weibo?q=' + urllib.parse.quote(q),
            'n': num(row.get('num')),
            'd': row.get('label_name') or '',
        })
        if len(out) >= limit:
            break
    return out


def baidu_hot(limit=50):
    """百度热搜。数据内嵌在 HTML 注释 <!--s-data:{...}--> 里"""
    raw = get('https://top.baidu.com/board?tab=realtime')
    m = re.search(r'<!--s-data:(.*?)-->', raw, re.S)
    if not m:
        raise RuntimeError('s-data not found')
    d = json.loads(m.group(1))
    out = []
    for row in d['data']['cards'][0].get('content') or []:
        word = row.get('word') or row.get('query')
        if not word:
            continue
        out.append({
            't': clean(word),
            'u': row.get('rawUrl') or row.get('url') or
                 'https://www.baidu.com/s?wd=' + urllib.parse.quote(word),
            'n': num(row.get('hotScore')),
            'd': clean(row.get('desc'))[:60],
        })
        if len(out) >= limit:
            break
    return out


def douyin_hot(limit=50):
    """抖音热榜"""
    raw = get('https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/')
    j = json.loads(raw)
    out = []
    for row in j.get('word_list') or []:
        word = row.get('word')
        if not word:
            continue
        out.append({
            't': clean(word),
            'u': 'https://www.douyin.com/search/' + urllib.parse.quote(word),
            'n': num(row.get('hot_value')),
            'd': '',
        })
        if len(out) >= limit:
            break
    return out


def toutiao_hot(limit=50):
    """今日头条热榜"""
    raw = get('https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc',
              referer='https://www.toutiao.com/')
    j = json.loads(raw)
    out = []
    for row in j.get('data') or []:
        title = row.get('Title')
        if not title:
            continue
        out.append({
            't': clean(title),
            'u': row.get('Url') or 'https://www.toutiao.com/',
            'n': num(row.get('HotValue')),
            'd': row.get('Label') or '',
        })
        if len(out) >= limit:
            break
    return out


def zhihu_hot(limit=50):
    """知乎热榜。www.zhihu.com/api/v3 要登录 → 401，改用 api.zhihu.com"""
    raw = get('https://api.zhihu.com/topstory/hot-lists/total?limit=50',
              referer='https://www.zhihu.com/hot')
    j = json.loads(raw)
    out = []
    for row in j.get('data') or []:
        tgt = row.get('target') or {}
        title = tgt.get('title')
        qid = tgt.get('id')
        if not title or not qid:
            continue
        out.append({
            't': clean(title),
            'u': 'https://www.zhihu.com/question/%s' % qid,
            'n': cn_heat(row.get('detail_text')),
            'd': row.get('detail_text') or '',
        })
        if len(out) >= limit:
            break
    return out


def bili_hot(limit=50):
    """B 站热门（综合榜）"""
    raw = get('https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all',
              referer='https://www.bilibili.com/')
    j = json.loads(raw)
    out = []
    for row in (j.get('data') or {}).get('list') or []:
        title = row.get('title')
        bv = row.get('bvid')
        if not title or not bv:
            continue
        out.append({
            't': clean(title),
            'u': 'https://www.bilibili.com/video/%s' % bv,
            'n': num((row.get('stat') or {}).get('view')),
            'd': (row.get('owner') or {}).get('name') or '',
        })
        if len(out) >= limit:
            break
    return out


def sspai_new(limit=20):
    """少数派最新（小红书热榜没有公开接口，这一格用少数派替）"""
    raw = get('https://sspai.com/api/v1/article/index/page/get?limit=20&offset=0')
    j = json.loads(raw)
    rows = j.get('data') if isinstance(j, dict) else j
    out = []
    for row in rows or []:
        title, aid = row.get('title'), row.get('id')
        if not title or not aid:
            continue
        out.append({
            't': clean(title),
            'u': 'https://sspai.com/post/%s' % aid,
            'n': 0,
            'd': clean(row.get('summary'))[:50],
        })
        if len(out) >= limit:
            break
    return out


def woshipm_daily(limit=20):
    """人人都是产品经理 · 日榜（api2/app/article/popular/daily，按 scores 排）"""
    raw = get('https://www.woshipm.com/api2/app/article/popular/daily',
              referer='https://www.woshipm.com/')
    j = json.loads(raw)
    out = []
    for row in (j.get('RESULT') or []):
        d = row.get('data') or {}
        title, aid = clean(d.get('articleTitle')), d.get('id')
        if not title or not aid:
            continue
        out.append({
            't': title,
            'u': 'https://www.woshipm.com/?p=%s' % aid,
            'n': num(row.get('scores')),
            'd': d.get('timeStr') or '',
        })
        if len(out) >= limit:
            break
    return out


def woshipm_new(limit=20):
    """人人都是产品经理 · 最新（RSS 按时间倒序）"""
    return rss_items(get('https://www.woshipm.com/feed',
                         referer='https://www.woshipm.com/'), limit)


def juejin_hot(limit=50):
    """掘金热榜。必须带 Referer，否则 403"""
    raw = get('https://api.juejin.cn/content_api/v1/content/article_rank'
              '?category_id=1&type=hot', referer='https://juejin.cn/')
    j = json.loads(raw)
    if j.get('err_no') != 0:
        raise RuntimeError('juejin err_no=%s %s' % (j.get('err_no'), j.get('err_msg')))
    out = []
    for row in (j.get('data') or []):
        c = row.get('content') or {}
        title, cid = clean(c.get('title')), c.get('content_id')
        if not title or not cid:
            continue
        cnt = row.get('content_counter') or {}
        out.append({
            't': title,
            'u': 'https://juejin.cn/post/%s' % cid,
            'n': num(cnt.get('hot_rank') or cnt.get('view')),
            'd': '',
        })
        if len(out) >= limit:
            break
    return out


def baijing_new(limit=20):
    """白鲸出海 · 最新（/rss 是文章，/feed 是问答，别搞混）"""
    return rss_items(get('https://www.baijingapp.com/rss',
                         referer='https://www.baijingapp.com/'), limit)


# ───────────────────────────── 海外 ─────────────────────────────

def hacker_news(limit=30):
    """Hacker News 首页（news.ycombinator.com 的 RSS 在这里连不通，走 firebase）

    每条 item 是一次单独请求，30 条串行 + 重试要几十秒，是整轮抓取最慢的一环。
    并发 8 路之后压到两三秒。
    """
    from concurrent.futures import ThreadPoolExecutor
    ids = json.loads(get('https://hacker-news.firebaseio.com/v0/topstories.json'))[:limit]

    def one(i):
        try:
            return json.loads(get('https://hacker-news.firebaseio.com/v0/item/%s.json' % i,
                                  timeout=12, tries=2))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(one, ids))          # map 保序，榜单顺序不会乱

    out = []
    for it in items:
        if not it or it.get('type') != 'story' or not it.get('title'):
            continue
        out.append({
            't': clean(it['title']),
            'u': it.get('url') or 'https://news.ycombinator.com/item?id=%s' % it.get('id'),
            'n': num(it.get('score')),
            'd': '%s comments' % it.get('descendants', 0),
        })
    return out


def github_trending(limit=25):
    """GitHub Trending。
    优先抓 HTML 版（真·trending），但 github.com 在部分网络下会被掐连接
    （Tunnel 502 / Remote end closed），所以抓不到时退回官方 API：
    按「近 7 天新建 + star 最多的仓库」近似 trending，字段和排序都够用。
    """
    try:
        raw = get('https://github.com/trending?since=daily', tries=1)
        out = []
        for block in re.findall(r'<article class="Box-row">(.*?)</article>', raw, re.S):
            m = re.search(r'href="/([^"/]+/[^"/]+)"', block)
            if not m:
                continue
            repo = m.group(1)
            dm = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', block, re.S)
            sm = re.search(r'([\d,]+)\s*stars today', block)
            out.append({
                't': repo,
                'u': 'https://github.com/%s' % repo,
                'n': num(sm.group(1)) if sm else 0,
                'd': clean(dm.group(1))[:70] if dm else '',
            })
            if len(out) >= limit:
                break
        if out:
            return out
    except Exception:
        pass

    since = (datetime.now(CST) - timedelta(days=7)).strftime('%Y-%m-%d')
    raw = get('https://api.github.com/search/repositories'
              '?q=created:>%s&sort=stars&order=desc&per_page=%d' % (since, limit))
    j = json.loads(raw)
    out = []
    for r in j.get('items') or []:
        out.append({
            't': r.get('full_name') or '',
            'u': r.get('html_url') or '',
            'n': num(r.get('stargazers_count')),
            'd': clean(r.get('description'))[:70],
        })
    return [x for x in out if x['t'] and x['u']]


def product_hunt(limit=25):
    """Product Hunt（Atom feed，没有票数，取 tagline 当副标题）"""
    return atom_items(get('https://www.producthunt.com/feed'), limit)


def lobsters(limit=25):
    return rss_items(get('https://lobste.rs/rss'), limit)


def techcrunch(limit=20):
    return rss_items(get('https://techcrunch.com/feed/'), limit)


def the_verge(limit=20):
    """The Verge 用的是 Atom，不是 RSS 2.0"""
    return atom_items(get('https://www.theverge.com/rss/index.xml'), limit)


def ars_technica(limit=20):
    return rss_items(get('https://feeds.arstechnica.com/arstechnica/index'), limit)


def wired(limit=20):
    return rss_items(get('https://www.wired.com/feed/rss'), limit)


# ─────────── 以下 4 个走代理，直连不通（见文件头 PROXY 说明）───────────

def google_trends(limit=20):
    """Google Trends 每日趋势 RSS（geo=US）。
    <ht:approx_traffic> 是 '200+' 这种模糊值，真实搜索量 Google 不给，
    这里把数字取出来当热度、原始串塞进描述，页面显示 '200+ 搜索' 才对得上。
    """
    xml = get('https://trends.google.com/trending/rss?geo=US', use_proxy=True)
    out = []
    for block in re.findall(r'<item>(.*?)</item>', xml, re.S):
        t = re.search(r'<title>(.*?)</title>', block, re.S)
        if not t:
            continue
        traffic = re.search(r'<ht:approx_traffic>(.*?)</ht:approx_traffic>', block)
        news = re.search(r'<ht:news_item_title>(.*?)</ht:news_item_title>', block)
        q = clean(t.group(1))
        raw = clean(traffic.group(1)) if traffic else ''
        out.append({
            't': q,
            'u': 'https://trends.google.com/trends/explore?geo=US&q=' + urllib.parse.quote(q),
            'n': num(raw),
            'd': (raw + ' · ' if raw else '') + (clean(news.group(1))[:60] if news else ''),
        })
        if len(out) >= limit:
            break
    return out


def reddit_hot(limit=25):
    """Reddit r/popular（Atom）。Reddit 限流很硬，短时间内第二次请求就 429，
    所以只抓一次、不做重试风暴，失败就让上层沿用上一版。"""
    xml = get('https://www.reddit.com/r/popular/.rss?limit=%d' % limit,
              use_proxy=True, tries=1, timeout=25)
    out = []
    for block in re.findall(r'<entry>(.*?)</entry>', xml, re.S):
        t = re.search(r'<title>(.*?)</title>', block, re.S)
        link = re.search(r'<link href="([^"]+)"', block)
        sub = re.search(r'<category term="([^"]*)"', block)
        if not t or not link:
            continue
        out.append({
            't': clean(t.group(1)),
            'u': clean(link.group(1)),
            'n': 0,                                   # feed 里没有票数，不编
            'd': 'r/' + clean(sub.group(1)) if sub else '',
        })
        if len(out) >= limit:
            break
    return out


def wikipedia_top(limit=20):
    """Wikipedia 每日浏览量 Top（REST pageviews）。

    接口只给到「昨天」，而且当天数据经常还没生成（404 / items 为空）。
    以前只试昨天一天，赶上没出数据整个榜就 stale，所以往前退着试三天。
    前几名常年是 Main_Page / Special:Search 这类导航页，要滤掉。
    """
    j = None
    for back in (1, 2, 3):
        day = (datetime.now(CST) - timedelta(days=back)).strftime('%Y/%m/%d')
        try:
            d = json.loads(get('https://wikimedia.org/api/rest_v1/metrics/pageviews'
                               '/top/en.wikipedia/all-access/%s' % day,
                               use_proxy=True, tries=1))
            if (d.get('items') or [{}])[0].get('articles'):
                j = d
                break
        except Exception:
            continue
    if not j:
        raise RuntimeError('pageviews 近三天都没有数据')
    arts = j['items'][0]['articles']
    skip = ('Main_Page', 'Special:', 'Wikipedia:', 'Portal:', 'Help:',
            'File:', 'Category:', 'Template:', 'Talk:', 'Draft:', 'Module:')
    out = []
    for a in arts:
        t = a.get('article') or ''
        if not t or t.startswith(skip) or t == '-':
            continue
        out.append({
            't': t.replace('_', ' '),
            'u': 'https://en.wikipedia.org/wiki/' + urllib.parse.quote(t),
            'n': num(a.get('views')),
            'd': '',
        })
        if len(out) >= limit:
            break
    return out


def bluesky_hot(limit=20):
    """Bluesky 的 What's Hot 时间线（公开 XRPC，免 key）。
    这是目前唯一能拿到的「真·社媒热点」——TikTok/IG/FB 全都要登录态。"""
    u = ('https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed'
         '?feed=at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot'
         '&limit=%d' % limit)
    j = json.loads(get(u, use_proxy=True))
    out = []
    for it in j.get('feed') or []:
        p = it.get('post') or {}
        rec = p.get('record') or {}
        txt = re.sub(r'\s+', ' ', rec.get('text') or '').strip()
        if not txt:
            continue
        rkey = (p.get('uri') or '').rsplit('/', 1)[-1]
        handle = (p.get('author') or {}).get('handle', '')
        out.append({
            't': txt[:100] + ('…' if len(txt) > 100 else ''),
            'u': 'https://bsky.app/profile/%s/post/%s' % (handle, rkey),
            'n': num(p.get('likeCount')),
            'd': '@' + handle,
        })
        if len(out) >= limit:
            break
    return out


# (id, 名称, 副标题, 地区, 主页, 抓取函数, 数值单位)
BOARDS = [
    # ── 国内 ──
    ('weibo',    '微博热搜',           '实时',      'cn', 'https://s.weibo.com/top/summary', weibo_hot,    ''),
    ('baidu',    '百度热搜',           '实时',      'cn', 'https://top.baidu.com/board?tab=realtime', baidu_hot, ''),
    ('douyin',   '抖音热榜',           '实时',      'cn', 'https://www.douyin.com/hot', douyin_hot,   ''),
    ('toutiao',  '今日头条',           '热点',      'cn', 'https://www.toutiao.com/', toutiao_hot,    ''),
    ('zhihu',    '知乎热榜',           '综合',      'cn', 'https://www.zhihu.com/hot', zhihu_hot,    ''),
    ('bili',     'B 站热门',           '综合榜',    'cn', 'https://www.bilibili.com/v/popular/rank/all', bili_hot, ' 播放'),
    ('sspai',    '少数派',             '最新',      'cn', 'https://sspai.com/', sspai_new,            ''),
    ('woshipm-daily', '人人都是产品经理', '日榜',   'cn', 'https://www.woshipm.com/', woshipm_daily, ' 分'),
    ('woshipm-new',   '人人都是产品经理', '最新',   'cn', 'https://www.woshipm.com/', woshipm_new,   ''),
    ('juejin',   '掘金',               '热榜',      'cn', 'https://juejin.cn/', juejin_hot,          ''),
    ('baijing',  '白鲸出海',           '最新',      'cn', 'https://www.baijingapp.com/', baijing_new, ''),

    # ── 海外 ──
    ('gtrends',    'Google Trends',    'Daily · US', 'os', 'https://trends.google.com/trending?geo=US', google_trends, '+ 搜索'),
    ('reddit',     'Reddit',           'r/popular',  'os', 'https://www.reddit.com/r/popular/', reddit_hot,   ''),
    ('hn',       'Hacker News',        'Front page', 'os', 'https://news.ycombinator.com/', hacker_news, ' pts'),
    ('ph',       'Product Hunt',       'Today',      'os', 'https://www.producthunt.com/', product_hunt, ''),
    ('gh',       'GitHub Trending',    'Trending',   'os', 'https://github.com/trending', github_trending, ' ★'),
    ('wiki',       'Wikipedia',        'Most read',  'os', 'https://en.wikipedia.org/wiki/Wikipedia:Top_25_Report', wikipedia_top, ' 次'),
    ('bsky',       'Bluesky',          "What's Hot", 'os', 'https://bsky.app/', bluesky_hot,          ' ♥'),
    ('lobsters', 'Lobsters',           'Hot',        'os', 'https://lobste.rs/', lobsters,             ''),
    ('techcrunch', 'TechCrunch',       'Latest',     'os', 'https://techcrunch.com/', techcrunch,     ''),
    ('verge',    'The Verge',          'Latest',     'os', 'https://www.theverge.com/', the_verge,     ''),
    ('ars',      'Ars Technica',       'Latest',     'os', 'https://arstechnica.com/', ars_technica,   ''),
    ('wired',    'WIRED',              'Latest',     'os', 'https://www.wired.com/', wired,            ''),
]


def main():
    global PROXY
    only_json = '--json' in sys.argv
    region = None
    if '--only' in sys.argv:
        region = sys.argv[sys.argv.index('--only') + 1]
    if '--proxy' in sys.argv:
        PROXY = sys.argv[sys.argv.index('--proxy') + 1]
    elif not PROXY:
        PROXY = detect_proxy()
    if PROXY:
        print('代理：%s' % PROXY)
    else:
        print('代理：未设置（没读到系统代理，海外源会失败）')

    prev = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding='utf-8'))
            prev = {b['id']: b for b in old.get('boards', [])}
        except Exception:
            prev = {}

    boards = []
    for bid, name, sub, reg, home, fn, unit in BOARDS:
        if region and reg != region:
            if bid in prev:                 # 只抓一边时，另一边保持原样
                boards.append(prev[bid])
            continue
        try:
            items = fn()
            if not items:
                raise RuntimeError('empty')
            boards.append({'id': bid, 'name': name, 'sub': sub, 'region': reg,
                           'home': home, 'unit': unit, 'items': items, 'stale': False})
            print('  ✓ %-14s %-12s %2d 条   %s' % (bid, sub, len(items), items[0]['t'][:34]))
        except Exception as e:
            old = prev.get(bid)
            if old and old.get('items'):
                old = dict(old)
                old['stale'] = True
                boards.append(old)
                print('  ! %-14s 抓取失败（%s），沿用上一版 %d 条' % (bid, e, len(old['items'])))
            else:
                boards.append({'id': bid, 'name': name, 'sub': sub, 'region': reg,
                               'home': home, 'unit': unit, 'items': [], 'stale': True})
                print('  ✗ %-14s 抓取失败：%s' % (bid, e))

    now = datetime.now(CST)
    doc = {
        'updated': now.strftime('%Y-%m-%d %H:%M'),
        'updated_iso': now.isoformat(timespec='seconds'),
        'boards': boards,
    }
    if only_json:
        print(json.dumps(doc, ensure_ascii=False, indent=1)[:1200])
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    cn = sum(1 for b in boards if b['region'] == 'cn')
    total = sum(len(b['items']) for b in boards)
    print('\n写入 %s —— %d 个榜单（国内 %d / 海外 %d）/ %d 条，更新于 %s' %
          (os.path.relpath(OUT, ROOT), len(boards), cn, len(boards) - cn, total, doc['updated']))


if __name__ == '__main__':
    main()
