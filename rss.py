import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

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

# 网站时间按 UTC+8 处理
SITE_TZ = timezone(timedelta(hours=8))

# 页与页之间暂停
PAGE_DELAY = 1.0

# 打开每篇帖子之间暂停
POST_DELAY = 0.6

# 防止网页异常导致无限翻页
MAX_PAGES = 1000


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
    """
    请求网页，失败自动重试。
    """

    last_error = None

    for attempt in range(1, retries + 1):

        try:
            print(f"请求：{url}")

            r = session.get(
                url,
                timeout=30
            )

            r.raise_for_status()

            # 自动判断网页编码
            r.encoding = r.apparent_encoding

            return BeautifulSoup(
                r.text,
                "html.parser"
            )

        except Exception as e:

            last_error = e

            print(
                f"请求失败，第 {attempt}/{retries} 次：",
                e
            )

            time.sleep(3 * attempt)

    raise last_error


# =========================================================
# URL
# =========================================================

def make_page_url(page):
    """
    生成搜索结果分页地址。
    """

    if page == 1:
        return LIST_URL

    return (
        "https://t66y.com/thread0806.php"
        f"?fid=2&search=219675&page={page}"
    )


# =========================================================
# 获取列表页主题
# =========================================================

def get_posts_from_page(page):
    """
    获取某一页中的所有帖子。
    """

    url = make_page_url(page)

    soup = get_soup(url)

    posts = []
    seen = set()

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get("href", "")

        # T66Y 主题详情链接一般包含 htm_data
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

        # 防止一个帖子在 HTML 中出现多次
        if full_url in seen:
            continue

        seen.add(full_url)

        # 如果能找到所在表格行，就检查作者
        row = a.find_parent("tr")

        if row:
            row_text = row.get_text(
                " ",
                strip=True
            )

            # 当前 search 本身已经是作者搜索，
            # 但仍多做一次校验。
            if (
                TARGET_AUTHOR
                and TARGET_AUTHOR not in row_text
            ):
                continue

        posts.append({
            "title": title,
            "url": full_url
        })

    return posts


# =========================================================
# 自动遍历所有分页
# =========================================================

def collect_post_links():
    """
    从第一页一直抓到最后一页。

    不依赖网页上只显示几个页码，
    而是不断尝试下一页。
    当某一页已经没有主题时停止。
    """

    all_posts = []

    seen_urls = set()

    page = 1

    while page <= MAX_PAGES:

        print()
        print(
            "======================================"
        )
        print(f"正在读取列表第 {page} 页")
        print(
            "======================================"
        )

        try:

            posts = get_posts_from_page(
                page
            )

        except Exception as e:

            print(
                f"第 {page} 页读取失败：",
                e
            )

            # 单页错误时先重试下一页不太安全，
            # 所以这里停止，避免漏大量内容。
            break

        print(
            f"这一页发现 {len(posts)} 个主题"
        )

        # 没有主题 = 已到最后一页
        if not posts:
            print(
                "没有发现主题，认为已经到最后一页。"
            )
            break

        new_count = 0

        for post in posts:

            if post["url"] in seen_urls:
                continue

            seen_urls.add(
                post["url"]
            )

            all_posts.append(
                post
            )

            new_count += 1

        print(
            f"新增 {new_count} 个主题"
        )

        # 防止服务器把最后一页重复返回
        if new_count == 0:

            print(
                "这一页没有新的主题，停止翻页。"
            )

            break

        page += 1

        time.sleep(
            PAGE_DELAY
        )

    print()
    print(
        f"列表抓取结束，共找到 "
        f"{len(all_posts)} 个不重复主题"
    )

    return all_posts


# =========================================================
# 解析真实发帖时间
# =========================================================

def parse_publish_time(soup):
    """
    从详情页寻找楼主真实发帖时间。

    常见格式：
    Posted: 09-19 13:52 樓主
    """

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

    now = datetime.now(
        SITE_TZ
    )

    year = now.year

    try:

        dt = datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=SITE_TZ
        )

    except ValueError:

        return None

    # 解决跨年问题
    #
    # 例如现在是 2027-01-02
    # 网页写 12-30
    # 那应该是 2026-12-30
    if dt > now:

        try:
            dt = dt.replace(
                year=year - 1
            )

        except ValueError:
            pass

    return dt


# =========================================================
# 清理正文
# =========================================================

def clean_content(node, post_url):
    """
    清理正文 HTML，并修复图片地址。
    """

    if node is None:
        return ""

    # 删除脚本 / 样式等
    for bad in node.find_all([
        "script",
        "style",
        "iframe",
        "noscript"
    ]):

        bad.decompose()

    # 修复图片地址
    for img in node.find_all("img"):

        src = img.get("src")

        if not src:
            continue

        img["src"] = urljoin(
            post_url,
            src
        )

        # 清除可能造成阅读器问题的属性
        for attr in [
            "onclick",
            "onload"
        ]:

            if attr in img.attrs:
                del img.attrs[attr]

    # 修复正文里的链接
    for a in node.find_all(
        "a",
        href=True
    ):

        a["href"] = urljoin(
            post_url,
            a["href"]
        )

    return str(node)


