import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator


# =========================================================
# 配置
# =========================================================

BASE_URL = "https://t66y.com/"

LIST_URL = (
    "https://t66y.com/thread0806.php"
    "?fid=2&search=219675"
)

TARGET_AUTHOR = "愛在黑夜"

CACHE_FILE = "posts_cache.json"
FEED_FILE = "feed.xml"

# 目前确认至少有 47 页。
# 如果网站以后变成 48、49……程序会自动识别更大的数字。
MIN_TOTAL_PAGES = 47

SITE_TZ = timezone(
    timedelta(hours=8)
)

# 列表页间隔
PAGE_DELAY = 1.5

# 进入帖子详情页的间隔
POST_DELAY = 0.6

# 单页失败最多重试次数
MAX_RETRIES = 5

# None = RSS 中保留全部帖子
# 如果以后 feed.xml 太大，可以改成 500 / 1000
RSS_MAX_ITEMS = None


# =========================================================
# HTTP
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})


def get_soup(url, retries=3):

    last_error = None

    for attempt in range(1, retries + 1):

        try:

            print(f"请求：{url}")

            r = session.get(
                url,
                timeout=30
            )

            r.raise_for_status()

            if (
                not r.encoding
                or r.encoding.lower()
                == "iso-8859-1"
            ):
                r.encoding = r.apparent_encoding

            return BeautifulSoup(
                r.text,
                "html.parser"
            )

        except Exception as e:

            last_error = e

            print(
                f"请求失败 "
                f"{attempt}/{retries}：{e}"
            )

            time.sleep(
                3 * attempt
            )

    raise last_error


# =========================================================
# 缓存
# =========================================================

def load_cache():

    if not os.path.exists(
        CACHE_FILE
    ):

        print(
            "没有历史缓存："
            "本次为第一次全量抓取。"
        )

        return {}

    try:

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(
            data,
            dict
        ):
            return {}

        print(
            f"已读取缓存："
            f"{len(data)} 篇"
        )

        return data

    except Exception as e:

        print(
            "缓存读取失败：",
            e
        )

        return {}


def save_cache(cache):

    temp_file = (
        CACHE_FILE + ".tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_file,
        CACHE_FILE
    )

    print(
        f"缓存已保存："
        f"{len(cache)} 篇"
    )


# =========================================================
# 分页
# =========================================================

def make_page_url(page):

    if page == 1:
        return LIST_URL

    # 按网站分页链接的参数顺序
    return (
        "https://t66y.com/thread0806.php"
        f"?fid=2&page={page}&search=219675"
    )


def get_total_pages():

    print()
    print("正在检测总页数……")

    pages = {1}

    try:

        soup = get_soup(
            LIST_URL
        )

        for a in soup.find_all(
            "a",
            href=True
        ):

            try:

                full_url = urljoin(
                    BASE_URL,
                    a["href"]
                )

                query = parse_qs(
                    urlparse(
                        full_url
                    ).query
                )

                if (
                    query.get(
                        "fid",
                        [""]
                    )[0]
                    != "2"
                ):
                    continue

                if (
                    query.get(
                        "search",
                        [""]
                    )[0]
                    != "219675"
                ):
                    continue

                if "page" not in query:
                    continue

                page = int(
                    query["page"][0]
                )

                if page >= 1:
                    pages.add(page)

            except Exception:
                pass

    except Exception as e:

        print(
            "自动检测页数失败：",
            e
        )

    detected = max(pages)

    total = max(
        detected,
        MIN_TOTAL_PAGES
    )

    print(
        f"网页检测：{detected} 页"
    )

    print(
        f"本次扫描：1～{total} 页"
    )

    return total


# =========================================================
# 列表页
# =========================================================

def get_posts_from_page(page):

    soup = get_soup(
        make_page_url(page)
    )

    posts = []
    page_seen = set()

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get(
            "href",
            ""
        )

        if "htm_data" not in href:
            continue

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        url = urljoin(
            BASE_URL,
            href
        )

        if url in page_seen:
            continue

        page_seen.add(url)

        posts.append({
            "title": title,
            "url": url
        })

    return posts


def fetch_list_page(
    page,
    previous_signature=None
):

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            posts = (
                get_posts_from_page(
                    page
                )
            )

            if not posts:

                print(
                    f"⚠️ 第 {page} 页返回 0 条，"
                    f"重试 {attempt}/{MAX_RETRIES}"
                )

                time.sleep(
                    attempt * 5
                )

                continue

            signature = tuple(
                p["url"]
                for p in posts
            )

            if (
                previous_signature
                and signature
                == previous_signature
            ):

                print(
                    f"⚠️ 第 {page} 页疑似返回"
                    "上一页内容，重新请求。"
                )

                time.sleep(
                    attempt * 5
                )

                continue

            return posts, signature

        except Exception as e:

            print(
                f"⚠️ 第 {page} 页失败 "
                f"{attempt}/{MAX_RETRIES}：{e}"
            )

            time.sleep(
                attempt * 5
            )

    return None, None


