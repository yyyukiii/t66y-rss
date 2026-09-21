import json
import os
import re
import time

from datetime import datetime, timezone, timedelta
from urllib.parse import (
    urljoin,
    urlparse,
    parse_qs,
    parse_qsl,
    urlencode,
    urlunparse,
)

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator


# =========================================================
# RSS 源
# =========================================================

SOURCES = [
    {
        "id": "asia_uncensored_original",
        "name": "亚洲无码原创区",
        "url": "https://t66y.com/thread0806.php?fid=2&search=219675",
        "min_pages": 47,
    },
    {
        "id": "western_original",
        "name": "欧美原创区",
        "url": "https://t66y.com/thread0806.php?fid=4&search=219675",
        "min_pages": 23,
    },
    {
        "id": "china_original",
        "name": "国产原创区",
        "url": "https://t66y.com/thread0806.php?fid=25&search=219675",
        "min_pages": 5,
    },
]


# =========================================================
# 基本设置
# =========================================================

TARGET_AUTHOR = "愛在黑夜"

DATA_DIR = "data"

# 三个分类共享：
# 每轮最多补抓 450 篇历史正文。
#
# 真正的新帖子不占这 450 篇额度。
GLOBAL_BACKFILL_BATCH_SIZE = 450

SITE_TZ = timezone(
    timedelta(hours=8)
)

PAGE_DELAY = 1.0
POST_DELAY = 1.0

DETAIL_PAUSE_EVERY = 50
DETAIL_PAUSE_SECONDS = 8

MAX_LIST_RETRIES = 5
MAX_DETAIL_FAILURES = 3
MAX_CONSECUTIVE_403 = 3

# None = RSS 保留全部帖子
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
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": (
        "zh-CN,zh;q=0.9,en;q=0.8"
    ),
})


class ForbiddenError(Exception):
    pass


class SourceScanError(Exception):
    pass


def get_soup(url, retries=3):

    last_error = None

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            print(
                f"请求：{url}"
            )

            response = session.get(
                url,
                timeout=30
            )

            if response.status_code == 403:

                raise ForbiddenError(
                    url
                )

            response.raise_for_status()

            if (
                not response.encoding
                or response.encoding.lower()
                == "iso-8859-1"
            ):

                response.encoding = (
                    response.apparent_encoding
                    or "utf-8"
                )

            return BeautifulSoup(
                response.text,
                "html.parser"
            )

        except ForbiddenError:
            raise

        except Exception as e:

            last_error = e

            print(
                f"请求失败 "
                f"{attempt}/{retries}："
                f"{e}"
            )

            time.sleep(
                4 * attempt
            )

    raise last_error


# =========================================================
# 文件路径
# =========================================================

def safe_name(name):

    return (
        re.sub(
            r'[\\/:*?"<>|]+',
            "_",
            name
        ).strip()
        or "feed"
    )


def source_paths(source):

    name = safe_name(
        source["name"]
    )

    folder = os.path.join(
        DATA_DIR,
        name
    )

    return {
        "dir": folder,

        "feed": os.path.join(
            folder,
            f"{name}_feed.xml"
        ),

        "cache": os.path.join(
            folder,
            f"{name}_posts_cache.json"
        ),

        "state": os.path.join(
            folder,
            f"{name}_rss_state.json"
        ),
    }


def ensure_source_dir(source):

    paths = source_paths(
        source
    )

    os.makedirs(
        paths["dir"],
        exist_ok=True
    )

    return paths


# =========================================================
# JSON
# =========================================================

def load_json(
    path,
    default
):

    if not os.path.exists(
        path
    ):

        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(
                f
            )

    except Exception as e:

        print(
            f"读取 {path} 失败：{e}"
        )

        return default


def atomic_save_json(
    path,
    data
):

    directory = os.path.dirname(
        path
    )

    if directory:

        os.makedirs(
            directory,
            exist_ok=True
        )

    temp_path = (
        path + ".tmp"
    )

    with open(
        temp_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_path,
        path
    )


