import json
import os
import re
import shutil
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

TARGET_AUTHOR = "愛在黑夜"
DATA_DIR = "data"

# 三个分类共享，每轮最多补抓 450 篇历史正文。
# 真正的新帖不占这 450 篇额度。
GLOBAL_BACKFILL_BATCH_SIZE = 450

SITE_TZ = timezone(timedelta(hours=8))

PAGE_DELAY = 1.0
POST_DELAY = 1.0

DETAIL_PAUSE_EVERY = 50
DETAIL_PAUSE_SECONDS = 8

MAX_LIST_RETRIES = 5
MAX_DETAIL_FAILURES = 3
MAX_CONSECUTIVE_403 = 3

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


class ForbiddenError(Exception):
    pass


class SourceScanError(Exception):
    pass


def get_soup(url, retries=3):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            print(f"请求：{url}")

            r = session.get(url, timeout=30)

            if r.status_code == 403:
                raise ForbiddenError(url)

            r.raise_for_status()

            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"

            return BeautifulSoup(r.text, "html.parser")

        except ForbiddenError:
            raise

        except Exception as e:
            last_error = e
            print(f"请求失败 {attempt}/{retries}：{e}")
            time.sleep(4 * attempt)

    raise last_error


# =========================================================
# 路径
# =========================================================

def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "feed"


def source_paths(source):
    name = safe_name(source["name"])
    folder = os.path.join(DATA_DIR, name)

    return {
        "dir": folder,
        "feed": os.path.join(folder, f"{name}_feed.xml"),
        "cache": os.path.join(folder, f"{name}_posts_cache.json"),
        "state": os.path.join(folder, f"{name}_rss_state.json"),
    }


def ensure_source_dir(source):
    paths = source_paths(source)
    os.makedirs(paths["dir"], exist_ok=True)
    return paths


# =========================================================
# 迁移旧版亚洲无码原创区数据
# =========================================================

def maybe_migrate_legacy_asia(source):
    if source["id"] != "asia_uncensored_original":
        return

    paths = ensure_source_dir(source)

    if not os.path.exists(paths["cache"]) and os.path.exists("posts_cache.json"):
        print("发现旧 posts_cache.json，迁移到「亚洲无码原创区」。")
        shutil.copy2("posts_cache.json", paths["cache"])

    if not os.path.exists(paths["state"]) and os.path.exists("rss_state.json"):
        print("发现旧 rss_state.json，迁移到「亚洲无码原创区」。")
        shutil.copy2("rss_state.json", paths["state"])


# =========================================================
# JSON
# =========================================================

def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"读取 {path} 失败：{e}")
        return default


def atomic_save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temp_path, path)


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
    if not isinstance(cache, dict):
        return {}

    for url, item in list(cache.items()):
        if not isinstance(item, dict):
            cache[url] = {
                "title": "",
                "url": url,
                "published": None,
                "content": "",
                "status": "index_only",
                "fail_count": 0,
            }
            continue

        item.setdefault("url", url)
        item.setdefault("title", "")
        item.setdefault("published", None)
        item.setdefault("content", "")
        item.setdefault("fail_count", 0)

        status = item.get("status")

        if status == "retry":
            item["status"] = "retry_history"
            continue

        if status in VALID_STATUSES:
            continue

        if item.get("blocked"):
            item["status"] = "blocked"
            continue

        if item.get("ok") is True:
            content = item.get("content", "")

            if (
                "历史索引" in content
                or "等待抓取" in content
                or not content
            ):
                item["status"] = "index_only"
            else:
                item["status"] = "full"

            continue

        if item.get("ok") is False:
            item["status"] = "retry_history"
            continue

        item["status"] = "index_only"

    return cache


def load_cache(source):
    paths = ensure_source_dir(source)

    cache = normalize_cache(
        load_json(paths["cache"], {})
    )

    print(f"[{source['name']}] 读取缓存：{len(cache)} 篇")
    return cache


def save_cache(source, cache):
    paths = source_paths(source)
    atomic_save_json(paths["cache"], cache)

    print(f"[{source['name']}] 缓存已保存：{len(cache)} 篇")


# =========================================================
# 状态
# =========================================================

def load_state(source):
    paths = ensure_source_dir(source)

    state = load_json(
        paths["state"],
        {"initialized": False},
    )

    if not isinstance(state, dict):
        state = {"initialized": False}

    state.setdefault("initialized", False)
    return state


