import feedparser
import requests
from datetime import datetime
import time
import logging
from typing import List, Dict, Any, Optional
import hashlib
from bs4 import BeautifulSoup # Import BeautifulSoup
import pytz  # 添加时区支持
from .news_db_manager import NewsDBManager
from .config_manager import load_config
from .wechat_parser import WeChatParser
# Import the normalization function
from .news_db_manager import NewsDBManager
import re # Add re import for whitespace normalization

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("rss_parser")

class RssParser:
    """RSS Feed解析器，用于获取和处理RSS Feed的内容"""
    
    def __init__(self, user_agent: str = "NeuroFeed RSS Reader/1.0"):
        """初始化RSS解析器
        
        Args:
            user_agent: 请求头中的User-Agent字段
        """
        self.user_agent = user_agent
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.db_manager = NewsDBManager()
        # Get the normalization function instance
        self.normalize_article_id = self.db_manager.normalize_article_id
        self.wechat_parser = WeChatParser()  # Initialize the WeChat parser
        
        # 从配置加载是否跳过已处理文章的设置（初始值）
        config = load_config()
        # 修复：更正配置路径访问方式
        self.skip_processed = config.get("global_settings", {}).get("general_settings", {}).get("skip_processed_articles", False)
        logger.info(f"初始化时 - 跳过已处理文章: {'是' if self.skip_processed else '否'}")
        logger.debug(f"配置内容: {config}")
        
        # 从配置加载时区处理设置
        general_settings = config.get("global_settings", {}).get("general_settings", {})
        self.assume_utc = False  # 修正：无时区信息的日期不应假定为UTC
        logger.info(f"无时区信息时将保留原始时间（假定为本地时间）")
    
    def refresh_settings(self):
        """刷新配置设置，确保使用最新的配置值"""
        try:
            config = load_config()
            # 修复：更正配置路径访问方式
            prev_setting = self.skip_processed
            self.skip_processed = config.get("global_settings", {}).get("general_settings", {}).get("skip_processed_articles", False)
            
            logger.info(f"刷新设置 - 跳过已处理文章: {'是' if self.skip_processed else '否'}")
            logger.debug(f"设置变化: {prev_setting} -> {self.skip_processed}")
            logger.debug(f"配置结构: {config.get('global_settings', {}).get('general_settings', {})}")
            
            # 刷新时区处理设置
            general_settings = config.get("global_settings", {}).get("general_settings", {})
            self.assume_utc = False  # 修正：无时区信息的日期不应假定为UTC
            
            logger.info(f"无时区信息时将保留原始时间（假定为本地时间）")
            
            # 通过显式返回布尔值避免任何转换问题
            return self.skip_processed is True
        except Exception as e:
            logger.error(f"刷新设置时出错: {e}")
            return False
    
    def _convert_to_local_time(self, dt: datetime) -> datetime:
        """
        只对有时区信息的日期进行转换，没有时区信息的保持原样
        
        Args:
            dt: 输入的datetime对象
            
        Returns:
            datetime: 转换后的datetime对象
        """
        if dt is None:
            return None
            
        # 只有当datetime有时区信息时才进行转换
        if dt.tzinfo is not None:
            # 获取本地时区
            local_tz = datetime.now().astimezone().tzinfo
            # 转换到本地时区并返回
            logger.debug(f"转换有时区信息的时间 {dt} 到本地时区")
            return dt.astimezone(local_tz)
            
        # feedparser解析的时间通常是UTC时间，但没有时区信息
        # 这里做一个明确的假设：无时区信息的feedparser时间是UTC时间
        logger.debug(f"时间 {dt} 无时区信息，假定为UTC时间并转换为本地时间")
        utc_dt = dt.replace(tzinfo=pytz.UTC)
        return utc_dt.astimezone(datetime.now().astimezone().tzinfo)

    def _clean_html(self, html_content: str) -> str:
        """
        Removes HTML tags from a string and normalizes whitespace.

        Args:
            html_content: The HTML string.

        Returns:
            Plain text string.
        """
        if not html_content:
            return ""
        try:
            # First, normalize any <em> tags by removing them but preserving their content without adding spaces
            # This needs to happen before BeautifulSoup parsing to avoid extra whitespace
            html_content = re.sub(r'<em>([^<]*)</em>', r'\1', html_content)
            
            soup = BeautifulSoup(html_content, "html.parser")
            
            # Still unwrap any remaining <em> tags (in case regex didn't catch them all)
            for em_tag in soup.find_all('em'):
                em_tag.unwrap()
                
            # Get text, using single newline as a basic separator
            raw_text = soup.get_text(separator='\n', strip=True)
            
            processed_lines = []
            for line in raw_text.splitlines():
                stripped_line = line.strip() # Strip leading/trailing whitespace from the line
                if stripped_line: # Only process non-empty lines
                    # First normalize multiple spaces within the line to a single space
                    normalized_line = re.sub(r'\s+', ' ', stripped_line)
                    processed_lines.append(normalized_line)
            
            # Join processed lines with double newlines to represent paragraphs
            cleaned_text = '\n\n'.join(processed_lines)
            return cleaned_text
        except Exception as e:
            logger.warning(f"Error cleaning HTML: {e}. Returning original content.")
            # Return original content if cleaning fails
            return html_content
    
    def fetch_feed(self, feed_url: str, items_count: int = 10, task_id: str = None, recipients: List[str] = None) -> Dict[str, Any]:
        """获取RSS Feed内容
        
        Args:
            feed_url: RSS Feed的URL
            items_count: 要获取的条目数量
            task_id: 当前执行的任务ID（用于跳过被该任务丢弃或已发送的文章）
            recipients: 当前任务的收件人列表（用于检查是否所有人都收到过）
        """
        try:
            # 每次获取Feed前刷新配置
            self.refresh_settings()
            
            logger.info(f"\n============ 开始获取Feed ============")
            logger.info(f"Feed URL: {feed_url}")
            logger.info(f"计划获取条目数量: {items_count}")
            logger.info(f"跳过已处理文章: {'是' if self.skip_processed else '否'}")
            if task_id:
                logger.info(f"当前任务ID: {task_id}")
            if recipients:
                logger.info(f"当前收件人: {recipients}")
            start_time = time.time()
            
            # Check if this is a WeChat source that needs special handling
            is_wechat_source = "WXS_" in feed_url or "weixin" in feed_url
            
            # Use special handling for WeChat sources
            if is_wechat_source:
                logger.info(f"Detected WeChat source, using specialized parser: {feed_url}")
                wechat_result = self.wechat_parser.parse_wechat_source(feed_url, items_count)
                
                # If WeChat parsing failed, return the error
                if wechat_result["status"] != "success":
                    return wechat_result
                
                # Process WeChat items and store them in the database
                processed_entries = []
                skipped_count = 0
                total_entries = len(wechat_result["items"])
                
                for entry in wechat_result["items"]:
                    # Generate a unique ID for this WeChat article
                    title = entry.get('title', 'No Title')
                    original_link = entry.get('link', feed_url)
                    # Normalize the link BEFORE hashing
                    normalized_link = self.normalize_article_id(original_link)
                    
                    # Use normalized link + title as article_id
                    # Ensure consistent encoding and hashing
                    id_string = f"{normalized_link}::{title}" # Use a separator just in case
                    article_id = f"wechat_{hashlib.md5(id_string.encode('utf-8')).hexdigest()}"
                    
                    # Check if we should skip this article using the generated article_id
                    skip_article = False
                    skip_reason = ""
                    
                    if self.skip_processed and task_id: # Ensure task_id is available for checks
                        # Check using the consistently generated article_id
                        if self.db_manager.is_article_discarded_for_task(article_id, task_id):
                            skip_article = True
                            skip_reason = f"在任务 {task_id} 中被丢弃过"
                        elif self.db_manager.is_article_sent_for_task(article_id, task_id):
                            skip_article = True
                            skip_reason = f"在任务 {task_id} 中已发送过"
                    
                    if skip_article:
                        skipped_count += 1
                        logger.info(f"跳过微信文章: {title} (ID: {article_id}) - 原因: {skip_reason}")
                        continue
                    
                    # Generate content hash
                    content = entry.get('content', '')
                    content_hash = hashlib.md5(content.encode()).hexdigest() if content else None
                    
                    # Store in database using the generated article_id
                    self.db_manager.add_news_article(
                        article_id=article_id, # Use the generated ID
                        title=title,
                        link=original_link, # Store original link for display
                        source=entry.get('source', '微信公众号'),
                        published_date=entry.get('published', datetime.now().isoformat()),
                        content_hash=content_hash
                    )
                    
                    # Add article_id to entry
                    entry["article_id"] = article_id
                    processed_entries.append(entry)
                
                elapsed_time = time.time() - start_time
                logger.info(f"\n============ WeChat Feed获取完成 ============")
                logger.info(f"Feed URL: {feed_url}")
                logger.info(f"获取条目数: {len(processed_entries)}")
                logger.info(f"耗时: {elapsed_time:.2f}秒")
                
                return {
                    "status": "success",
                    "items": processed_entries,
                    "feed_info": {
                        "title": processed_entries[0].get('source', '微信公众号') if processed_entries else "未知",
                        "description": "微信公众号内容",
                        "link": feed_url
                    },
                    "stats": {
                        "total_available": total_entries,
                        "processed": len(processed_entries),
                        "skipped": skipped_count
                    }
                }
            
            # 使用feedparser解析RSS Feed
            logger.info(f"解析RSS Feed: {feed_url}")
            feed = feedparser.parse(feed_url)
            
            # 检查Feed是否有效
            if not feed:
                logger.warning(f"Feed无效: {feed_url}")
                return {
                    "status": "fail",
                    "error": "无效的Feed",
                    "items": []
                }
            
            if not hasattr(feed, 'entries'):
                logger.warning(f"Feed没有entries属性: {feed_url}")
                return {
                    "status": "fail",
                    "error": "Feed结构无效",
                    "items": []
                }
            
            if not feed.entries:
                logger.warning(f"Feed没有条目: {feed_url}")
                return {
                    "status": "fail",
                    "error": "Feed为空",
                    "items": []
                }
            
            # 获取指定数量的条目
            total_entries = len(feed.entries)
            logger.info(f"Feed包含 {total_entries} 条原始条目")
            
            # 处理每个条目
            logger.info(f"\n============ 处理Feed条目 ============")
            
            processed_entries = []
            skipped_count = 0
            entry_index = 0
            
            # 处理所有条目，直到达到所需数量或遍历完所有条目
            while len(processed_entries) < items_count and entry_index < total_entries:
                if entry_index >= len(feed.entries):
                    break
                    
                entry = feed.entries[entry_index]
                entry_index += 1
                
                # 获取原始唯一标识符 (id or link)
                original_article_id_source = getattr(entry, 'id', None)
                original_link = getattr(entry, 'link', None)

                # Determine the base ID: prefer 'id', fallback to 'link'
                base_id = original_article_id_source if original_article_id_source else original_link

                if not base_id:
                     title_for_log = getattr(entry, 'title', f"Entry #{entry_index}")
                     logger.warning(f"无法为文章 '{title_for_log}' 获取 'id' 或 'link'，跳过此条目。")
                     continue # Skip this entry if no identifier found

                # Normalize the chosen identifier *before* using it for checks or storage
                article_id = self.normalize_article_id(base_id)
                
                # 增强版的跳过逻辑 using the normalized article_id
                skip_article = False
                skip_reason = ""
                
                if self.skip_processed and task_id: # Ensure task_id is available for checks
                    # Check using the normalized article_id
                    if self.db_manager.is_article_discarded_for_task(article_id, task_id):
                        skip_article = True
                        skip_reason = f"在任务 {task_id} 中被丢弃过"
                    elif self.db_manager.is_article_sent_for_task(article_id, task_id):
                        skip_article = True
                        skip_reason = f"在任务 {task_id} 中已发送过"
                
                if skip_article:
                    skipped_count += 1
                    title = getattr(entry, 'title', article_id)
                    logger.info(f"跳过文章 #{entry_index}: {title} (ID: {article_id}) - 原因: {skip_reason}")
                    continue
                
                # 提取发布日期，如果存在
                published_date = None
                pub_datetime = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_datetime = datetime(*entry.published_parsed[:6])
                    # feedparser解析的时间是UTC时间，但没有时区信息
                    # 明确添加UTC时区信息后再转换到本地时间
                    pub_datetime = pub_datetime.replace(tzinfo=pytz.UTC)
                    pub_datetime = self._convert_to_local_time(pub_datetime)
                    published_date = pub_datetime.isoformat()
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_datetime = datetime(*entry.updated_parsed[:6])
                    # 同样，明确添加UTC时区信息后再转换
                    pub_datetime = pub_datetime.replace(tzinfo=pytz.UTC)
                    pub_datetime = self._convert_to_local_time(pub_datetime)
                    published_date = pub_datetime.isoformat()
                
                # 获取标题和链接
                title = entry.title if hasattr(entry, 'title') else "无标题"
                link = entry.link if hasattr(entry, 'link') else ""
                logger.info(f"处理条目 #{len(processed_entries)+1} (总索引 #{entry_index}): {title}")
                
                # 获取摘要 (原始HTML)
                raw_summary = entry.summary if hasattr(entry, 'summary') else ""
                # 清理摘要HTML
                cleaned_summary = self._clean_html(raw_summary)
                
                # 获取内容 (原始HTML)
                raw_content = entry.content[0].value if hasattr(entry, 'content') and entry.content else raw_summary
                # 清理内容HTML
                cleaned_content = self._clean_html(raw_content)
                
                # 构建条目字典，使用清理后的文本
                processed_entry = {
                    "title": title,
                    "link": original_link, # Use original link for display/output
                    "summary": cleaned_summary, # Use cleaned summary
                    "published": published_date,
                    "source": feed.feed.title if hasattr(feed, 'feed') and hasattr(feed.feed, 'title') else feed_url,
                    "content": cleaned_content, # Use cleaned content
                    "article_id": article_id,  # Use the normalized article_id
                    "feed_url": feed_url  # 添加feed_url以便后续获取标签
                }
                
                # 记录清理后内容长度
                content_len = len(processed_entry["content"])
                summary_len = len(processed_entry["summary"])
                logger.info(f"条目清理后摘要长度: {summary_len} 字符")
                logger.info(f"条目清理后内容长度: {content_len} 字符")
                
                processed_entries.append(processed_entry)
                
                # Generate content hash using the *cleaned* content if available, else cleaned summary
                content_hash = None
                content_to_hash = cleaned_content if cleaned_content else cleaned_summary
                if content_to_hash:
                    content_hash = hashlib.md5(content_to_hash.encode('utf-8')).hexdigest()
                
                # Store in database using the normalized article_id
                self.db_manager.add_news_article(
                    article_id=article_id, # Use the normalized ID
                    title=entry.title,
                    link=original_link, # Store original link
                    source=feed.feed.title if hasattr(feed, 'feed') and hasattr(feed.feed, 'title') else feed_url,
                    published_date=published_date,
                    content_hash=content_hash # Use hash of cleaned content
                )
            
            # 如果启用了跳过文章功能，记录详细的统计信息
            if self.skip_processed:
                logger.info(f"\n============ 跳过已处理文章统计 ============")
                logger.info(f"Feed包含的总条目数: {total_entries}")
                logger.info(f"跳过的已处理文章数: {skipped_count}")
                logger.info(f"成功获取的新文章数: {len(processed_entries)}")
                
                # 如果获取的文章数少于要求数量，记录原因
                if len(processed_entries) < items_count:
                    logger.info(f"注意: 获取的新文章数 ({len(processed_entries)}) 少于计划数量 ({items_count})")
                    logger.info(f"原因: Feed中所有条目都已处理完毕或没有足够的新文章")
            
            elapsed_time = time.time() - start_time
            logger.info(f"\n============ Feed获取完成 ============")
            logger.info(f"Feed URL: {feed_url}")
            logger.info(f"获取条目数: {len(processed_entries)}")
            logger.info(f"耗时: {elapsed_time:.2f}秒")
            
            return {
                "status": "success",
                "items": processed_entries,
                "feed_info": {
                    "title": feed.feed.title if hasattr(feed, 'feed') and hasattr(feed.feed, 'title') else "未知",
                    "description": feed.feed.description if hasattr(feed, 'feed') and hasattr(feed.feed, 'description') else "无描述",
                    "link": feed.feed.link if hasattr(feed, 'feed') and hasattr(feed.feed, 'link') else feed_url
                },
                "stats": {
                    "total_available": total_entries,
                    "processed": len(processed_entries),
                    "skipped": skipped_count
                }
            }
        
        except Exception as e:
            import traceback
            logger.error(f"\n============ Feed获取失败 ============")
            logger.error(f"Feed URL: {feed_url}")
            logger.error(f"错误类型: {type(e).__name__}")
            logger.error(f"错误信息: {str(e)}")
            logger.error(f"详细追踪:\n{traceback.format_exc()}")
            return {
                "status": "fail",
                "error": str(e),
                "items": []
            }

    def fetch_multiple_feeds(self, feed_configs: List[Dict[str, Any]], task_id: str = None, recipients: List[str] = None) -> Dict[str, Dict[str, Any]]:
        """批量获取多个RSS Feed
        
        Args:
            feed_configs: 包含Feed URL和配置的字典列表
                每个字典应包含'url'和'items_count'
            task_id: 当前执行的任务ID
            recipients: 当前任务的收件人列表
                
        Returns:
            URL到Feed结果的映射字典
        """
        results = {}
        
        for config in feed_configs:
            url = config.get('url')
            items_count = config.get('items_count', 10)
            
            if not url:
                continue
                
            result = self.fetch_feed(url, items_count, task_id, recipients)
            results[url] = result
            
            # 添加一个小延迟，避免过快请求
            time.sleep(0.5)
        
        return results
