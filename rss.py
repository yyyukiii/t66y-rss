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
# 基本设置
# =========================================================

BASE_URL = "https://t66y.com/"

LIST_URL = (
    "https://t66y.com/thread0806.php"
    "?fid=2&search=219675"
)

TARGET_AUTHOR = "愛在黑夜"

CACHE_FILE = "posts_cache.json"
STATE_FILE = "rss_state.json"
FEED_FILE = "feed.xml"

# 已经确认目前至少有 47 页。
# 如果以后网站显示 48、49……会自动取更大的页数。
MIN_TOTAL_PAGES = 47

# 每次运行：
# 新帖全部优先抓取，不占这 400 篇额度。
# 新帖处理完成后，再补 400 篇历史正文。
BACKFILL_BATCH_SIZE = 400

# 网站时间按 UTC+8
SITE_TZ = timezone(
    timedelta(hours=8)
)

# 列表页请求间隔
PAGE_DELAY = 1.0

# 详情页请求间隔
POST_DELAY = 1.0

# 每处理多少篇详情页额外休息一下
DETAIL_PAUSE_EVERY = 50

# 额外休息秒数
DETAIL_PAUSE_SECONDS = 8

# 列表页最多重试次数
MAX_LIST_RETRIES = 5

# 普通详情页错误最多跨运行尝试次数
MAX_DETAIL_FAILURES = 3

# 如果连续出现多个 403，
# 认为网站可能正在限流，本轮停止详情抓取。
MAX_CONSECUTIVE_403 = 3

# RSS 保留数量
# None = 全部保留
RSS_MAX_ITEMS = None


# =========================================================
# HTTP Session
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


# =========================================================
# HTTP 请求
# =========================================================

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

            # 403 不在同一次请求中反复撞网站
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
                f"{attempt}/{retries}：{e}"
            )

            time.sleep(
                4 * attempt
            )

    raise last_error


# =========================================================
# 状态文件
# =========================================================