# =========================================================
# 缓存兼容
# =========================================================

VALID_STATUSES = {
    "full",
    "index_only",
    "pending_new",
    "retry_new",
    "retry_history",
    "blocked",
    "failed",
}


def normalize_cache(cache):

    if not isinstance(
        cache,
        dict
    ):

        return {}

    for url, item in list(
        cache.items()
    ):

        if not isinstance(
            item,
            dict
        ):

            cache[url] = {
                "title": "",
                "url": url,
                "published": None,
                "content": "",
                "status": "index_only",
                "fail_count": 0,
            }

            continue

        item.setdefault(
            "url",
            url
        )

        item.setdefault(
            "title",
            ""
        )

        item.setdefault(
            "published",
            None
        )

        item.setdefault(
            "content",
            ""
        )

        item.setdefault(
            "fail_count",
            0
        )

        status = item.get(
            "status"
        )

        if status == "retry":

            item["status"] = (
                "retry_history"
            )

            continue

        if status in VALID_STATUSES:

            continue

        if item.get(
            "blocked"
        ):

            item["status"] = (
                "blocked"
            )

            continue

        if (
            item.get("ok")
            is True
        ):

            content = item.get(
                "content",
                ""
            )

            if (
                "历史索引" in content
                or "等待抓取" in content
                or not content
            ):

                item["status"] = (
                    "index_only"
                )

            else:

                item["status"] = (
                    "full"
                )

            continue

        if (
            item.get("ok")
            is False
        ):

            item["status"] = (
                "retry_history"
            )

            continue

        item["status"] = (
            "index_only"
        )

    return cache


def load_cache(source):

    paths = ensure_source_dir(
        source
    )

    cache = normalize_cache(
        load_json(
            paths["cache"],
            {}
        )
    )

    print(
        f"[{source['name']}] "
        f"读取缓存："
        f"{len(cache)} 篇"
    )

    return cache


def save_cache(
    source,
    cache
):

    paths = ensure_source_dir(
        source
    )

    atomic_save_json(
        paths["cache"],
        cache
    )

    print(
        f"[{source['name']}] "
        f"缓存已保存："
        f"{len(cache)} 篇"
    )


# =========================================================
# 状态
# =========================================================

def load_state(source):

    paths = ensure_source_dir(
        source
    )

    state = load_json(
        paths["state"],
        {
            "initialized": False
        }
    )

    if not isinstance(
        state,
        dict
    ):

        state = {
            "initialized": False
        }

    state.setdefault(
        "initialized",
        False
    )

    return state


def save_state(
    source,
    state
):

    paths = ensure_source_dir(
        source
    )

    atomic_save_json(
        paths["state"],
        state
    )


# =========================================================
# 分页 URL
# =========================================================

def make_page_url(
    source,
    page
):

    parsed = urlparse(
        source["url"]
    )

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True
        )
    )

    if page <= 1:

        query.pop(
            "page",
            None
        )

    else:

        query["page"] = str(
            page
        )

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


# =========================================================
# 判断分页是否属于当前分类
# =========================================================

def same_source_query(
    source,
    href
):

    source_parsed = urlparse(
        source["url"]
    )

    source_query = parse_qs(
        source_parsed.query
    )

    full_url = urljoin(
        source["url"],
        href
    )

    parsed = urlparse(
        full_url
    )

    query = parse_qs(
        parsed.query
    )

    if (
        parsed.path
        != source_parsed.path
    ):

        return False

    for key in (
        "fid",
        "search"
    ):

        expected = source_query.get(
            key,
            [None]
        )[0]

        actual = query.get(
            key,
            [None]
        )[0]

        if (
            expected is not None
            and actual != expected
        ):

            return False

    return True


# =========================================================
# 自动检测总页数
# =========================================================

