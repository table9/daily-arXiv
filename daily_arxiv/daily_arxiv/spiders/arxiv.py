import scrapy
import os
import re
import json
from pathlib import Path
from datetime import date as Date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class ArxivSpider(scrapy.Spider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        categories = os.environ.get("CATEGORIES", "cs.CV")
        categories = categories.split(",")
        # 保存目标分类列表，用于后续验证
        self.target_categories = set(map(str.strip, categories))
        self.start_urls = [
            f"https://arxiv.org/list/{cat}/new" for cat in self.target_categories
        ]  # 起始URL（计算机科学领域的最新论文）
        self.target_date = kwargs.get('date')
        self.stats_file = kwargs.get('stats_file')
        if self.target_date:
            day = Date.fromisoformat(self.target_date)
            today = datetime.now(timezone.utc).date()
            if not today - timedelta(days=90) <= day <= today:
                raise ValueError('arXiv catchup supports only the last 90 days')
            self.start_urls = [
                f'https://arxiv.org/catchup/{cat}/{day.isoformat()}?abs=False&page=1'
                for cat in sorted(self.target_categories)
            ]
        self.expected_pages = set(self.start_urls)
        self.completed_pages = set()
        self.selected_ids = set()

    name = "arxiv"  # 爬虫名称
    allowed_domains = ["arxiv.org"]  # 允许爬取的域名

    def parse(self, response):
        if self.target_date:
            yield from self.parse_dated(response)
            return
        # 提取每篇论文的信息
        anchors = []
        for li in response.css("div[id=dlpage] ul li"):
            href = li.css("a::attr(href)").get()
            if href and "item" in href:
                anchors.append(int(href.split("item")[-1]))

        # 遍历每篇论文的详细信息
        for paper in response.css("dl dt"):
            paper_anchor = paper.css("a[name^='item']::attr(name)").get()
            if not paper_anchor:
                continue
                
            paper_id = int(paper_anchor.split("item")[-1])
            if anchors and paper_id >= anchors[-1]:
                continue

            # 获取论文ID
            abstract_link = paper.css("a[title='Abstract']::attr(href)").get()
            if not abstract_link:
                continue
                
            arxiv_id = abstract_link.split("/")[-1]
            
            # 获取对应的论文描述部分 (dd元素)
            paper_dd = paper.xpath("following-sibling::dd[1]")
            if not paper_dd:
                continue
            
            # 提取论文分类信息 - 在subjects部分
            subjects_text = paper_dd.css(".list-subjects .primary-subject::text").get()
            if not subjects_text:
                # 如果找不到主分类，尝试其他方式获取分类
                subjects_text = paper_dd.css(".list-subjects::text").get()
            
            if subjects_text:
                # 解析分类信息，通常格式如 "Computer Vision and Pattern Recognition (cs.CV)"
                # 提取括号中的分类代码
                categories_in_paper = re.findall(r'\(([^)]+)\)', subjects_text)
                
                # 检查论文分类是否与目标分类有交集
                paper_categories = set(categories_in_paper)
                if paper_categories.intersection(self.target_categories):
                    yield {
                        "id": arxiv_id,
                        "categories": list(paper_categories),  # 添加分类信息用于调试
                    }
                    self.logger.info(f"Found paper {arxiv_id} with categories {paper_categories}")
                else:
                    self.logger.debug(f"Skipped paper {arxiv_id} with categories {paper_categories} (not in target {self.target_categories})")
            else:
                # 如果无法获取分类信息，记录警告但仍然返回论文（保持向后兼容）
                self.logger.warning(f"Could not extract categories for paper {arxiv_id}, including anyway")
                yield {
                    "id": arxiv_id,
                    "categories": [],
                }

    def parse_dated(self, response):
        """Read the announcement cohort, not a submission-date approximation."""
        expected_heading = datetime.strptime(self.target_date, '%Y-%m-%d').strftime('%a, %d %b %Y')
        heading = response.css('#dlpage h1::text').get('')
        if not heading.endswith(' on ' + expected_heading):
            raise ValueError(f'Unexpected catchup page/date: {response.url}')
        counts = re.findall(r'Total of ([\d,]+) entries', response.text)
        if not counts or len(set(counts)) != 1:
            raise ValueError(f'Missing/inconsistent catchup count: {response.url}')
        total = int(counts[0].replace(',', ''))
        parts = urlsplit(response.url)
        page = int(parse_qs(parts.query).get('page', ['1'])[0])
        papers = response.css('#dlpage dl dt')
        if len(papers) != min(2000, max(0, total - (page - 1) * 2000)):
            raise ValueError(f'Truncated catchup page: {response.url}')
        if page == 1:
            for number in range(2, (total + 1999) // 2000 + 1):
                url = urlunsplit(parts._replace(query=urlencode({'abs': 'False', 'page': number})))
                self.expected_pages.add(url)
                yield response.follow(url, callback=self.parse)
        for block in response.css('#dlpage dl'):
            section = block.css('h3::text').get('')
            if not section.startswith(('New submissions', 'Cross submissions')):
                if block.css('dt') and not section.startswith('Replacement submissions'):
                    raise ValueError(f'Unknown catchup section: {section}')
                continue
            for paper in block.css('dt'):
                link = paper.css('a[title="Abstract"]::attr(href)').get()
                description = paper.xpath('following-sibling::dd[1]')
                primary = description.css('.primary-subject::text').get('')
                categories = set(re.findall(r'\(([^)]+)\)', primary))
                if not link or not categories:
                    raise ValueError(f'Incomplete catchup item: {response.url}')
                # Keep the existing primary-category filter; exclude replacements.
                if categories.intersection(self.target_categories):
                    paper_id = link.removeprefix('/abs/')
                    if paper_id not in self.selected_ids:
                        self.selected_ids.add(paper_id)
                        yield {'id': paper_id, 'categories': sorted(categories)}
        self.completed_pages.add(response.url)

    def closed(self, reason):
        if self.stats_file:
            stats = self.crawler.stats.get_stats()
            stats['date'] = self.target_date
            stats['expected_items'] = len(self.selected_ids)
            stats['missing_pages'] = sorted(self.expected_pages - self.completed_pages)
            stats['close_reason'] = reason
            Path(self.stats_file).write_text(json.dumps(stats, default=str), encoding='utf-8')