def save_state(source, state):
    atomic_save_json(
        source_paths(source)["state"],
        state,
    )


# =========================================================
# 分页 URL
# =========================================================

def make_page_url(source, page):
    parsed = urlparse(source["url"])

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )

    if page <= 1:
        query.pop("page", None)
    else:
        query["page"] = str(page)

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


def same_source_query(source, href):
    source_parsed = urlparse(source["url"])
    source_query = parse_qs(source_parsed.query)

    full_url = urljoin(source["url"], href)
    parsed = urlparse(full_url)
    query = parse_qs(parsed.query)

    if parsed.path != source_parsed.path:
        return False

    for key in ("fid", "search"):
        expected = source_query.get(key, [None])[0]
        actual = query.get(key, [None])[0]

        if expected is not None and actual != expected:
            return False

    return True


# =========================================================
# 总页数
# =========================================================

def get_total_pages(source):
    print(f"[{source['name']}] 正在检测总页数……")

    pages = {1}

    try:
        soup = get_soup(source["url"])

        for a in soup.find_all("a", href=True):
            try:
                href = a["href"]

                if not same_source_query(source, href):
                    continue

                full_url = urljoin(source["url"], href)
                query = parse_qs(urlparse(full_url).query)

                if "page" not in query:
                    continue

                page = int(query["page"][0])

                if page >= 1:
                    pages.add(page)

            except Exception:
                continue

    except Exception as e:
        print(f"[{source['name']}] 自动检测页数失败：{e}")

    detected = max(pages)

    total = max(
        detected,
        int(source.get("min_pages", 1)),
    )

    print(
        f"[{source['name']}] "
        f"网页检测：{detected} 页；"
        f"本次扫描：1～{total} 页"
    )

    return total


# =========================================================
# 列表页
# =========================================================

def get_posts_from_page(source, page):
    soup = get_soup(
        make_page_url(source, page)
    )

    posts = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")

        if "htm_data" not in href:
            continue

        title = a.get_text(" ", strip=True)

        if not title:
            continue

        url = urljoin(source["url"], href)

        if url in seen:
            continue

        seen.add(url)

        posts.append({
            "title": title,
            "url": url,
        })

    return posts


def fetch_list_page(
    source,
    page,
    previous_signature=None,
):
    for attempt in range(
        1,
        MAX_LIST_RETRIES + 1,
    ):
        try:
            posts = get_posts_from_page(
                source,
                page,
            )

            if not posts:
                print(
                    f"[{source['name']}] "
                    f"第 {page} 页返回 0 条，"
                    f"重试 {attempt}/{MAX_LIST_RETRIES}"
                )
                time.sleep(attempt * 5)
                continue

            signature = tuple(
                post["url"]
                for post in posts
            )

            if (
                previous_signature
                and signature == previous_signature
            ):
                print(
                    f"[{source['name']}] "
                    f"第 {page} 页疑似重复上一页，"
                    f"重试 {attempt}/{MAX_LIST_RETRIES}"
                )
                time.sleep(attempt * 5)
                continue

            return posts, signature

        except ForbiddenError:
            print(
                f"[{source['name']}] "
                f"第 {page} 个列表页返回 403"
            )
            return None, None

        except Exception as e:
            print(
                f"[{source['name']}] "
                f"第 {page} 页失败 "
                f"{attempt}/{MAX_LIST_RETRIES}：{e}"
            )
            time.sleep(attempt * 5)

    return None, None