def get_total_pages(source):

    print(
        f"[{source['name']}] "
        "正在检测总页数……"
    )

    pages = {1}

    try:

        soup = get_soup(
            source["url"]
        )

        for a in soup.find_all(
            "a",
            href=True
        ):

            try:

                href = a["href"]

                if not same_source_query(
                    source,
                    href
                ):

                    continue

                full_url = urljoin(
                    source["url"],
                    href
                )

                query = parse_qs(
                    urlparse(
                        full_url
                    ).query
                )

                if "page" not in query:

                    continue

                page = int(
                    query["page"][0]
                )

                if page >= 1:

                    pages.add(
                        page
                    )

            except Exception:

                continue

    except Exception as e:

        print(
            f"[{source['name']}] "
            f"自动检测页数失败：{e}"
        )

    detected = max(
        pages
    )

    total = max(
        detected,
        int(
            source.get(
                "min_pages",
                1
            )
        )
    )

    print(
        f"[{source['name']}] "
        f"网页检测：{detected} 页；"
        f"本次扫描：1～{total} 页"
    )

    return total


# =========================================================
# 单个列表页
# =========================================================

def get_posts_from_page(
    source,
    page
):

    soup = get_soup(
        make_page_url(
            source,
            page
        )
    )

    posts = []

    seen = set()

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
            source["url"],
            href
        )

        if url in seen:

            continue

        seen.add(
            url
        )

        posts.append({
            "title": title,
            "url": url,
        })

    return posts


# =========================================================
# 列表页请求
# =========================================================

def fetch_list_page(
    source,
    page,
    previous_signature=None,
    allow_empty=False,
):

    for attempt in range(
        1,
        MAX_LIST_RETRIES + 1
    ):

        try:

            posts = (
                get_posts_from_page(
                    source,
                    page
                )
            )

            if not posts:

                # 最后一页允许是空尾页
                if allow_empty:

                    print(
                        f"[{source['name']}] "
                        f"第 {page} 页为空，"
                        "这是最后一页，"
                        "视为正常空尾页。"
                    )

                    return (
                        [],
                        ()
                    )

                print(
                    f"[{source['name']}] "
                    f"第 {page} 页返回 0 条，"
                    f"重试 {attempt}/"
                    f"{MAX_LIST_RETRIES}"
                )

                time.sleep(
                    attempt * 5
                )

                continue

            signature = tuple(
                post["url"]
                for post in posts
            )

            if (
                previous_signature
                and signature
                == previous_signature
            ):

                print(
                    f"[{source['name']}] "
                    f"第 {page} 页疑似重复上一页，"
                    f"重试 {attempt}/"
                    f"{MAX_LIST_RETRIES}"
                )

                time.sleep(
                    attempt * 5
                )

                continue

            return (
                posts,
                signature
            )

        except ForbiddenError:

            print(
                f"[{source['name']}] "
                f"第 {page} 个列表页返回 403"
            )

            return (
                None,
                None
            )

        except Exception as e:

            print(
                f"[{source['name']}] "
                f"第 {page} 页失败 "
                f"{attempt}/"
                f"{MAX_LIST_RETRIES}："
                f"{e}"
            )

            time.sleep(
                attempt * 5
            )

    return (
        None,
        None
    )


# =========================================================
# 扫描某分类全部分页
# =========================================================

