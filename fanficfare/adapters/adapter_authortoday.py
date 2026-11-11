# -*- coding: utf-8 -*-

# Standard library imports
from __future__ import absolute_import
import logging
import re
import json
import time
from html import escape
from datetime import datetime

# Third-party imports
global requests, BeautifulSoup
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    requests = None
    BeautifulSoup = None
    import traceback
    logging.getLogger(__name__).error("Failed to import required modules: %s\n%s", 
                                     str(e), traceback.format_exc())

# Local imports
from ..htmlcleanup import stripHTML
from .. import exceptions as exceptions
try:
    from urllib.parse import urlparse
except ImportError:  # pragma: no cover
    from urlparse import urlparse
from .base_adapter import BaseSiteAdapter, makeDate

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = (10, 30)

def getClass():
    return AuthorTodayAdapter

class _AuthorTodayAuthState:
    def __init__(self):
        self.logged_in = False
        self.login_attempts = 0
        self.browser_cache_checked = False
        self.bearer_token = None
        self.token_expires = None
        self.last_login_time = None
        self.user_id = None
        self.session = requests.Session()

class AuthorTodayAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)
        logger.debug("AuthorTodayAdapter instance created for URL: %s", url)
        
        # Check required dependencies
        global requests, BeautifulSoup
        if requests is None:
            raise exceptions.LibraryMissingException('requests')
        if BeautifulSoup is None:
            raise exceptions.LibraryMissingException('beautifulsoup4')
        
        self.username = self.getConfig("username")
        self.password = self.getConfig("password")
        self.is_adult = self.getConfig('is_adult', False)
        self._auth_state = self.__class__._get_auth_state(self.username)
        self.session = self._auth_state.session
        
        logger.debug("Initialized Author.Today session for account %s", self.username or "guest")
        
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0'
        })
        
        # Extract story ID from URL
        m = re.match(self.getSiteURLPattern(), url)
        if m:
            self.story.setMetadata('storyId', m.group('id'))
            
            # normalized story URL
            self._setURL('https://' + self.getSiteDomain() + '/work/' + self.story.getMetadata('storyId'))
        else:
            raise exceptions.InvalidStoryURL(url,
                                          self.getSiteDomain(),
                                          self.getSiteExampleURLs())
        
        # Each adapter needs to have a unique site abbreviation
        self.story.setMetadata('siteabbrev', 'atd')
        
        # The date format will vary from site to site
        self.dateformat = "%Y-%m-%d"
        
        # Добавляем счетчики для всей книги
        self.total_book_images = 0
        self.successful_book_downloads = 0
        self.failed_book_downloads = 0
        self.chapters_processed = 0
        # Добавляем счетчик типов изображений
        self.image_types = {
            'jpeg': 0,
            'png': 0,
            'gif': 0,
            'webp': 0,
            'other': 0
        }

    @staticmethod
    def getSiteDomain():
        return 'author.today'

    @classmethod
    def getSiteExampleURLs(cls):
        return "https://" + cls.getSiteDomain() + "/work/123456"

    def getSiteURLPattern(self):
        return r"https?://" + re.escape(self.getSiteDomain()) + r"/work/(?P<id>\d+)"

    @classmethod
    def _get_auth_state(cls, username):
        if not hasattr(cls, '_auth_states'):
            cls._auth_states = {}
        account_key = username or ''
        if account_key not in cls._auth_states:
            cls._auth_states[account_key] = _AuthorTodayAuthState()
        return cls._auth_states[account_key]

    @property
    def bearer_token(self):
        return self._auth_state.bearer_token

    @bearer_token.setter
    def bearer_token(self, value):
        self._auth_state.bearer_token = value

    @property
    def user_id(self):
        return self._auth_state.user_id

    @user_id.setter
    def user_id(self, value):
        self._auth_state.user_id = value

    def use_pagecache(self):
        return True

    def doExtractChapterUrlsAndMetadata(self, get_cover=True):
        self._get_cover = get_cover
        return self.extractChapterUrlsAndMetadata()

    def performLogin(self, url, data=None):
        """
        Perform login to author.today using bearer token API approach.
        First opens login page in WebView for user authentication,
        then retrieves bearer token using LoginCookie.
        """
        logger.debug("performLogin called. Current _logged_in (class): %s, _login_attempts (class): %d",
                     self._auth_state.logged_in, self._auth_state.login_attempts)
        if self._auth_state.logged_in and self.checkLogin(url): # Проверяем, что токен еще действителен
            logger.debug('Already logged in and token is valid (class attributes).')
            return True
        
        logger.debug("Proceeding with login. _logged_in (class): %s, bearer_token (class): %s, token_expires (class): %s",
                     self._auth_state.logged_in,
                     "present" if self.bearer_token else "absent",
                     self._auth_state.token_expires)
    
        if not self.username:
            raise exceptions.FailedToLogin(url, 'No username set. Please set username in personal.ini')
        if not self.password:
            raise exceptions.FailedToLogin(url, 'No password set. Please set password in personal.ini')
    
        self._auth_state.login_attempts += 1
        if self._auth_state.login_attempts > 3:
            self._auth_state.login_attempts = 0
            raise exceptions.FailedToLogin(url, "Exceeded maximum login attempts (3)")
            
        logger.debug("Starting login process... (Attempt %d/3)", self._auth_state.login_attempts)
    
        try:
            # Try to use browser cache if enabled and not checked yet
            if self.getConfig('use_browser_cache', False) and not self._auth_state.browser_cache_checked:
                logger.debug("Attempting to use browser cache for login (class attribute)")
                self._auth_state.browser_cache_checked = True
                cached_cookie = self.get_browser_cookie()
                if cached_cookie:
                    self.session.cookies.update(cached_cookie)
    
            # First step: Get login page and CSRF token
            login_url = f'https://{self.getSiteDomain()}/account/login'
            logger.debug(f"Fetching login page: {login_url} with timeout=(10, 30)")
            response = self.session.get(login_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            logger.debug(f"Login page GET response status: {response.status_code}")
    
            # Extract CSRF token from the specific login form
            soup = BeautifulSoup(response.text, 'html.parser')
            login_form = soup.find('form', {'action': '/account/login', 'method': 'POST'})
            if not login_form:
                raise exceptions.FailedToLogin(url, 'Could not find login form')
            
            csrf_input = login_form.find('input', {'name': '__RequestVerificationToken'})
            if not csrf_input:
                raise exceptions.FailedToLogin(url, 'Could not find CSRF token in login form')
            csrf_token = csrf_input.get('value')
            logger.debug("CSRF token extracted")
    
            # Perform login to get LoginCookie
            login_data = {
                'Login': self.username,
                'Password': self.password,
                '__RequestVerificationToken': csrf_token,
                'RememberMe': 'true',
                'SendEmailIfNeeded': 'false' # Изменяем значение на 'false'
            }
    
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': f'https://{self.getSiteDomain()}',
                'Referer': login_url,
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/javascript, */*; q=0.01'
            }
    
            logger.debug(f"Posting login data to: {login_url} with timeout=(10, 30)")
            response = self.session.post(login_url, data=login_data, headers=headers,
                                         timeout=REQUEST_TIMEOUT)
            
            logger.debug(f"Login POST response status: {response.status_code}")
            
            response.raise_for_status()
    
            # Check if we got LoginCookie
            if not self.getAuthCookie():
                raise exceptions.FailedToLogin(url, "Login failed - LoginCookie not found")
    
            # Now get the bearer token using LoginCookie
            token_url = f'https://{self.getSiteDomain()}/account/bearer-token'
            logger.debug(f"Fetching bearer token from: {token_url} with timeout=(10, 30)")
            token_response = self.session.get(token_url, timeout=REQUEST_TIMEOUT)
            token_response.raise_for_status()
    
            try:
                token_data = token_response.json()
                if 'token' not in token_data:
                    raise exceptions.FailedToLogin(url, 'Failed to obtain bearer token')
    
                # Store the token and update session headers
                self.bearer_token = token_data['token']
                self.user_id = str(token_data['userId'])
                self._auth_state.token_expires = datetime.strptime(token_data['expires'], "%Y-%m-%dT%H:%M:%SZ")
    
                # Update session headers with bearer token
                self.session.headers.update({
                    'Authorization': f'Bearer {self.bearer_token}'
                })
    
                self._auth_state.logged_in = True
                self._auth_state.login_attempts = 0
                self._auth_state.last_login_time = datetime.utcnow()
                logger.debug("Successfully obtained bearer token. Expires: %s", self._auth_state.token_expires)
                return True
    
            except (ValueError, KeyError) as e:
                logger.error(f"Failed to parse token response: {str(e)}")
                raise exceptions.FailedToLogin(url, f'Failed to parse token response: {str(e)}')
    
        except Exception as e:
            error_msg = str(e)
            if isinstance(e, requests.exceptions.RequestException):
                error_msg = f'Network error during login: {str(e)}'
            elif isinstance(e, exceptions.FailedToLogin):
                error_msg = str(e)
            else:
                error_msg = f'Unexpected error during login: {str(e)}'
            
            logger.error('Login failed: %s', error_msg)
            raise exceptions.FailedToLogin(url, error_msg)

    def checkLogin(self, url):
        """Check if current session is logged in and token is valid."""
        logger.debug("checkLogin called for URL: %s (Story ID: %s). _logged_in (class): %s, bearer_token (class): %s, token_expires (class): %s",
                     url, self.story.getMetadata('storyId'),
                     self._auth_state.logged_in,
                     "present" if self.bearer_token else "absent",
                     self._auth_state.token_expires)
        
        if not self._auth_state.logged_in or not self.bearer_token:
            logger.debug("Not logged in or no bearer token (class attributes).")
            return False
            
        # Check if token has expired
        if self._auth_state.token_expires and datetime.utcnow() > self._auth_state.token_expires:
            logger.debug("Bearer token has expired (class attribute). Resetting login state.")
            self._auth_state.logged_in = False
            self.bearer_token = None
            self._auth_state.token_expires = None
            self.session.headers.pop('Authorization', None)
            return False
            
        # Проверяем, что токен не истечет в ближайшее время (например, через 5 минут)
        if self._auth_state.token_expires and (self._auth_state.token_expires - datetime.utcnow()).total_seconds() < 300:
            logger.debug("Bearer token will expire soon (less than 5 minutes) (class attribute). Resetting login state.")
            self._auth_state.logged_in = False
            self.bearer_token = None
            self._auth_state.token_expires = None
            self.session.headers.pop('Authorization', None)
            return False

        # No longer making a test request to verify token, relying on token_expires
        logger.debug("Token is considered valid based on expiration time (class attribute).")
        return True
            
    def get_browser_cookie(self):
        """Get cached browser cookies if available."""
        try:
            from ..browsercache import get_browser_cookies
            return get_browser_cookies(self.getSiteDomain())
        except:
            return None
            
    def getAuthCookie(self):
        """Check for presence of auth cookie."""
        try:
            cookies = self.session.cookies
            return any(cookie.name in ['LoginCookie', 'ngLoginCookie'] for cookie in cookies)
        except:
            return False

    def decrypt_chapter_text(self, encrypted_text, reader_secret):
        """
        Decrypt the chapter text using the reader secret.
        
        Args:
            encrypted_text (str): The encrypted text from the server
            reader_secret (str): The reader secret key
            
        Returns:
            str: The decrypted text
        """
        logger.debug(f"decrypt_chapter_text called:")
        logger.debug("  reader_secret type: %s", type(reader_secret))
        logger.debug("  encrypted_text type: %s, length: %s",
                 type(encrypted_text), len(encrypted_text) if encrypted_text else 0)
        logger.debug("  user_id available: %s", self.user_id is not None)
        try:
            if not encrypted_text or not reader_secret:
                raise ValueError("Missing encrypted_text or reader_secret")
                
            # Create decryption key by reversing reader_secret and appending user_id
            key = reader_secret[::-1] + "@_@" + (self.user_id or "")
            text_bytes = encrypted_text.encode('utf-16-le', 'surrogatepass')
            key_bytes = key.encode('utf-16-le', 'surrogatepass')
            text_codes = [int.from_bytes(text_bytes[pos:pos + 2], 'little')
                          for pos in range(0, len(text_bytes), 2)]
            key_codes = [int.from_bytes(key_bytes[pos:pos + 2], 'little')
                         for pos in range(0, len(key_bytes), 2)]
            key_len = len(key_codes)
        
            logger.debug("Decrypting text (UTF-16 length: %d) with key (UTF-16 length: %d)",
                         len(text_codes), key_len)
        
            result = bytearray()
            for pos, text_code in enumerate(text_codes):
                key_char = key_codes[pos % key_len]
                result.extend((text_code ^ key_char).to_bytes(2, 'little'))

            decrypted = bytes(result).decode('utf-16-le', 'surrogatepass')
        
            # Проверяем, что расшифрова��ный текст содержит валидный HTML
            if not ('<' in decrypted and '>' in decrypted):
                raise ValueError("Decrypted text does not appear to be valid HTML")
            
            logger.debug("Successfully decrypted %d UTF-16 code units", len(text_codes))
            return decrypted
        
        except Exception:
            logger.exception("Error decrypting chapter text")
            raise
        
    def extractChapterUrlsAndMetadata(self):
        try:
            logger.debug("extractChapterUrlsAndMetadata called. Checking login status...")
            # Attempt to log in if not already logged in or token expired
            if not self.checkLogin(self.url):
                logger.debug("Not logged in or token expired in extractChapterUrlsAndMetadata. Performing login...")
                self.performLogin(self.url)
 
            # Получаем ID книги из URL
            story_id = self.story.getMetadata('storyId')

            # More robust token handling
            if not self.bearer_token:
                logger.warning("Bearer token still not found after login attempt in extractChapterUrlsAndMetadata. Cannot proceed.")
                raise exceptions.StoryDoesNotExist(self.url) # Удален лишний аргумент
 
            # Формируем URL для API запроса
            api_url = f'https://api.author.today/v1/work/{story_id}/details'
            
            # Делаем запрос к API
            headers = {
                'Authorization': f'Bearer {self.bearer_token}'
            }
            
            response = self.session.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            data = response.json()
            
            # Проверка на существование произведения
            if not data or 'id' not in data:
                raise exceptions.StoryDoesNotExist(self.url)
                
            # Проверка на 18+
            if data.get('adultOnly', False) and not self.is_adult:
                raise exceptions.AdultCheckRequired(self.url)

            # Установка метаданных из API
            self.story.setMetadata('title', data.get('title', ''))
            
            # Обложка
            if getattr(self, '_get_cover', True) and data.get('coverUrl'):
                cover_url = data['coverUrl']
                if not cover_url.startswith('http'):
                    cover_url = 'https://' + self.getSiteDomain() + cover_url
                self.setCoverImage(self.url, cover_url)

            # Теги и жанры
            tags = data.get('tags', [])
            if data.get('adultOnly', False):
                tags.append("18+")
            
            for tag in tags:
                self.story.addToList('genre', tag)
                self.story.addToList('tags', tag)
                self.story.addToList('subject', tag)

            # Автор
            self.story.setMetadata('author', data.get('authorFIO', ''))
            self.story.setMetadata('authorId', str(data.get('authorId', '')))
            self.story.setMetadata('authorUrl', f'https://{self.getSiteDomain()}/u/{data.get("authorUserName", "")}')

            # Описание
            annotation = data.get('annotation', '')
            if annotation:
                # Создаем soup объект только если есть аннотация
                soup = self.make_soup(annotation)
                self.story.setMetadata('description', stripHTML(annotation))
            else:
                self.story.setMetadata('description', '')

            # Статус
            self.story.setMetadata('status', 'Completed' if data.get('isFinished', False) else 'In-Progress')

            # Количество слов
            if 'textLength' in data:
                self.story.setMetadata('numWords', str(data['textLength']))

            # Даты
            if 'lastUpdateTime' in data:
                update_date = data['lastUpdateTime'].split('T')[0]
                self.story.setMetadata('dateUpdated', makeDate(update_date, self.dateformat))
                
            if 'lastModificationTime' in data:
                pub_date = data['lastModificationTime'].split('T')[0]
                self.story.setMetadata('datePublished', makeDate(pub_date, self.dateformat))

            # Дополнительные метаданные
            if data.get('seriesTitle'):
                self.story.setMetadata('series', data['seriesTitle'])
                if data.get('seriesOrder'):
                    self.story.setMetadata('seriesIndex', data['seriesOrder'])

            # Получение списка глав через отдельный API-запрос
            chapters_url = f'https://api.author.today/v1/work/{story_id}/content'
            chapters_response = self.session.get(chapters_url, headers=headers,
                                                 timeout=REQUEST_TIMEOUT)
            chapters_response.raise_for_status()
            chapters_data = chapters_response.json()

            if not chapters_data:
                # Если глав нт - одноглавая история
                self.add_chapter('Chapter 1', self.url)
                self.story.setMetadata('numChapters', 1)
                return

            # Получаем список глав
            chapters = []
            for chapter in chapters_data:
                chapter_title = chapter.get('title', '')
                chapter_url = f'https://{self.getSiteDomain()}/reader/{story_id}/{chapter["id"]}'
                chapters.append((chapter_title, chapter_url))

            # Добавляем основные главы
            for title, url in chapters:
                self.add_chapter(title, url)

            # Проверяем наличие изображений в галерее
            if data.get('galleryImages'):
                gallery_images = data['galleryImages']
                if gallery_images:
                    # Сохраняем изображения галереи для возможного обновления
                    self.gallery_images = gallery_images
                    
                    # Download gallery images only when the gallery chapter is fetched.
                    gallery_chapter_title = "Доп. материалы"
                    gallery_url = f"{self.url}#gallery"
                    self.add_chapter(gallery_chapter_title, gallery_url)
                    logger.debug("Added gallery chapter")

            # Отладочная информация об изображениях в ImageStore
            img_store_images = self.story.getImgUrls()
            logger.debug(f"Images in ImageStore: {len(img_store_images)}")
            for i, img in enumerate(img_store_images):
                logger.debug(f"  Image {i}: {img.get('newsrc', 'no newsrc')} -> {img.get('url', 'no url')}")

            # Обновляем количество глав
            total_chapters = len(chapters) + (1 if data.get('galleryImages') else 0)
            self.story.setMetadata('numChapters', total_chapters)
            logger.debug(f"Total chapters: {total_chapters} (including gallery)")

            return
            
        except Exception as e:
            logger.error("Error in extractChapterUrlsAndMetadata: %s", e)
            raise

    def get_request(self, url, **kwargs):
        """
        Overridden get_request to add cache monitoring
        """
        logger.debug("Fetching URL: %s (use_basic_cache: %s)" % 
                    (url, self.getConfig('use_basic_cache')))
        
        response = super().get_request(url, **kwargs)
        
        logger.debug("Response received for: %s (length: %s)" % 
                    (url, len(response) if response else 'None'))
        
        return response

    def getChapterTextNum(self, url, index):
        """Override to handle gallery chapter updates during -u flag"""
        # Если это глава галереи и есть старые главы (обновление), то принудительно обновляем галерею
        if url and '#gallery' in url and (self.oldchapters or self.oldchaptersmap):
            logger.debug("Force updating gallery chapter during update - regenerating gallery content")
            # Принудительно пересоздаем галерею
            if hasattr(self, 'gallery_images') and self.gallery_images:
                gallery_html = self.make_gallery_chapter(self.gallery_images)
                self.chapter_gallery_content = gallery_html
                logger.debug("Gallery chapter content refreshed for update")
        
        return self.getChapterText(url)

    def hasGalleryForUpdate(self):
        """Check if this story has a gallery that should be updated"""
        has_gallery = hasattr(self, 'gallery_images') and self.gallery_images and len(self.gallery_images) > 0
        logger.debug(f"hasGalleryForUpdate check: hasattr(gallery_images)={hasattr(self, 'gallery_images')}, gallery_images={getattr(self, 'gallery_images', None)}, result={has_gallery}")
        return has_gallery

    def getChapterText(self, url):
        """Get chapter text using Author.Today API"""
        logger.debug('Getting chapter text from: %s' % url)
        
        if url and '#gallery' in url:
            if not hasattr(self, 'chapter_gallery_content') and getattr(self, 'gallery_images', None):
                self.chapter_gallery_content = self.make_gallery_chapter(self.gallery_images)
            if hasattr(self, 'chapter_gallery_content'):
                logger.debug("Returning gallery chapter content")
                return self.chapter_gallery_content
        
        self.chapters_processed += 1
        
        logger.debug("getChapterText called for URL: %s. Checking login status...", url)
        # Ensure we're logged in before proceeding
        if not self.checkLogin(url):
            logger.debug("Not logged in or token expired in getChapterText. Performing login...")
            self.performLogin(url)
        
        # Extract work_id and chapter_id from URL
        match = re.match(r'.*?/reader/(\d+)/(\d+)', url)
        if not match:
            # Try alternate URL pattern
            match = re.match(r'.*?/work/(\d+)(?:/chapter/(\d+))?', url)
            if not match:
                raise ValueError('Failed to extract IDs from chapter URL')
                
        work_id = match.group(1)
        chapter_id = match.group(2) if match.group(2) else "1"  # Default to first chapter
        
        try:
            # Ensure we have bearer token
            if not self.bearer_token:
                logger.error("No bearer token available (class attribute)")
                return ""
    
            # Формируем URL запроса, как в JavaScript скрипте
            chapter_request_url = f'https://{self.getSiteDomain()}/reader/{work_id}/chapter'
            params = {
                'id': chapter_id,
                '_': int(time.time() * 1000) # Добавляем метку времени в миллисекундах
            }

            # Set headers similar to JavaScript, including Authorization
            headers = {
                'Authorization': f'Bearer {self.bearer_token}',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Origin': f'https://{self.getSiteDomain()}',
                'Referer': f'https://{self.getSiteDomain()}/reader/{work_id}',
                'User-Agent': self.session.headers['User-Agent'] # Используем User-Agent из сессии
            }

            # Get chapter content
            response = self.session.get(chapter_request_url, headers=headers, params=params,
                                        timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            # Get reader_secret from headers
            reader_secret = response.headers.get('reader-secret')
            if not reader_secret:
                raise ValueError('No reader-secret header found in chapter response')

            try:
                json_data = response.json()
            except json.JSONDecodeError as e:
                raise ValueError('Failed to decode chapter response JSON') from e

            # Check if response indicates success and contains text data
            if not json_data.get('isSuccessful') or not json_data.get('data') or 'text' not in json_data['data']:
                raise ValueError('Chapter response contains no text')

            # Decrypt chapter content using reader_secret from headers
            decrypted_text = self.decrypt_chapter_text(json_data['data']['text'], reader_secret)
            if not decrypted_text:
                raise ValueError('Decryption returned empty chapter text')
            
            # Parse the decrypted HTML
            chapter_soup = self.make_soup(decrypted_text)
            
            # Инициализация счетчиков для главы
            total_images = 0
            valid_urls = 0
            download_attempts = 0
            successful_downloads = 0
            failed_downloads = 0
            chapter_image_types = {
                'jpeg': 0,
                'png': 0,
                'gif': 0,
                'webp': 0,
                'other': 0
            }
            
            # Поиск изображений
            images = chapter_soup.find_all('img')
            total_images = len(images)
            self.total_book_images += total_images
            
            logger.info(f"\n=== Chapter {self.chapters_processed} Image Processing Statistics ===")
            logger.info(f"Images found in this chapter: {total_images}")
            
            # Обработка изображений
            if self.getConfig('extract_images', False):
                logger.debug("Image extraction is enabled")
                
                for idx, img in enumerate(images, 1):
                    if img.get('src'):
                        img_url = img['src']
                        valid_urls += 1
                        logger.debug(f"\nProcessing image {idx}/{total_images}: {img_url}")
                        
                        if img_url.startswith("https://cm.author.today/"):
                            download_attempts += 1
                            try:
                                logger.debug(f"Attempting to download image: {img_url}")
                                img_data = self.download_image(img_url)
                                
                                if img_data:
                                    successful_downloads += 1
                                    self.successful_book_downloads += 1
                                    
                                    # Определение типа изображения по content-type
                                    content_type = response.headers.get('content-type', '').lower()
                                    if 'jpeg' in content_type or 'jpg' in content_type:
                                        image_type = 'jpeg'
                                    elif 'png' in content_type:
                                        image_type = 'png'
                                    elif 'gif' in content_type:
                                        image_type = 'gif'
                                    elif 'webp' in content_type:
                                        image_type = 'webp'
                                    else:
                                        image_type = 'other'
                                    
                                    # Обновляем счетчики типов
                                    chapter_image_types[image_type] += 1
                                    self.image_types[image_type] += 1
                                    
                                    logger.debug(f"Successfully downloaded image {idx} (size: {len(img_data)} bytes, type: {image_type})")
                                else:
                                    failed_downloads += 1
                                    self.failed_book_downloads += 1
                                    logger.warning(f"Failed to download image {idx}")
                            except Exception as e:
                                failed_downloads += 1
                                self.failed_book_downloads += 1
                                logger.error(f"Error processing image {img_url}: {e}")
                        else:
                            logger.debug(f"Skipping non-author.today image URL: {img_url}")
                    else:
                        logger.warning(f"Image tag {idx} has no src attribute")
                
                # Статистика главы
                logger.info("\n=== Chapter Image Processing Statistics ===")
                logger.info(f"Chapter {self.chapters_processed}:")
                logger.info(f"Images found in chapter: {total_images}")
                logger.info(f"Valid URLs found: {valid_urls}")
                logger.info(f"Download attempts: {download_attempts}")
                logger.info(f"Successfully downloaded: {successful_downloads}")
                logger.info(f"Failed downloads: {failed_downloads}")
                if download_attempts > 0:
                    logger.info(f"Chapter success rate: {(successful_downloads/download_attempts*100):.1f}%")
                
                # Статистика типов изображений в главе
                logger.info("\n=== Chapter Image Types ===")
                for img_type, count in chapter_image_types.items():
                    if count > 0:
                        logger.info(f"{img_type.upper()}: {count}")
                
                # Общая статистика книги
                logger.info("\n=== Overall Book Statistics ===")
                logger.info(f"Chapters processed: {self.chapters_processed}")
                logger.info(f"Total images found in book: {self.total_book_images}")
                logger.info(f"Total successfully downloaded: {self.successful_book_downloads}")
                logger.info(f"Total failed downloads: {self.failed_book_downloads}")
                if self.total_book_images > 0:
                    logger.info(f"Overall success rate: {(self.successful_book_downloads/self.total_book_images*100):.1f}%")
                
                # Общая статистика типов изображений
                logger.info("\n=== Overall Image Types Statistics ===")
                for img_type, count in self.image_types.items():
                    if count > 0:
                        logger.info(f"{img_type.upper()}: {count}")
                        percentage = (count / self.successful_book_downloads * 100) if self.successful_book_downloads > 0 else 0
                        logger.info(f"{img_type.upper()} percentage: {percentage:.1f}%")
                
                logger.info("=====================================\n")
            else:
                logger.debug("Image extraction is disabled in configuration")
            
            return self.utf8FromSoup(url, chapter_soup)
            
        except Exception:
            logger.exception("Error getting chapter text")
            raise
            
    def download_image(self, img_url, headers=None):
        """Загрузка изображения с расширенным логированием"""
        logger.debug(f"\n=== Starting image download ===")
        logger.debug(f"URL: {img_url}")
        
        # Проверка URL
        if not img_url or not img_url.startswith("https://cm.author.today/"):
            logger.error(f"Invalid image URL: {img_url}")
            return None
        
        # Подготовка заголовков
        request_headers = {
            'Authorization': f'Bearer {self.bearer_token}' if self.bearer_token else '',
            'Accept': 'image/*',
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://' + self.getSiteDomain(),
            'Origin': 'https://' + self.getSiteDomain()
        }
        if headers:
            request_headers.update(headers)
        
        try:
            logger.debug("Sending HTTP request...")
            response = self.session.get(img_url, headers=request_headers, timeout=REQUEST_TIMEOUT)
            
            logger.debug(f"Response status code: {response.status_code}")
            
            response.raise_for_status()
            
            content_length = len(response.content)
            logger.debug(f"Downloaded content length: {content_length} bytes")
            
            if content_length < 100:  # Минимальный размер изображения
                logger.warning(f"Suspicious image size: {content_length} bytes")
                return None
            
            # Проверка типа контента
            content_type = response.headers.get('content-type', '')
            logger.debug(f"Content type: {content_type}")
            
            if not any(img_type in content_type.lower() for img_type in ['image/jpeg', 'image/png', 'image/gif', 'image/webp']):
                logger.warning(f"Unexpected content type: {content_type}")
                return None
            
            logger.debug("Image download successful")
            return response.content
            
        except Exception as e:
            logger.error(f"Error downloading image: {e}")
            logger.exception("Full traceback:")
            return None

    def make_gallery_chapter(self, gallery_images):
        """Создает HTML-контент для главы с галереей"""
        html = ['<div class="gallery-chapter">']
        
        def fetch_image(url, referer=None, image=False):
            """Функция для загрузки изображения с поддержкой referer"""
            # Принудительно загружаем изображение напрямую, игнорируя кэш
            # чтобы гарантировать загрузку всех изображений галереи
            return self.get_request_raw(url, referer=referer, image=image)
        
        for idx, img in enumerate(gallery_images, 1):
            img_url = img.get('url')
            if not img_url:
                continue

            caption = img.get('caption') or ''
            caption = str(caption)

            try:
                image_src, _ = self.story.addImgUrl(
                    parenturl=self.url,
                    url=img_url,
                    cover=False,
                    fetch=fetch_image
                )
                if not image_src or image_src.startswith('failedtoload'):
                    self.failed_book_downloads += 1
                    continue

                html.append('<div class="gallery-item">')
                html.append('<img src="%s" alt="%s"/>' % (
                    escape(image_src, quote=True), escape(caption, quote=True)))
                if caption:
                    html.append('<div class="gallery-caption"><p>%s</p></div>' % escape(caption))
                html.append('</div>')

                self.successful_book_downloads += 1
                logger.debug("Added gallery image %d/%d", idx, len(gallery_images))

            except Exception as e:
                self.failed_book_downloads += 1
                logger.warning("Error processing gallery image %s: %s", img_url, e)
        
        html.append('</div>')
        
        css = """
        <style>
            .gallery-chapter {
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 2em;
                padding: 1em;
            }
            .gallery-item {
                max-width: 100%;
                text-align: center;
            }
            .gallery-item img {
                max-width: 100%;
                height: auto;
            }
            .gallery-caption {
                margin-top: 0.5em;
                font-style: italic;
                color: #666;
            }
        </style>
        """
        
        return css + '\n'.join(html)