def collect_all_list_posts(source):
    total_pages = get_total_pages(source)

    all_posts = []
    seen_urls = set()
    failed_pages = []
    previous_signature = None

    for page in range(
        1,
        total_pages + 1,
    ):
        print(
            f"[{source['name']}] "
            f"正在扫描 {page}/{total_pages} 页"
        )

        posts, signature = fetch_list_page(
            source,
            page,
            previous_signature,
        )

        if not posts:
            failed_pages.append(page)

            print(
                f"[{source['name']}] "
                f"第 {page} 页第一次失败，"
                "先继续后面的页面"
            )

            continue

        previous_signature = signature

        added = 0

        for post in posts:
            if post["url"] in seen_urls:
                continue

            seen_urls.add(post["url"])
            all_posts.append(post)
            added += 1

        print(
            f"[{source['name']}] "
            f"本页 {len(posts)} 条；"
            f"新增 {added}；"
            f"累计 {len(all_posts)}"
        )

        time.sleep(PAGE_DELAY)

    if failed_pages:
        print(
            f"[{source['name']}] "
            f"开始补抓失败页面：{failed_pages}"
        )

        time.sleep(15)

        still_failed = []

        for page in failed_pages:
            posts, _ = fetch_list_page(
                source,
                page,
                None,
            )

            if not posts:
                still_failed.append(page)
                continue

            for post in posts:
                if post["url"] not in seen_urls:
                    seen_urls.add(post["url"])
                    all_posts.append(post)

            time.sleep(PAGE_DELAY)

        if still_failed:
            raise SourceScanError(
                f"[{source['name']}] "
                "以下页面两轮重试后仍失败："
                f"{still_failed}"
            )

    print(
        f"[{source['name']}] "
        f"扫描完成，共 {len(all_posts)} 个不重复帖子"
    )

    return all_posts, total_pages


# =========================================================
# 发帖时间
# =========================================================

def infer_year_from_url(
    post_url,
    fallback_month,
):
    match = re.search(
        r"htm_data/(\d{2})(\d{2})/",
        post_url,
    )

    if match:
        yy = int(match.group(1))
        mm = int(match.group(2))

        if 1 <= mm <= 12:
            return 2000 + yy

    now = datetime.now(SITE_TZ)

    if fallback_month > now.month:
        return now.year - 1

    return now.year


def parse_publish_time(
    soup,
    post_url,
):
    text = soup.get_text(
        " ",
        strip=True,
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
            re.I,
        )

        if match:
            break

    if not match:
        return None

    month, day, hour, minute = map(
        int,
        match.groups(),
    )

    year = infer_year_from_url(
        post_url,
        month,
    )

    try:
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=SITE_TZ,
        )
    except ValueError:
        return None


# =========================================================
# 正文
# =========================================================