def collect_all_list_posts(source):

    total_pages = (
        get_total_pages(
            source
        )
    )

    all_posts = []

    seen_urls = set()

    failed_pages = []

    previous_signature = None

    for page in range(
        1,
        total_pages + 1
    ):

        print(
            f"[{source['name']}] "
            f"正在扫描 "
            f"{page}/{total_pages} 页"
        )

        posts, signature = (
            fetch_list_page(
                source,
                page,
                previous_signature,
                allow_empty=(
                    page == total_pages
                ),
            )
        )

        # None = 真失败
        if posts is None:

            failed_pages.append(
                page
            )

            print(
                f"[{source['name']}] "
                f"第 {page} 页第一次失败，"
                "先继续后面的页面"
            )

            continue

        # [] = 正常空尾页
        if len(posts) == 0:

            print(
                f"[{source['name']}] "
                f"第 {page}/{total_pages} 页"
                "是空尾页，不计为失败。"
            )

            continue

        previous_signature = (
            signature
        )

        added = 0

        for post in posts:

            if (
                post["url"]
                in seen_urls
            ):

                continue

            seen_urls.add(
                post["url"]
            )

            all_posts.append(
                post
            )

            added += 1

        print(
            f"[{source['name']}] "
            f"本页 {len(posts)} 条；"
            f"新增 {added}；"
            f"累计 {len(all_posts)}"
        )

        time.sleep(
            PAGE_DELAY
        )

    # =====================================================
    # 第二轮补抓真正失败的页面
    # =====================================================

    if failed_pages:

        print(
            f"[{source['name']}] "
            f"开始补抓失败页面："
            f"{failed_pages}"
        )

        time.sleep(15)

        still_failed = []

        for page in failed_pages:

            posts, _ = (
                fetch_list_page(
                    source,
                    page,
                    None,
                    allow_empty=(
                        page == total_pages
                    ),
                )
            )

            if posts is None:

                still_failed.append(
                    page
                )

                continue

            if len(posts) == 0:

                print(
                    f"[{source['name']}] "
                    f"第 {page} 页为空尾页，"
                    "补抓阶段视为成功。"
                )

                continue

            for post in posts:

                if (
                    post["url"]
                    not in seen_urls
                ):

                    seen_urls.add(
                        post["url"]
                    )

                    all_posts.append(
                        post
                    )

            time.sleep(
                PAGE_DELAY
            )

        if still_failed:

            raise SourceScanError(
                f"[{source['name']}] "
                "以下页面两轮重试后仍失败："
                f"{still_failed}"
            )

    print(
        f"[{source['name']}] "
        f"扫描完成，共 "
        f"{len(all_posts)} 个"
        "不重复帖子"
    )

    return (
        all_posts,
        total_pages
    )


# =========================================================
# 从 URL 推断年份
# =========================================================

def infer_year_from_url(
    post_url,
    fallback_month
):

    match = re.search(
        r"htm_data/"
        r"(\d{2})(\d{2})/",
        post_url
    )

    if match:

        yy = int(
            match.group(1)
        )

        mm = int(
            match.group(2)
        )

        if 1 <= mm <= 12:

            return (
                2000 + yy
            )

    now = datetime.now(
        SITE_TZ
    )

    if fallback_month > now.month:

        return (
            now.year - 1
        )

    return now.year


# =========================================================
# 真实发布时间
# =========================================================

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
        ),
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

    if node is None:

        return ""

    for bad in node.find_all(
        [
            "script",
            "style",
            "iframe",
            "noscript",
        ]
    ):

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

        img.attrs.pop(
            "onclick",
            None
        )

        img.attrs.pop(
            "onload",
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

    for element_id in (
        "read_tpc",
        "read_tpc_0",
    ):

        node = soup.find(
            id=element_id
        )

        if node:

            return clean_content(
                node,
                post_url
            )

    for selector in (
        ".tpc_content",
        ".post_content",
        ".post-content",
    ):

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
# 单篇详情
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
                "正文没有成功自动提取，"
                "请打开原帖查看。"
                "</p>"
            )

        return {
            "type": "success",

            "published": (
                published.isoformat()
                if published
                else None
            ),

            "content": content,
        }

    except ForbiddenError:

        print(
            f"⛔ 详情页返回 403："
            f"{post['url']}"
        )

        return {
            "type": "403",
            "published": None,
            "content": "",
        }

    except Exception as e:

        print(
            f"⚠️ 详情页异常："
            f"{post['url']}：{e}"
        )

        return {
            "type": "error",
            "published": None,
            "content": "",
        }