def load_state():

    if not os.path.exists(
        STATE_FILE
    ):

        return {
            "initialized": False
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(
            data,
            dict
        ):

            return {
                "initialized": False
            }

        return data

    except Exception:

        return {
            "initialized": False
        }


def save_state(state):

    temp_file = (
        STATE_FILE + ".tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_file,
        STATE_FILE
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
    """
    兼容前面几版生成过的 posts_cache.json。
    """

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
                "fail_count": 0
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

        # 旧版 retry
        if status == "retry":

            item["status"] = (
                "retry_history"
            )

            continue

        if status in VALID_STATUSES:
            continue

        # 兼容以前的 blocked 字段
        if item.get(
            "blocked"
        ):

            item["status"] = (
                "blocked"
            )

            continue

        # 兼容以前的 ok 字段
        if item.get(
            "ok"
        ) is True:

            content = item.get(
                "content",
                ""
            )

            # 判断是不是旧版历史占位内容
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

        if item.get(
            "ok"
        ) is False:

            item["status"] = (
                "retry_history"
            )

            continue

        item["status"] = (
            "index_only"
        )

    return cache


def load_cache():

    if not os.path.exists(
        CACHE_FILE
    ):

        print()
        print(
            "没有历史缓存。"
        )

        return {}

    try:

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            cache = json.load(f)

        cache = normalize_cache(
            cache
        )

        print()
        print(
            f"读取历史缓存："
            f"{len(cache)} 篇"
        )

        return cache

    except Exception as e:

        print(
            f"缓存读取失败：{e}"
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

    return (
        "https://t66y.com/thread0806.php"
        f"?fid=2&page={page}&search=219675"
    )


def get_total_pages():

    print()
    print(
        "正在检测总页数……"
    )

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

                parsed = urlparse(
                    full_url
                )

                query = parse_qs(
                    parsed.query
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

                    pages.add(
                        page
                    )

            except Exception:
                continue

    except Exception as e:

        print(
            f"自动检测页数失败：{e}"
        )

    detected = max(
        pages
    )

    total = max(
        detected,
        MIN_TOTAL_PAGES
    )

    print(
        f"网页检测页数：{detected}"
    )

    print(
        f"本次完整扫描："
        f"1～{total} 页"
    )

    return total


# =========================================================
# 单个列表页
# =========================================================

def get_posts_from_page(page):

    soup = get_soup(
        make_page_url(
            page
        )
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

        # 只处理主题详情链接
        if "htm_data" not in href:
            continue

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        full_url = urljoin(
            BASE_URL,
            href
        )

        if full_url in page_seen:
            continue

        page_seen.add(
            full_url
        )

        posts.append({
            "title": title,
            "url": full_url
        })

    return posts


def fetch_list_page(
    page,
    previous_signature=None
):

    for attempt in range(
        1,
        MAX_LIST_RETRIES + 1
    ):

        try:

            posts = (
                get_posts_from_page(
                    page
                )
            )

            if not posts:

                print(
                    f"⚠️ 第 {page} 页"
                    "返回 0 条，"
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

            # 防止网站异常返回上一页
            if (
                previous_signature
                and signature
                == previous_signature
            ):

                print(
                    f"⚠️ 第 {page} 页"
                    "疑似重复上一页，"
                    "重新请求。"
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
                f"⛔ 第 {page} 个"
                "列表页返回 403"
            )

            return (
                None,
                None
            )

        except Exception as e:

            print(
                f"⚠️ 第 {page} 页失败 "
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
# 完整扫描所有列表页
# =========================================================

def collect_all_list_posts():

    total_pages = (
        get_total_pages()
    )

    all_posts = []

    seen_urls = set()

    failed_pages = []

    previous_signature = None

    for page in range(
        1,
        total_pages + 1
    ):

        print()
        print(
            "=" * 60
        )

        print(
            f"正在扫描 "
            f"{page}/{total_pages} 页"
        )

        print(
            "=" * 60
        )

        posts, signature = (
            fetch_list_page(
                page,
                previous_signature
            )
        )

        if not posts:

            print(
                f"❌ 第 {page} 页"
                "第一次扫描失败。"
            )

            print(
                "先继续扫描后面的页面。"
            )

            failed_pages.append(
                page
            )

            continue

        previous_signature = (
            signature
        )

        added = 0

        for post in posts:

            url = post["url"]

            if url in seen_urls:
                continue

            seen_urls.add(
                url
            )

            all_posts.append(
                post
            )

            added += 1

        print(
            f"第 {page}/{total_pages} 页："
            f"{len(posts)} 条"
        )

        print(
            f"新增 {added} 条，"
            f"累计 {len(all_posts)} 条"
        )

        time.sleep(
            PAGE_DELAY
        )

    # =====================================================
    # 第二轮补抓失败页
    # =====================================================

    if failed_pages:

        print()
        print(
            "=" * 60
        )

        print(
            "开始补抓失败页面："
        )

        print(
            failed_pages
        )

        print(
            "=" * 60
        )

        time.sleep(15)

        still_failed = []

        for page in failed_pages:

            posts, _ = (
                fetch_list_page(
                    page,
                    None
                )
            )

            if not posts:

                still_failed.append(
                    page
                )

                continue

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
                f"✓ 第 {page} 页"
                f"补抓成功，"
                f"新增 {added} 条"
            )

            time.sleep(
                PAGE_DELAY
            )

        # 有页面彻底失败就不更新 RSS
        if still_failed:

            raise RuntimeError(
                "以下列表页经过两轮重试"
                "仍然无法读取："
                f"{still_failed}。"
                "为了避免生成残缺 RSS，"
                "本次停止更新。"
            )

    print()
    print(
        "=" * 60
    )

    print(
        f"全部 {total_pages} 页"
        "扫描完成"
    )

    print(
        f"共发现 "
        f"{len(all_posts)} 个"
        "不重复帖子"
    )

    print(
        "=" * 60
    )

    return all_posts


# =========================================================
# 发布时间
# =========================================================

def infer_year_from_url(
    post_url,
    fallback_month
):

    # 示例：
    #
    # /htm_data/2403/2/6236255.html
    #
    # 2403 = 2024 年 03 月

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
# 正文清理
# =========================================================

def clean_content(
    node,
    post_url
):

    if node is None:

        return ""

    for bad in node.find_all([
        "script",
        "style",
        "iframe",
        "noscript"
    ]):

        bad.decompose()

    # 图片地址改成绝对地址
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

    # 正文里的超链接也改成绝对地址
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

    # 这种老论坛常见楼主正文 ID
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

            "content": content
        }

    except ForbiddenError:

        print(
            "⛔ 详情页返回 403："
        )

        print(
            post["url"]
        )

        return {
            "type": "403",
            "published": None,
            "content": ""
        }

    except Exception as e:

        print(
            f"⚠️ 详情页异常：{e}"
        )

        return {
            "type": "error",
            "published": None,
            "content": ""
        }


# =========================================================
# 建立历史索引
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

        "fail_count": 0
    }


# =========================================================
# 同步列表 -> 缓存
# =========================================================

def sync_list_to_cache(
    list_posts,
    cache,
    baseline_mode
):

    new_count = 0

    for post in list_posts:

        url = post["url"]

        # 已经存在
        if url in cache:

            cache[url][
                "title"
            ] = post["title"]

            cache[url][
                "url"
            ] = url

            continue

        # =================================================
        # 第一次运行这一新版程序：
        #
        # 当前网站上已经存在的几千篇，
        # 全部视为历史基准，
        # 绝不能当成几千篇“新帖”。
        # =================================================

        if baseline_mode:

            cache[url] = (
                make_index_item(
                    post
                )
            )

            continue

        # =================================================
        # 初始化完成之后出现的新 URL
        # 才是真正的新帖子
        # =================================================

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

            "fail_count": 0
        }

        new_count += 1

    return new_count


# =========================================================
# 把详情结果写回缓存
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

    # =====================================================
    # 成功
    # =====================================================

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

            "fail_count": 0
        }

        return "success"

    # =====================================================
    # 403
    # =====================================================

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
                "没有继续自动访问。"
                "</p>"
            ),

            # 以后不会每 10 分钟重复撞这个 URL
            "status": "blocked",

            "fail_count": (
                old_item.get(
                    "fail_count",
                    0
                )
            )
        }

        return "403"

    # =====================================================
    # 普通临时错误
    # =====================================================

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

        if is_new:

            status = "retry_new"

        else:

            status = "retry_history"

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
        )
    }

    return "error"


