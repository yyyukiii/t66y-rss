import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from urllib.parse import urljoin
import time

BASE_URL = "https://t66y.com/"
LIST_URL = "https://t66y.com/thread0806.php?fid=2&search=219675"

# 想监控的作者
TARGET_AUTHOR = "愛在黑夜"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

def get_posts(page=1):
    url = LIST_URL

    # 如果分页参数实际不同，可以后面再按网页结构调整
    if page > 1:
        url += f"&page={page}"

    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()

    # 这类老网页常见编码
    r.encoding = r.apparent_encoding

    soup = BeautifulSoup(r.text, "html.parser")

    posts = []

    # 找帖子链接
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(strip=True)

        if not title:
            continue

        # 只处理帖子详情链接
        if "htm_data" not in href:
            continue

        full_url = urljoin(BASE_URL, href)

        row = a.find_parent("tr")

        author = ""

        if row:
            text = row.get_text(" ", strip=True)

            # 只筛指定作者
            if TARGET_AUTHOR not in text:
                continue

            author = TARGET_AUTHOR

        posts.append({
            "title": title,
            "url": full_url,
            "author": author
        })

    return posts


def main():
    all_posts = []
    seen = set()

    # 抓前 10 页
    for page in range(1, 11):
        try:
            posts = get_posts(page)

            for post in posts:
                if post["url"] not in seen:
                    seen.add(post["url"])
                    all_posts.append(post)

            time.sleep(1)

        except Exception as e:
            print(f"第 {page} 页失败:", e)

    fg = FeedGenerator()

    fg.title(f"{TARGET_AUTHOR} - T66Y RSS")
    fg.link(href=LIST_URL)
    fg.description(f"自动监控 {TARGET_AUTHOR} 的新帖子")
    fg.language("zh-CN")

    for post in all_posts:
        fe = fg.add_entry()

        fe.title(post["title"])
        fe.link(href=post["url"])
        fe.guid(post["url"], permalink=True)

        fe.author({
            "name": post["author"]
        })

        fe.description(
            f'<a href="{post["url"]}">查看帖子</a>'
        )

    fg.rss_file("feed.xml", pretty=True)

    print(f"生成完成，共 {len(all_posts)} 条")


if __name__ == "__main__":
    main()