# =========================================================
# 历史索引
# =========================================================

def make_index_item(post):

    return {
        "title": post["title"],
        "url": post["url"],

        "published": None,

        "content": (
            "<p>"
            "历史帖子正文尚未抓取。"
            "</p>"
        ),

        "status": "index_only",

        "fail_count": 0,
    }


# =========================================================
# 列表同步进缓存
# =========================================================

def sync_list_to_cache(
    list_posts,
    cache,
    baseline_mode
):

    new_count = 0

    for post in list_posts:

        url = post["url"]

        if url in cache:

            cache[url][
                "title"
            ] = post["title"]

            cache[url][
                "url"
            ] = url

            continue

        # 第一次初始化
        if baseline_mode:

            cache[url] = (
                make_index_item(
                    post
                )
            )

            continue

        # 真正的新帖子
        cache[url] = {
            "title": post["title"],
            "url": url,

            "published": None,

            "content": (
                "<p>"
                "新帖正文等待抓取。"
                "</p>"
            ),

            "status": "pending_new",

            "fail_count": 0,
        }

        new_count += 1

    return new_count


# =========================================================
# 写入详情结果
# =========================================================

def apply_detail_result(
    post,
    result,
    cache,
    is_new
):

    url = post["url"]

    old_item = cache.get(
        url,
        {}
    )

    old_published = (
        old_item.get(
            "published"
        )
    )

    old_content = (
        old_item.get(
            "content",
            ""
        )
    )

    # 成功
    if (
        result["type"]
        == "success"
    ):

        cache[url] = {
            "title": post["title"],
            "url": url,

            "published": (
                result.get(
                    "published"
                )
                or old_published
            ),

            "content": (
                result.get(
                    "content"
                )
                or old_content
            ),

            "status": "full",

            "fail_count": 0,
        }

        return "success"

    # 403
    if (
        result["type"]
        == "403"
    ):

        cache[url] = {
            "title": post["title"],
            "url": url,

            "published": (
                old_published
            ),

            "content": (
                "<p>"
                "该详情页返回 403，"
                "目前只保留标题和原帖链接。"
                "</p>"
            ),

            "status": "blocked",

            "fail_count": (
                old_item.get(
                    "fail_count",
                    0
                )
            ),
        }

        return "403"

    # 普通错误
    fail_count = (
        old_item.get(
            "fail_count",
            0
        )
        + 1
    )

    if (
        fail_count
        >= MAX_DETAIL_FAILURES
    ):

        status = "failed"

    else:

        status = (
            "retry_new"
            if is_new
            else "retry_history"
        )

    cache[url] = {
        "title": post["title"],
        "url": url,

        "published": (
            old_published
        ),

        "content": (
            old_content
            or
            "<p>"
            "正文暂时读取失败。"
            "</p>"
        ),

        "status": status,

        "fail_count": (
            fail_count
        ),
    }

    return "error"


# =========================================================
# 节流
# =========================================================

def detail_pause(index):

    time.sleep(
        POST_DELAY
    )

    if (
        index > 0
        and index
        % DETAIL_PAUSE_EVERY
        == 0
    ):

        print(
            f"已处理 {index} 篇详情，"
            f"额外休息 "
            f"{DETAIL_PAUSE_SECONDS} 秒"
        )

        time.sleep(
            DETAIL_PAUSE_SECONDS
        )


# =========================================================
# 初始化分类
# =========================================================