# =========================================================
# 详情请求节流
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

        print()
        print(
            f"已处理 {index} 篇详情，"
            f"额外休息 "
            f"{DETAIL_PAUSE_SECONDS} 秒。"
        )

        time.sleep(
            DETAIL_PAUSE_SECONDS
        )


# =========================================================
# 优先处理新帖子
# =========================================================

def process_new_posts(
    list_posts,
    cache
):

    priority_posts = []

    for post in list_posts:

        item = cache.get(
            post["url"],
            {}
        )

        status = item.get(
            "status"
        )

        if status in {
            "pending_new",
            "retry_new"
        }:

            priority_posts.append(
                post
            )

    print()
    print(
        "=" * 60
    )

    print(
        f"本次需要优先处理的新帖："
        f"{len(priority_posts)} 篇"
    )

    print(
        "=" * 60
    )

    consecutive_403 = 0

    total = len(
        priority_posts
    )

    for index, post in enumerate(
        priority_posts,
        start=1
    ):

        print()
        print(
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
                is_new=True
            )
        )

        if outcome == "403":

            consecutive_403 += 1

        else:

            consecutive_403 = 0

        # 新帖尽量及时保存进度
        if (
            index % 10
            == 0
        ):

            save_cache(
                cache
            )

        # 连续 403 说明很可能正在限流
        if (
            consecutive_403
            >= MAX_CONSECUTIVE_403
        ):

            print()
            print(
                "⚠️ 连续出现多个 403。"
            )

            print(
                "本轮停止继续请求详情页，"
                "避免进一步触发网站限制。"
            )

            save_cache(
                cache
            )

            return True

        detail_pause(
            index
        )

    save_cache(
        cache
    )

    return False