def clean_content(
    node,
    post_url,
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

    for img in node.find_all("img"):
        src = img.get("src")

        if src:
            img["src"] = urljoin(
                post_url,
                src,
            )

        img.attrs.pop(
            "onclick",
            None,
        )
        img.attrs.pop(
            "onload",
            None,
        )

    for a in node.find_all(
        "a",
        href=True,
    ):
        a["href"] = urljoin(
            post_url,
            a["href"],
        )

    return str(node)


def find_main_content(
    soup,
    post_url,
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
                post_url,
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
                post_url,
            )

    return ""


# =========================================================
# 详情页
# =========================================================

def fetch_post_detail(post):
    try:
        soup = get_soup(
            post["url"]
        )

        published = parse_publish_time(
            soup,
            post["url"],
        )

        content = find_main_content(
            soup,
            post["url"],
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
# 缓存同步
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


def sync_list_to_cache(
    list_posts,
    cache,
    baseline_mode,
):
    new_count = 0

    for post in list_posts:
        url = post["url"]

        if url in cache:
            cache[url]["title"] = (
                post["title"]
            )
            cache[url]["url"] = url
            continue

        if baseline_mode:
            cache[url] = make_index_item(
                post
            )
            continue

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


def apply_detail_result(
    post,
    result,
    cache,
    is_new,
):
    url = post["url"]
    old_item = cache.get(url, {})

    old_published = old_item.get(
        "published"
    )
    old_content = old_item.get(
        "content",
        "",
    )

    if result["type"] == "success":
        cache[url] = {
            "title": post["title"],
            "url": url,
            "published": (
                result.get("published")
                or old_published
            ),
            "content": (
                result.get("content")
                or old_content
            ),
            "status": "full",
            "fail_count": 0,
        }

        return "success"

    if result["type"] == "403":
        cache[url] = {
            "title": post["title"],
            "url": url,
            "published": old_published,
            "content": (
                "<p>"
                "该详情页返回 403，"
                "目前只保留标题和原帖链接。"
                "</p>"
            ),
            "status": "blocked",
            "fail_count": old_item.get(
                "fail_count",
                0,
            ),
        }

        return "403"

    fail_count = (
        old_item.get(
            "fail_count",
            0,
        )
        + 1
    )

    if fail_count >= MAX_DETAIL_FAILURES:
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
        "published": old_published,
        "content": (
            old_content
            or
            "<p>"
            "正文暂时读取失败。"
            "</p>"
        ),
        "status": status,
        "fail_count": fail_count,
    }

    return "error"


# =========================================================
# 节流
# =========================================================

def detail_pause(index):
    time.sleep(POST_DELAY)

    if (
        index > 0
        and index % DETAIL_PAUSE_EVERY == 0
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
    total_pages,
):
    maybe_migrate_legacy_asia(
        source
    )

    cache = load_cache(
        source
    )

    state = load_state(
        source
    )

    initialized = bool(
        state.get(
            "initialized",
            False,
        )
    )

    existing_visible = sum(
        1
        for post in list_posts
        if post["url"] in cache
    )

    coverage = (
        existing_visible
        / len(list_posts)
        if list_posts
        else 0
    )

    baseline_mode = (
        not initialized
        or not cache
        or coverage < 0.90
    )

    new_count = sync_list_to_cache(
        list_posts,
        cache,
        baseline_mode,
    )

    state.update({
        "initialized": True,
        "last_scan": datetime.now(
            SITE_TZ
        ).isoformat(),
        "last_total_pages": total_pages,
        "last_list_count": len(
            list_posts
        ),
    })

    save_cache(
        source,
        cache,
    )

    save_state(
        source,
        state,
    )

    print(
        f"[{source['name']}] "
        f"列表 {len(list_posts)}；"
        f"缓存 {len(cache)}；"
        f"覆盖率 {coverage:.1%}；"
        f"基准模式 {baseline_mode}；"
        f"真正新增 {new_count}"
    )

    return {
        "source": source,
        "list_posts": list_posts,
        "cache": cache,
    }


# =========================================================
# 新帖优先
# =========================================================

def process_new_posts(context):
    source = context["source"]
    posts = context["list_posts"]
    cache = context["cache"]

    tasks = [
        post
        for post in posts
        if cache.get(
            post["url"],
            {},
        ).get("status")
        in {
            "pending_new",
            "retry_new",
        }
    ]

    print(
        f"[{source['name']}] "
        f"优先处理新帖：{len(tasks)} 篇"
    )

    consecutive_403 = 0
    total = len(tasks)

    for index, post in enumerate(
        tasks,
        start=1,
    ):
        print(
            f"[{source['name']}] "
            f"[新帖 {index}/{total}] "
            f"{post['title']}"
        )

        result = fetch_post_detail(
            post
        )

        outcome = apply_detail_result(
            post,
            result,
            cache,
            True,
        )

        if outcome == "403":
            consecutive_403 += 1
        else:
            consecutive_403 = 0

        if index % 10 == 0:
            save_cache(
                source,
                cache,
            )

        if (
            consecutive_403
            >= MAX_CONSECUTIVE_403
        ):
            print(
                f"[{source['name']}] "
                "连续多个 403，"
                "本轮停止详情抓取"
            )

            save_cache(
                source,
                cache,
            )

            return True

        detail_pause(index)

    save_cache(
        source,
        cache,
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
# 全局历史补抓：总共450篇，动态分配，最新优先
# =========================================================

def history_priority(
    post,
    item,
    position,
):
    """
    优先顺序：

    1. 如果已经知道真实发布时间，用真实发布时间。
    2. 否则从 URL 的 htm_data/YYMM 推断年月。
    3. 同年月时，列表位置越靠前越优先。
    """

    published = parse_cached_datetime(
        item.get("published")
    )

    if published:
        return (
            2,
            published.timestamp(),
            -position,
        )

    match = re.search(
        r"htm_data/(\d{2})(\d{2})/",
        post["url"],
    )

    if match:
        year = 2000 + int(
            match.group(1)
        )

        month = int(
            match.group(2)
        )

        if 1 <= month <= 12:
            rough = datetime(
                year,
                month,
                1,
                tzinfo=SITE_TZ,
            )

            return (
                1,
                rough.timestamp(),
                -position,
            )

    return (
        0,
        0,
        -position,
    )


def process_global_history_backfill(
    contexts
):
    candidates = []

    for context in contexts:
        source = context["source"]
        posts = context["list_posts"]
        cache = context["cache"]

        for position, post in enumerate(
            posts
        ):
            item = cache.get(
                post["url"],
                {},
            )

            status = item.get(
                "status",
                "index_only",
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
                "priority": history_priority(
                    post,
                    item,
                    position,
                ),
            })

    candidates.sort(
        key=lambda x: x["priority"],
        reverse=True,
    )

    tasks = candidates[
        :GLOBAL_BACKFILL_BATCH_SIZE
    ]

    print()
    print("=" * 70)

    print(
        f"三个分类历史待补总数："
        f"{len(candidates)} 篇"
    )

    print(
        f"本轮全局历史补抓："
        f"{len(tasks)} / "
        f"{GLOBAL_BACKFILL_BATCH_SIZE} 篇"
    )

    allocation = {}

    for task in tasks:
        name = task["source"]["name"]

        allocation[name] = (
            allocation.get(
                name,
                0,
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

    print("=" * 70)

    consecutive_403 = 0
    source_counts = {}
    total = len(tasks)

    for index, task in enumerate(
        tasks,
        start=1,
    ):
        source = task["source"]
        context = task["context"]
        post = task["post"]
        cache = context["cache"]

        name = source["name"]

        print(
            f"[全局历史 "
            f"{index}/{total}] "
            f"[{name}] "
            f"{post['title']}"
        )

        result = fetch_post_detail(
            post
        )

        outcome = apply_detail_result(
            post,
            result,
            cache,
            False,
        )

        source_counts[name] = (
            source_counts.get(
                name,
                0,
            )
            + 1
        )

        if outcome == "403":
            consecutive_403 += 1
        else:
            consecutive_403 = 0

        if source_counts[name] % 20 == 0:
            save_cache(
                source,
                cache,
            )

        if (
            consecutive_403
            >= MAX_CONSECUTIVE_403
        ):
            print(
                "⚠️ 连续出现多个 403，"
                "本轮全局历史补抓提前停止。"
            )
            break

        detail_pause(index)

    for context in contexts:
        save_cache(
            context["source"],
            context["cache"],
        )

    return (
        consecutive_403
        >= MAX_CONSECUTIVE_403
    )


# =========================================================
# 生成 RSS
# =========================================================

def generate_rss(context):
    source = context["source"]
    cache = context["cache"]

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
        tzinfo=SITE_TZ,
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
        reverse=True,
    )

    if RSS_MAX_ITEMS is not None:
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
        rel="alternate",
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
        url = post.get("url")

        if not url:
            continue

        fe = fg.add_entry()

        fe.id(url)

        fe.guid(
            url,
            permalink=True,
        )

        fe.title(
            post.get("title")
            or "无标题"
        )

        fe.link(
            href=url,
            rel="alternate",
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
            "index_only",
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
            "",
        )

        content = (
            post.get("content")
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
            f'<p><a href="{url}">'
            f'查看原帖'
            f'</a></p>'
        )

    fg.rss_file(
        paths["feed"],
        pretty=True,
    )

    print(
        f"[{source['name']}] "
        f"已生成："
        f"{paths['feed']}"
    )

    # 兼容原来的 /feed.xml
    # 继续作为亚洲无码原创区 RSS
    if (
        source["id"]
        == "asia_uncensored_original"
    ):
        shutil.copy2(
            paths["feed"],
            "feed.xml",
        )


# =========================================================
# MAIN
# =========================================================

def main():
    print(
        "开始更新多分类 RSS"
    )

    contexts = []

    # 第一阶段：
    # 先扫描三个分类的全部分页
    for source in SOURCES:
        print()
        print("#" * 70)
        print(
            f"扫描分类："
            f"{source['name']}"
        )
        print("#" * 70)

        try:
            posts, total_pages = (
                collect_all_list_posts(
                    source
                )
            )

            context = prepare_source(
                source,
                posts,
                total_pages,
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

    # 第二阶段：
    # 三个分类的新帖全部优先处理
    site_limited = False

    for context in contexts:
        limited = process_new_posts(
            context
        )

        if limited:
            site_limited = True
            break

    # 第三阶段：
    # 新帖处理完之后，
    # 三个分类共享450篇历史补抓额度，
    # 按最新 -> 最旧动态分配。
    if not site_limited:
        process_global_history_backfill(
            contexts
        )
    else:
        print(
            "检测到网站可能正在限流，"
            "本轮跳过历史补抓。"
        )

    # 第四阶段：
    # 分别生成三个 RSS
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