def prepare_source(
    source,
    list_posts,
    total_pages
):

    cache = load_cache(
        source
    )

    state = load_state(
        source
    )

    initialized = bool(
        state.get(
            "initialized",
            False
        )
    )

    # 第一次运行：
    # 当前已有内容全部作为历史基准
    baseline_mode = (
        not initialized
        or not cache
    )

    new_count = (
        sync_list_to_cache(
            list_posts,
            cache,
            baseline_mode
        )
    )

    state.update({
        "initialized": True,

        "last_scan": (
            datetime.now(
                SITE_TZ
            ).isoformat()
        ),

        "last_total_pages": (
            total_pages
        ),

        "last_list_count": (
            len(list_posts)
        ),
    })

    save_cache(
        source,
        cache
    )

    save_state(
        source,
        state
    )

    print(
        f"[{source['name']}] "
        f"列表 {len(list_posts)}；"
        f"缓存 {len(cache)}；"
        f"基准模式 {baseline_mode}；"
        f"真正新增 {new_count}"
    )

    return {
        "source": source,
        "list_posts": list_posts,
        "cache": cache,
    }


# =========================================================
# 第一优先级：
# 所有新帖子
# =========================================================

def process_new_posts(context):

    source = context[
        "source"
    ]

    posts = context[
        "list_posts"
    ]

    cache = context[
        "cache"
    ]

    tasks = [
        post
        for post in posts
        if cache.get(
            post["url"],
            {}
        ).get(
            "status"
        )
        in {
            "pending_new",
            "retry_new",
        }
    ]

    print(
        f"[{source['name']}] "
        f"优先处理新帖："
        f"{len(tasks)} 篇"
    )

    consecutive_403 = 0

    total = len(
        tasks
    )

    for index, post in enumerate(
        tasks,
        start=1
    ):

        print(
            f"[{source['name']}] "
            f"[新帖 "
            f"{index}/{total}] "
            f"{post['title']}"
        )

        result = (
            fetch_post_detail(
                post
            )
        )

        outcome = (
            apply_detail_result(
                post,
                result,
                cache,
                True
            )
        )

        if outcome == "403":

            consecutive_403 += 1

        else:

            consecutive_403 = 0

        if (
            index % 10
            == 0
        ):

            save_cache(
                source,
                cache
            )

        if (
            consecutive_403
            >= MAX_CONSECUTIVE_403
        ):

            print(
                f"[{source['name']}] "
                "连续多个 403，"
                "本轮停止详情抓取。"
            )

            save_cache(
                source,
                cache
            )

            return True

        detail_pause(
            index
        )

    save_cache(
        source,
        cache
    )

    return False


# =========================================================
# RSS 时间解析
# =========================================================

def parse_cached_datetime(value):

    if not value:

        return None

    try:

        dt = datetime.fromisoformat(
            value
        )

        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=SITE_TZ
            )

        return dt

    except Exception:

        return None


# =========================================================
# 历史补抓优先级
#
# 越新的帖子越优先
# =========================================================

def history_priority(
    post,
    item,
    position
):

    # 如果已经有真实时间
    published = (
        parse_cached_datetime(
            item.get(
                "published"
            )
        )
    )

    if published:

        return (
            3,
            published.timestamp(),
            0,
            -position,
        )

    # 从 URL 中读取 YYMM
    match = re.search(
        r"htm_data/"
        r"(\d{2})(\d{2})/",
        post["url"]
    )

    year = 0
    month = 0

    if match:

        year = (
            2000
            + int(
                match.group(1)
            )
        )

        month = int(
            match.group(2)
        )

    rough_timestamp = 0

    if (
        year > 0
        and 1 <= month <= 12
    ):

        try:

            rough_timestamp = (
                datetime(
                    year,
                    month,
                    1,
                    tzinfo=SITE_TZ
                ).timestamp()
            )

        except Exception:

            rough_timestamp = 0

    # URL 最后的帖子 ID
    post_id = 0

    id_match = re.search(
        r"/(\d+)\.html(?:$|\?)",
        post["url"]
    )

    if id_match:

        try:

            post_id = int(
                id_match.group(1)
            )

        except Exception:

            post_id = 0

    return (
        2 if rough_timestamp else 1,
        rough_timestamp,
        post_id,
        -position,
    )


# =========================================================
# 全局历史补抓
#
# 三个分类共享 450 篇额度
#
# 自动动态分配
#
# 越新的历史帖子越优先
# =========================================================