# =========================================================
# 每轮补 400 篇历史正文
# =========================================================

def process_history_backfill(
    list_posts,
    cache
):

    pending_history = []

    for post in list_posts:

        item = cache.get(
            post["url"],
            {}
        )

        status = item.get(
            "status",
            "index_only"
        )

        if status in {
            "index_only",
            "retry_history"
        }:

            pending_history.append(
                post
            )

    remaining_before = len(
        pending_history
    )

    tasks = pending_history[
        :BACKFILL_BATCH_SIZE
    ]

    print()
    print(
        "=" * 60
    )

    print(
        f"历史正文尚未完成："
        f"{remaining_before} 篇"
    )

    print(
        f"本轮准备补抓："
        f"{len(tasks)} 篇"
    )

    print(
        "=" * 60
    )

    consecutive_403 = 0

    total = len(
        tasks
    )

    for index, post in enumerate(
        tasks,
        start=1
    ):

        print()
        print(
            f"[历史补抓 "
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
                is_new=False
            )
        )

        if outcome == "403":

            consecutive_403 += 1

        else:

            consecutive_403 = 0

        if (
            index % 20
            == 0
        ):

            save_cache(
                cache
            )

        if (
            consecutive_403
            >= MAX_CONSECUTIVE_403
        ):

            print()
            print(
                "⚠️ 历史补抓连续出现多个 403。"
            )

            print(
                "本轮提前停止，"
                "剩余历史内容下次继续。"
            )

            break

        detail_pause(
            index
        )

    save_cache(
        cache
    )


# =========================================================
# 总更新逻辑
# =========================================================

def update_cache(
    list_posts,
    cache,
    state
):

    initialized = bool(
        state.get(
            "initialized",
            False
        )
    )

    # 当前列表中，有多少 URL 已经存在缓存里
    existing_visible = sum(
        1
        for post in list_posts
        if post["url"] in cache
    )

    if list_posts:

        coverage = (
            existing_visible
            / len(list_posts)
        )

    else:

        coverage = 0

    # =====================================================
    # baseline_mode：
    #
    # 第一次使用这套新逻辑；
    # 或缓存被删除；
    # 或旧缓存只覆盖了不到一半的网页。
    #
    # 这种情况下，
    # 当前已有帖子全部视为历史基准，
    # 不会当成几千篇“新帖子”。
    # =====================================================

    baseline_mode = (
        not initialized
        or not cache
        or coverage < 0.5
    )

    print()
    print(
        "=" * 60
    )

    print(
        f"当前列表帖子："
        f"{len(list_posts)} 篇"
    )

    print(
        f"缓存中已有："
        f"{len(cache)} 篇"
    )

    print(
        f"当前列表缓存覆盖率："
        f"{coverage:.1%}"
    )

    print(
        f"初始化基准模式："
        f"{baseline_mode}"
    )

    print(
        "=" * 60
    )

    new_count = (
        sync_list_to_cache(
            list_posts,
            cache,
            baseline_mode
        )
    )

    save_cache(
        cache
    )

    # 从这一次开始，
    # 后续运行就可以判断什么是真正的新帖
    state["initialized"] = True

    state["baseline_count"] = len(
        list_posts
    )

    state["last_scan"] = (
        datetime.now(
            SITE_TZ
        ).isoformat()
    )

    save_state(
        state
    )

    if baseline_mode:

        print()
        print(
            "当前网站已有内容"
            "已经建立为历史基准。"
        )

        print(
            "本轮不会把它们全部当成新帖。"
        )

    else:

        print()
        print(
            f"本次发现真正的新 URL："
            f"{new_count} 篇"
        )

    # =====================================================
    # 第一优先级：
    # 真正的新帖子
    # =====================================================

    site_limited = (
        process_new_posts(
            list_posts,
            cache
        )
    )

    # 如果新帖阶段已经连续 403，
    # 本轮不要继续补 400 篇历史。
    if site_limited:

        print()
        print(
            "检测到网站可能正在限流。"
        )

        print(
            "本轮跳过历史 400 篇补抓。"
        )

        return cache

    # =====================================================
    # 第二优先级：
    # 再额外补 400 篇历史正文
    # =====================================================

    process_history_backfill(
        list_posts,
        cache
    )

    return cache