# =========================================================
# 抓取楼主正文
# =========================================================

def find_main_content(
    soup,
    post_url
):
    """
    优先寻找 T66Y 老论坛常见楼主正文节点。
    """

    candidates = []

    # T66Y 常见正文 ID
    for element_id in [
        "read_tpc",
        "read_tpc_0"
    ]:

        node = soup.find(
            id=element_id
        )

        if node:
            candidates.append(
                node
            )

    # 常见 class
    if not candidates:

        for class_pattern in [
            r"\btpc_content\b",
            r"\btpc\b",
            r"\bpost_content\b",
            r"\bpost-content\b"
        ]:

            nodes = soup.find_all(
                ["div", "td"],
                class_=re.compile(
                    class_pattern,
                    re.I
                )
            )

            if nodes:
                candidates.extend(
                    nodes
                )
                break

    if candidates:

        # 通常第一个就是楼主正文
        return clean_content(
            candidates[0],
            post_url
        )

    # 最后的退路：
    # 找文本量最大的 td/div
    best_node = None
    best_length = 0

    for node in soup.find_all(
        ["td", "div"]
    ):

        text = node.get_text(
            " ",
            strip=True
        )

        length = len(text)

        if length > best_length:

            # 排除明显整页容器
            if length < 100000:

                best_length = length
                best_node = node

    if best_node:

        return clean_content(
            best_node,
            post_url
        )

    return ""


# =========================================================
# 抓取详情
# =========================================================

def parse_post_detail(post):
    """
    获取：
    - 发布时间
    - 楼主正文
    """

    url = post["url"]

    try:

        soup = get_soup(
            url
        )

        published = parse_publish_time(
            soup
        )

        content = find_main_content(
            soup,
            url
        )

        if not content:

            content = (
                "<p>"
                "正文抓取失败，请打开原帖查看。"
                "</p>"
            )

        return {
            **post,
            "published": published,
            "content": content
        }

    except Exception as e:

        print(
            "读取主题失败：",
            post["title"],
            e
        )

        return {
            **post,
            "published": None,
            "content": (
                "<p>"
                "正文暂时抓取失败。"
                "</p>"
            )
        }


# =========================================================
# 抓所有详情
# =========================================================

def collect_all_posts():
    """
    获取全部主题及正文。
    """

    basic_posts = collect_post_links()

    result = []

    total = len(
        basic_posts
    )

    for index, post in enumerate(
        basic_posts,
        start=1
    ):

        print()
        print(
            f"[{index}/{total}] "
            f"{post['title']}"
        )

        detail = parse_post_detail(
            post
        )

        result.append(
            detail
        )

        time.sleep(
            POST_DELAY
        )

    return result


# =========================================================
# RSS
# =========================================================

def generate_rss(posts):

    # 只让带时间的正常参与排序
    #
    # timezone-aware datetime 与
    # timezone-aware datetime 比较。
    minimum_time = datetime(
        1970,
        1,
        1,
        tzinfo=SITE_TZ
    )

    posts.sort(
        key=lambda item: (
            item.get("published")
            or minimum_time
        ),
        reverse=True
    )

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
        f"{TARGET_AUTHOR} 的帖子 RSS，"
        "包含楼主正文，并按照真实发布时间倒序排列。"
    )

    fg.language(
        "zh-CN"
    )

    # RSS 自身最后更新时间
    fg.lastBuildDate(
        datetime.now(
            SITE_TZ
        )
    )

    for post in posts:

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

        published = post.get(
            "published"
        )

        if published is not None:

            # 此处现在一定包含 +08:00 时区
            fe.pubDate(
                published
            )

        content = post.get(
            "content",
            ""
        )

        # description 直接放完整正文
        fe.description(
            content
            +
            "<hr>"
            +
            f'<p><a href="{post["url"]}">'
            "查看原帖"
            "</a></p>"
        )

    fg.rss_file(
        "feed.xml",
        pretty=True
    )

    print()
    print(
        "======================================"
    )
    print(
        f"RSS 生成成功，共 {len(posts)} 篇"
    )
    print(
        "文件：feed.xml"
    )
    print(
        "======================================"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print()
    print(
        "开始更新 T66Y RSS"
    )

    posts = collect_all_posts()

    if not posts:

        raise RuntimeError(
            "没有抓取到任何帖子，"
            "为避免覆盖现有 RSS，停止生成。"
        )

    generate_rss(
        posts
    )


if __name__ == "__main__":
    main()