def process_global_history_backfill(
    contexts
):

    candidates = []

    # 收集三个分类全部待补历史
    for context in contexts:

        source = context[
            "source"
        ]

        posts = context[
            "list_posts"
        ]

        cache = context[
            "cache"
        ]

        for position, post in enumerate(
            posts
        ):

            item = cache.get(
                post["url"],
                {}
            )

            status = item.get(
                "status",
                "index_only"
            )

            if status not in {
                "index_only",
                "retry_history",
            }:

                continue

            candidates.append({
                "source": source,
                "context": context,
                "post": post,

                "priority": (
                    history_priority(
                        post,
                        item,
                        position
                    )
                ),
            })

    # 最新 -> 最旧
    candidates.sort(
        key=lambda item: (
            item["priority"]
        ),
        reverse=True
    )

    tasks = candidates[
        :GLOBAL_BACKFILL_BATCH_SIZE
    ]

    print()
    print(
        "=" * 70
    )

    print(
        f"三个分类历史待补总数："
        f"{len(candidates)} 篇"
    )

    print(
        f"本轮全局历史补抓："
        f"{len(tasks)} / "
        f"{GLOBAL_BACKFILL_BATCH_SIZE} 篇"
    )

    # 显示动态分配
    allocation = {}

    for task in tasks:

        name = task[
            "source"
        ]["name"]

        allocation[name] = (
            allocation.get(
                name,
                0
            )
            + 1
        )

    for context in contexts:

        name = context[
            "source"
        ]["name"]

        print(
            f"{name}："
            f"{allocation.get(name, 0)} 篇"
        )

    print(
        "=" * 70
    )

    consecutive_403 = 0

    source_counts = {}

    total = len(
        tasks
    )

    for index, task in enumerate(
        tasks,
        start=1
    ):

        source = task[
            "source"
        ]

        context = task[
            "context"
        ]

        post = task[
            "post"
        ]

        cache = context[
            "cache"
        ]

        name = source[
            "name"
        ]

        print(
            f"[全局历史 "
            f"{index}/{total}] "
            f"[{name}] "
            f"{post['title']}"
        )

        result = (
            fetch_post_detail(
                post
            )
        )

        outcome = (
            apply_detail_result(
                post,
                result,
                cache,
                False
            )
        )

        source_counts[name] = (
            source_counts.get(
                name,
                0
            )
            + 1
        )

        if outcome == "403":

            consecutive_403 += 1

        else:

            consecutive_403 = 0

        if (
            source_counts[name]
            % 20
            == 0
        ):

            save_cache(
                source,
                cache
            )

        if (
            consecutive_403
            >= MAX_CONSECUTIVE_403
        ):

            print()
            print(
                "⚠️ 连续出现多个 403。"
            )

            print(
                "本轮全局历史补抓提前停止，"
                "剩余内容下次继续。"
            )

            break

        detail_pause(
            index
        )

    # 最后保存三个分类
    for context in contexts:

        save_cache(
            context["source"],
            context["cache"]
        )

    return (
        consecutive_403
        >= MAX_CONSECUTIVE_403
    )


# =========================================================
# 生成 RSS
# =========================================================