# =========================================================
# RSS 时间解析
# =========================================================

def parse_cached_datetime(
    value
):

    if not value:

        return None

    try:

        dt = datetime.fromisoformat(
            value
        )

        # 兼容旧缓存里的无时区 datetime
        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=SITE_TZ
            )

        return dt

    except Exception:

        return None


# =========================================================
# 生成 RSS
# =========================================================

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

    # Python 排序是稳定的：
    # 没有真实发布时间的历史条目，
    # 会继续保持它们原来的插入顺序。
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

    if RSS_MAX_ITEMS is not None:

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
        f"{TARGET_AUTHOR} 的 RSS。"
        "新帖优先抓取全文，"
        "历史帖子自动分批补充正文。"
    )

    fg.language(
        "zh-CN"
    )

    fg.lastBuildDate(
        datetime.now(
            SITE_TZ
        )
    )

    count = 0

    full_count = 0

    pending_count = 0

    for post in items:

        url = post.get(
            "url"
        )

        title = post.get(
            "title"
        ) or "无标题"

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
            title
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

        content = post.get(
            "content",
            ""
        )

        status_text = ""

        if status == "full":

            full_count += 1

        elif status == "index_only":

            pending_count += 1

            status_text = (
                "<p><small>"
                "历史正文尚未补抓"
                "</small></p>"
            )

        elif status in {
            "pending_new",
            "retry_new"
        }:

            pending_count += 1

            status_text = (
                "<p><small>"
                "新帖正文等待抓取"
                "</small></p>"
            )

        elif status == "retry_history":

            pending_count += 1

            status_text = (
                "<p><small>"
                "历史正文将在后续任务中重试"
                "</small></p>"
            )

        elif status == "blocked":

            status_text = (
                "<p><small>"
                "该详情页返回 403，"
                "目前只保留标题和原帖链接"
                "</small></p>"
            )

        elif status == "failed":

            status_text = (
                "<p><small>"
                "该详情页多次读取失败，"
                "目前只保留标题和原帖链接"
                "</small></p>"
            )

        if not content:

            content = (
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
            f'<p><a href="{url}">'
            "查看原帖"
            "</a></p>"
        )

        count += 1

    fg.rss_file(
        FEED_FILE,
        pretty=True
    )

    print()
    print(
        "=" * 60
    )

    print(
        f"RSS 总条目："
        f"{count}"
    )

    print(
        f"已获取正文："
        f"{full_count}"
    )

    print(
        f"仍待补充正文："
        f"{pending_count}"
    )

    print(
        "=" * 60
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print()
    print(
        "开始更新 RSS"
    )

    cache = load_cache()

    state = load_state()

    # =====================================================
    # 每次仍然完整扫描全部 47+ 个列表页，
    # 保证可以及时发现真正的新帖子。
    # =====================================================

    list_posts = (
        collect_all_list_posts()
    )

    if not list_posts:

        raise RuntimeError(
            "没有获取到任何帖子列表，"
            "停止更新。"
        )

    # =====================================================
    # 顺序：
    #
    # 1. 新帖先抓
    # 2. 新帖全部完成
    # 3. 再补 400 篇历史
    # =====================================================

    cache = update_cache(
        list_posts,
        cache,
        state
    )

    # =====================================================
    # 用现有缓存生成 RSS
    # =====================================================

    generate_rss(
        cache
    )

    print()
    print(
        "本次 RSS 更新完成。"
    )


if __name__ == "__main__":

    main()