def collect_all_list_posts():

    total_pages = (
        get_total_pages()
    )

    all_posts = []
    seen = set()

    failed_pages = []

    previous_signature = None

    for page in range(
        1,
        total_pages + 1
    ):

        print()
        print(
            "=" * 55
        )

        print(
            f"正在扫描 "
            f"{page}/{total_pages} 页"
        )

        print(
            "=" * 55
        )

        posts, signature = (
            fetch_list_page(
                page,
                previous_signature
            )
        )

        if not posts:

            print(
                f"❌ 第 {page} 页暂时失败，"
                "先继续下一页。"
            )

            failed_pages.append(
                page
            )

            continue

        previous_signature = (
            signature
        )

        new_count = 0

        for post in posts:

            if post["url"] in seen:
                continue

            seen.add(
                post["url"]
            )

            all_posts.append(
                post
            )

            new_count += 1

        print(
            f"第 {page} 页："
            f"{len(posts)} 条，"
            f"新增 {new_count} 条"
        )

        print(
            f"累计："
            f"{len(all_posts)} 条"
        )

        time.sleep(
            PAGE_DELAY
        )

    # -------------------------------
    # 第二轮补抓失败页面
    # -------------------------------

    if failed_pages:

        print()
        print(
            "需要补抓页面：",
            failed_pages
        )

        time.sleep(10)

        still_failed = []

        for page in failed_pages:

            posts, _ = (
                fetch_list_page(
                    page
                )
            )

            if not posts:

                still_failed.append(
                    page
                )

                continue

            for post in posts:

                if post["url"] in seen:
                    continue

                seen.add(
                    post["url"]
                )

                all_posts.append(
                    post
                )

            print(
                f"✓ 第 {page} 页补抓成功"
            )

            time.sleep(
                PAGE_DELAY
            )

        if still_failed:

            raise RuntimeError(
                "以下页面经过两轮重试仍失败："
                f"{still_failed}。"
                "为了避免发布残缺 RSS，"
                "本次停止更新。"
            )

    print()
    print(
        "=" * 55
    )

    print(
        f"{total_pages} 页全部扫描完成"
    )

    print(
        f"共发现 "
        f"{len(all_posts)} 个帖子"
    )

    print(
        "=" * 55
    )

    return all_posts


# =========================================================
# 发布时间
# =========================================================

def infer_year_from_url(
    post_url,
    fallback_month
):
    """
    例如：
    htm_data/2609/2/xxxx.html

    2609 = 2026 年 09 月
    """

    match = re.search(
        r"htm_data/(\d{2})(\d{2})/",
        post_url
    )

    if match:

        year2 = int(
            match.group(1)
        )

        url_month = int(
            match.group(2)
        )

        if (
            1 <= url_month <= 12
        ):

            return (
                2000 + year2
            )

    now = datetime.now(
        SITE_TZ
    )

    year = now.year

    if fallback_month > now.month:
        year -= 1

    return year


def parse_publish_time(
    soup,
    post_url
):

    text = soup.get_text(
        " ",
        strip=True
    )

    patterns = [
        (
            r"Posted:\s*"
            r"(\d{2})-(\d{2})\s+"
            r"(\d{2}):(\d{2})"
        ),
        (
            r"發表於[:：]?\s*"
            r"(\d{2})-(\d{2})\s+"
            r"(\d{2}):(\d{2})"
        )
    ]

    match = None

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.I
        )

        if match:
            break

    if not match:
        return None

    month, day, hour, minute = map(
        int,
        match.groups()
    )

    year = infer_year_from_url(
        post_url,
        month
    )

    try:

        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=SITE_TZ
        )

    except ValueError:

        return None


# =========================================================
# 正文
# =========================================================

def clean_content(
    node,
    post_url
):

    if not node:
        return ""

    for bad in node.find_all([
        "script",
        "style",
        "iframe",
        "noscript"
    ]):

        bad.decompose()

    for img in node.find_all(
        "img"
    ):

        src = img.get(
            "src"
        )

        if src:

            img["src"] = urljoin(
                post_url,
                src
            )

        for attr in [
            "onclick",
            "onload"
        ]:

            img.attrs.pop(
                attr,
                None
            )

    for a in node.find_all(
        "a",
        href=True
    ):

        a["href"] = urljoin(
            post_url,
            a["href"]
        )

    return str(node)


def find_main_content(
    soup,
    post_url
):

    # 优先找楼主正文
    for element_id in [
        "read_tpc",
        "read_tpc_0"
    ]:

        node = soup.find(
            id=element_id
        )

        if node:

            return clean_content(
                node,
                post_url
            )

    selectors = [
        ".tpc_content",
        ".post_content",
        ".post-content"
    ]

    for selector in selectors:

        node = soup.select_one(
            selector
        )

        if node:

            return clean_content(
                node,
                post_url
            )

    return ""


# =========================================================
# 单篇帖子详情
# =========================================================

