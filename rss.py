import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

BASE_URL = "https://t66y.com/"
LIST_URL = "https://t66y.com/thread0806.php?fid=2&search=219675"
TARGET_AUTHOR = "愛在黑夜"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


def get_soup(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()

    # t66y 页面一般可由 apparent_encoding 正确判断
    r.encoding = r.apparent_encoding

    return BeautifulSoup(r.text, "html.parser")


def get_total_pages():
    """
    自动读取分页，不再写死抓 10 页。
    """
    soup = get_soup(LIST_URL)

    pages = {1}

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "thread0806.php" not in href:
            continue

        full_url = urljoin(BASE_URL, href)
        query = parse_qs(urlparse(full_url).query)

        if "page" in query:
            try:
                pages.add(int(query["page"][0]))
            except Exception:
                pass

    total = max(pages)

    print(f"检测到至少 {total} 页")

    return total


def get_posts_from_page(page):
    """
    从某一页提取主题标题和链接。
    """

    if page == 1:
        url = LIST_URL
    else:
        url = (
            "https://t66y.com/thread0806.php"
            f"?fid=2&page={page}&search=219675"
        )

    print(f"抓取列表第 {page} 页：{url}")

    soup = get_soup(url)

    posts = []

    for a in soup.find_all("a", href=True):

        href = a["href"]

        # 主题详情页
        if "htm_data" not in href:
            continue

        title = a.get_text(" ", strip=True)

        if not title:
            continue

        row = a.find_parent("tr")

        if not row:
            continue

        row_text = row.get_text(" ", strip=True)

        # 只要目标作者
        if TARGET_AUTHOR not in row_text:
            continue

        full_url = urljoin(BASE_URL, href)

        posts.append({
            "title": title,
            "url": full_url,
        })

    return posts


def parse_post_time(post_url):
    """
    进入主题详情页读取楼主真实发布时间。

    页面示例：
    TOP Posted: 09-19 13:52 樓主
    """

    try:
        soup = get_soup(post_url)

        text = soup.get_text(" ", strip=True)

        match = re.search(
            r"Posted:\s*(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s+樓主",
            text,
            re.I
        )

        if not match:
            print(f"没有找到发布时间：{post_url}")
            return None

        month, day, hour, minute = map(int, match.groups())

        now = datetime.now()

        year = now.year

        dt = datetime(
            year,
            month,
            day,
            hour,
            minute
        )

        # 防止跨年：
        # 比如现在 2027 年 1 月，
        # 页面显示 12-30，那么应该属于 2026 年。
        if dt > now:
            dt = dt.replace(year=year - 1)

        return dt

    except Exception as e:
        print(f"读取发布时间失败：{post_url}")
        print(e)

        return None


def collect_all_posts():
    total_pages = get_total_pages()

    all_posts = []
    seen = set()

    page = 1

    while page <= total_pages:

        try:
            posts = get_posts_from_page(page)

            print(f"第 {page} 页发现 {len(posts)} 个主题")

            for post in posts:

                if post["url"] in seen:
                    continue

                seen.add(post["url"])

                print("读取：", post["title"])

                published = parse_post_time(post["url"])

                post["published"] = published

                all_posts.append(post)

                # 稍微慢一点，减少服务器压力
                time.sleep(0.8)

            # 每翻一页再停一下
            time.sleep(1)

            page += 1

        except Exception as e:
            print(f"第 {page} 页失败：", e)

            # 继续下一页，不让整次任务直接中断
            page += 1

    return all_posts


def generate_rss(posts):

    # 没有解析到日期的放最后
    posts.sort(
        key=lambda x: x["published"] or datetime.min,
        reverse=True
    )

    fg = FeedGenerator()

    fg.title(f"{TARGET_AUTHOR} - T66Y RSS")

    fg.link(
        href=LIST_URL,
        rel="alternate"
    )

    fg.description(
        f"自动监控 {TARGET_AUTHOR} 的全部主题，"
        "按照楼主真实发布时间倒序排列"
    )

    fg.language("zh-CN")

    for post in posts:

        fe = fg.add_entry()

        fe.title(post["title"])

        fe.link(
            href=post["url"]
        )

        fe.guid(
            post["url"],
            permalink=True
        )

        fe.author({
            "name": TARGET_AUTHOR
        })

        if post["published"]:
            fe.pubDate(
                post["published"]
            )

        fe.description(
            f'<a href="{post["url"]}">打开原帖</a>'
        )

    fg.rss_file(
        "feed.xml",
        pretty=True
    )

    print()
    print("========================")
    print(f"完成，共抓到 {len(posts)} 个主题")
    print("========================")


def main():

    posts = collect_all_posts()

    generate_rss(posts)


if __name__ == "__main__":
    main()