def generate_rss(context):

    source = context[
        "source"
    ]

    cache = context[
        "cache"
    ]

    paths = ensure_source_dir(
        source
    )

    items = list(
        cache.values()
    )

    minimum_time = datetime(
        1970,
        1,
        1,
        tzinfo=SITE_TZ
    )

    # 按真实发布时间倒序
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

    if (
        RSS_MAX_ITEMS
        is not None
    ):

        items = items[
            :RSS_MAX_ITEMS
        ]

    fg = FeedGenerator()

    fg.id(
        source["url"]
    )

    fg.title(
        f"{source['name']} "
        f"- {TARGET_AUTHOR}"
    )

    fg.link(
        href=source["url"],
        rel="alternate"
    )

    fg.description(
        f"{source['name']}："
        f"{TARGET_AUTHOR} 的帖子 RSS。"
        "新帖优先抓取全文，"
        "历史帖子按最新优先动态补抓。"
    )

    fg.language(
        "zh-CN"
    )

    fg.lastBuildDate(
        datetime.now(
            SITE_TZ
        )
    )

    for post in items:

        url = post.get(
            "url"
        )

        if not url:

            continue

        fe = fg.add_entry()

        fe.id(
            url
        )

        fe.guid(
            url,
            permalink=True
        )

        fe.title(
            post.get(
                "title"
            )
            or "无标题"
        )

        fe.link(
            href=url,
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

        status = post.get(
            "status",
            "index_only"
        )

        status_text = {
            "index_only": (
                "<p><small>"
                "历史正文尚未补抓"
                "</small></p>"
            ),

            "pending_new": (
                "<p><small>"
                "新帖正文等待抓取"
                "</small></p>"
            ),

            "retry_new": (
                "<p><small>"
                "新帖正文等待重试"
                "</small></p>"
            ),

            "retry_history": (
                "<p><small>"
                "历史正文将在后续任务中重试"
                "</small></p>"
            ),

            "blocked": (
                "<p><small>"
                "详情页返回 403，"
                "目前只保留标题和原帖链接"
                "</small></p>"
            ),

            "failed": (
                "<p><small>"
                "详情页多次读取失败，"
                "目前只保留标题和原帖链接"
                "</small></p>"
            ),
        }.get(
            status,
            ""
        )

        content = (
            post.get(
                "content"
            )
            or
            "<p>"
            "暂时没有可显示的正文。"
            "</p>"
        )

        fe.description(
            status_text
            +
            content
            +
            "<hr>"
            +
            f'<p>'
            f'<a href="{url}">'
            f'查看原帖'
            f'</a>'
            f'</p>'
        )

    fg.rss_file(
        paths["feed"],
        pretty=True
    )

    print(
        f"[{source['name']}] "
        f"已生成："
        f"{paths['feed']}"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "开始更新多分类 RSS"
    )

    # 自动创建三个分类目录
    for source in SOURCES:

        ensure_source_dir(
            source
        )

    contexts = []

    # =====================================================
    # 第一阶段：
    # 先扫描三个分类全部页面
    # =====================================================

    for source in SOURCES:

        print()
        print(
            "#" * 70
        )

        print(
            f"扫描分类："
            f"{source['name']}"
        )

        print(
            "#" * 70
        )

        try:

            posts, total_pages = (
                collect_all_list_posts(
                    source
                )
            )

            context = (
                prepare_source(
                    source,
                    posts,
                    total_pages
                )
            )

            contexts.append(
                context
            )

        except Exception as e:

            print(
                f"❌ [{source['name']}] "
                f"本轮扫描失败，"
                f"保留旧数据：{e}"
            )

    if not contexts:

        raise RuntimeError(
            "三个分类本轮都扫描失败。"
        )

    # =====================================================
    # 第二阶段：
    # 三个分类所有新帖优先
    # =====================================================

    site_limited = False

    for context in contexts:

        limited = (
            process_new_posts(
                context
            )
        )

        if limited:

            site_limited = True

            break

    # =====================================================
    # 第三阶段：
    #
    # 三个分类共享 450 篇历史补抓额度
    # 自动动态分配
    # 最新优先
    # =====================================================

    if not site_limited:

        process_global_history_backfill(
            contexts
        )

    else:

        print(
            "检测到网站可能正在限流，"
            "本轮跳过历史补抓。"
        )

    # =====================================================
    # 第四阶段：
    # 分别生成三个 RSS
    # =====================================================

    for context in contexts:

        generate_rss(
            context
        )

    print()
    print(
        "全部分类处理完成。"
    )


if __name__ == "__main__":

    main()