def fetch_post_detail(post):

    try:

        soup = get_soup(
            post["url"]
        )

        published = (
            parse_publish_time(
                soup,
                post["url"]
            )
        )

        content = (
            find_main_content(
                soup,
                post["url"]
            )
        )

        if not content:

            content = (
                "<p>"
                "正文未能自动提取，"
                "请打开原帖查看。"
                "</p>"
            )

        return {
            "title": post["title"],
            "url": post["url"],

            "published": (
                published.isoformat()
                if published
                else None
            ),

            "content": content,

            # 页面请求本身成功
            "ok": True
        }

    except Exception as e:

        print(
            f"⚠️ 帖子详情失败："
            f"{post['title']}：{e}"
        )

        return {
            "title": post["title"],
            "url": post["url"],
            "published": None,
            "content": "",
            "ok": False
        }


# =========================================================
# 增量更新
# =========================================================

def update_cache(
    list_posts,
    cache
):

    # 先刷新已有项目的标题
    for post in list_posts:

        url = post["url"]

        if url in cache:

            cache[url]["title"] = (
                post["title"]
            )

    needs_fetch = []

    for post in list_posts:

        url = post["url"]

        # 从没抓过
        if url not in cache:

            needs_fetch.append(
                post
            )

            continue

        # 以前详情抓取失败，重新尝试
        if not cache[url].get(
            "ok",
            False
        ):

            needs_fetch.append(
                post
            )

    print()
    print(
        "=" * 55
    )

    print(
        f"缓存中已有："
        f"{len(cache)} 篇"
    )

    print(
        f"本次列表发现："
        f"{len(list_posts)} 篇"
    )

    print(
        f"需要进入详情页抓取："
        f"{len(needs_fetch)} 篇"
    )

    print(
        "=" * 55
    )

    # -------------------------------
    # 第一次：
    # needs_fetch 会是几千篇
    #
    # 以后：
    # 一般只会是 0、1、2……
    # -------------------------------

    total = len(
        needs_fetch
    )

    for index, post in enumerate(
        needs_fetch,
        start=1
    ):

        print()
        print(
            f"[详情 {index}/{total}] "
            f"{post['title']}"
        )

        detail = (
            fetch_post_detail(
                post
            )
        )

        cache[
            post["url"]
        ] = detail

        # 每抓 20 篇在本地保存一次
        # 防止脚本后面出错时内存中的进度全部丢失
        if index % 20 == 0:

            save_cache(
                cache
            )

        time.sleep(
            POST_DELAY
        )

    save_cache(
        cache
    )

    return cache


# =========================================================
# RSS
# =========================================================

def parse_cached_datetime(
    value
):

    if not value:
        return None

    try:

        return datetime.fromisoformat(
            value
        )

    except Exception:

        return None


def generate_rss(cache):

    items = list(
        cache.values()
    )

    minimum_time = datetime(
        1970,
        1,
        1,
        tzinfo=SITE_TZ
    )

    items.sort(
        key=lambda item: (
            parse_cached_datetime(
                item.get(
                    "published"
                )
            )
            or minimum_time
        ),
        reverse=True
    )

    if RSS_MAX_ITEMS:

        items = items[
            :RSS_MAX_ITEMS
        ]

    fg = FeedGenerator()

    fg.id(
        LIST_URL
    )

    fg.title(
        f"{TARGET_AUTHOR} - T66Y RSS"
    )

    fg.link(
        href=LIST_URL,
        rel="alternate"
    )

    fg.description(
        f"{TARGET_AUTHOR} 的 RSS，"
        "包含楼主正文并按真实发布时间排序。"
    )

    fg.language(
        "zh-CN"
    )

    fg.lastBuildDate(
        datetime.now(
            SITE_TZ
        )
    )

    added = 0

    for post in items:

        # 连详情页都没抓成功的，
        # 暂时不放进 RSS。
        if not post.get(
            "ok",
            False
        ):
            continue

        fe = fg.add_entry()

        fe.id(
            post["url"]
        )

        fe.guid(
            post["url"],
            permalink=True
        )

        fe.title(
            post["title"]
        )

        fe.link(
            href=post["url"],
            rel="alternate"
        )

        fe.author({
            "name": TARGET_AUTHOR
        })

        published = (
            parse_cached_datetime(
                post.get(
                    "published"
                )
            )
        )

        if published:

            fe.pubDate(
                published
            )

        content = post.get(
            "content",
            ""
        )

        fe.description(
            content
            +
            "<hr>"
            +
            f'<p><a href="{post["url"]}">'
            "查看原帖"
            "</a></p>"
        )

        added += 1

    fg.rss_file(
        FEED_FILE,
        pretty=True
    )

    print()
    print(
        "=" * 55
    )

    print(
        f"RSS 已生成："
        f"{added} 篇"
    )

    print(
        "=" * 55
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "开始更新 RSS"
    )

    # 1. 读取过去已经抓过的内容
    cache = load_cache()

    # 2. 每次仍然完整扫描全部 47+ 个列表页
    list_posts = (
        collect_all_list_posts()
    )

    if not list_posts:

        raise RuntimeError(
            "没有抓到任何列表内容，"
            "停止更新。"
        )

    # 3. 只有新帖子才进入详情页
    cache = update_cache(
        list_posts,
        cache
    )

    # 4. 用历史缓存 + 新增内容生成 RSS
    generate_rss(
        cache
    )


if __name__ == "__main__":
    main()
