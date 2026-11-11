#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2023 FanFicFare team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import absolute_import
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
import time
import urllib.parse
import requests

from .. import exceptions
from .base_adapter import BaseSiteAdapter

def getClass():
    return TLRulateRuAdapter

class TLRulateRuAdapter(BaseSiteAdapter):

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)
        
        self.story.setMetadata('siteabbrev','tlru')
        
        # get storyId from url
        # https://tl.rulate.ru/book/XXX
        self.story.setMetadata('storyId',self.parsedUrl.path.split('/')[2])
        
        # normalized story URL.
        self._setURL('https://' + self.getSiteDomain() + '/book/' + self.story.getMetadata('storyId'))
        
        self._is_logged_in = False
        self._login_attempts = 0
        self._total_requests = 0
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        })
        logger.debug("Initialized tl.rulate.ru session for %s", self.url)

    @staticmethod
    def getSiteDomain():
        return 'tl.rulate.ru'

    @classmethod
    def getSiteExampleURLs(cls):
        return 'https://' + cls.getSiteDomain() + '/book/12345'

    def getSiteURLPattern(self):
        return r'https?://' + re.escape(self.getSiteDomain()) + r'/book/\d+/?$'

    def has_login_credentials(self):
        username = self.getConfig('username', self.username)
        password = self.getConfig('password', self.password)
        return bool(username and username != 'NoneGiven' and password)

    def extractChapterUrlsAndMetadata(self):
        # Проверяем авторизацию перед извлечением данных
        if self.has_login_credentials() and not self._is_logged_in:
            logger.debug("Not logged in; attempting authentication")
            if not self.login():
                raise Exception("Failed to login to tl.rulate.ru")
            logger.debug("Successfully authenticated with tl.rulate.ru")
        elif self._is_logged_in:
            logger.debug("Already authenticated with tl.rulate.ru")
        else:
            logger.debug("No login credentials configured; using guest access")
                
        url = self.url
        logger.debug("URL: "+url)

        data = self.get_request(url)
        soup = self.make_soup(data)

        # Выводим HTML для отладки
        #print("Initial HTML content:")
        #print(soup.prettify())

        # Проверяем наличие формы подтверждения возраста
        age_form = soup.find('div', class_='errorpage')
        if age_form and "старше 18 лет" in age_form.get_text():
            logger.debug("Found age verification page; submitting confirmation")
            
            # Находим форму и её элементы
            form = age_form.find('form')
            path_input = form.find('input', {'name': 'path'}) if form else None
            if path_input and path_input.get('value'):
                # Получаем данные формы
                data = {
                    'path': path_input['value'],
                    'ok': 'Да'
                }
                
                # Отправляем POST-запрос на адрес формы
                self.post_request('https://tl.rulate.ru/mature', data)
                
                # После подтверждения возраста делаем новый запрос к странице книги
                response = self.get_request(self.url)
                soup = self.make_soup(response)
            
        # Теперь продолжаем поиск заголовка на странице
        title = soup.find('h1')
        if not title:
            logger.warning("Story title not found at %s", url)
            raise Exception('Story title not found!')

        # Извлекаем дополнительное название из тега small, если он есть
        small_tag = title.find('small')
        additional_title = small_tag.get_text().strip() if small_tag else None

        # Название может быть обернуто во вложенный тег внутри h1.
        main_title = ''.join(title.find_all(string=True, recursive=False)).strip()
        if not main_title:
            main_title = title.get_text(' ', strip=True)
            if additional_title and main_title.endswith(additional_title):
                main_title = main_title[:-len(additional_title)].rstrip(' /-')

        # Объединяем названия с разделителем, если есть дополнительное название
        # Объединяем названия с разделителем, если есть дополнительное название и оно отличается от основного
        if additional_title and additional_title != main_title:
            full_title = f"{main_title} / {additional_title}"
        else:
            full_title = main_title

        self.story.setMetadata('title', full_title)

        # Extract cover
        cover_images = soup.select(".images img")
        if not cover_images:
            cover_images = soup.select(".book-thumbnail img")
            
        for i, cover_img in enumerate(cover_images):
            cover_url = cover_img.get('data-src') or cover_img.get('src')
            if not cover_url:
                continue
                
            if not cover_url.startswith('http'):
                if cover_url.startswith('//'):
                    cover_url = 'https:' + cover_url
                else:
                    cover_url = 'https://' + self.getSiteDomain() + ('/' if not cover_url.startswith('/') else '') + cover_url
            
            # Download and save cover
            try:
                if i == 0:
                    self.setCoverImage(url, cover_url)
                else:
                    self.story.addImgUrl(url, cover_url, self.get_request_raw)
            except Exception as e:
                logger.warning(f"Failed to get cover {i+1}: {str(e)}")

        # Extract author
        author = soup.find('strong', text=re.compile(r'Автор:')).find_next('em').get_text().strip() if soup.find('strong', text=re.compile(r'Автор:')) else None
        
        if author:
            self.story.addToList('author', author)
            self.story.addToList('authorId', author)  # Using author name as ID since we don't have specific IDs
            self.story.setMetadata('authorUrl', 'https://' + self.getSiteDomain() + '/search?t=' + author)
        else:
            # Try to find owner in translation panel
            owner = soup.select_one(".tools>dl.info>dd>a.user[href^='/users/']")
            if owner:
                previous = owner.previous_sibling
                prev_text = (previous.get_text() if hasattr(previous, 'get_text') else str(previous or '')).strip().lower()
                if prev_text.endswith("владелец:"):
                    author = owner.get_text().strip()
                    author_id = owner['href'].split('/')[-1]
                    self.story.addToList('author', author)
                    self.story.addToList('authorId', author_id)
                    self.story.setMetadata('authorUrl', 'https://' + self.getSiteDomain() + owner['href'])
            else:
                self.story.addToList('author', 'Unknown')
                self.story.addToList('authorId', 'unknown')
                self.story.setMetadata('authorUrl', '')

        # Extract description
        description_anchor = soup.find('div', class_='btn-toolbar')
        description = description_anchor.find_next_sibling('div') if description_anchor else None
        if description:
            self.setDescription(url, description)

        # Extract status
        status_text = soup.find('strong', text=re.compile(r'Выпуск:')).find_next('em').get_text().strip() if soup.find('strong', text=re.compile(r'Выпуск:')) else None
        if status_text:
            self.story.setMetadata('status', 'In-Progress' if 'продолжается' in status_text.lower() else 'Completed')

        # Extract rating
        rating_div = soup.find('div', text=re.compile(r'Произведение:'))
        if rating_div and rating_div.find_next_sibling('div'):
            rating = rating_div.find_next_sibling('div').get_text().strip().split('/')[0].strip()
            if rating:
                self.story.setMetadata('rating', rating)

        # Extract genres and tags
        genres_p = soup.find('strong', text='Жанры:')
        if genres_p and genres_p.find_parent('p'):
            genres_em = genres_p.find_parent('p').find('em')
            if genres_em:
                genres = genres_em.find_all('a', class_='badge')
                logger.debug("Found %d genres", len(genres))
                for genre in genres:
                    genre_text = genre.get_text().strip()
                    self.story.addToList('genre', genre_text)

        tags_p = soup.find('strong', text='Тэги:')
        if tags_p and tags_p.find_parent('p'):
            tags_em = tags_p.find_parent('p').find('em')
            if tags_em:
                tags = tags_em.find_all('a', class_='badge')
                logger.debug("Found %d tags", len(tags))
                for tag in tags:
                    tag_text = tag.get_text().strip()
                    self.story.addToList('category', tag_text)

        # Get chapter list
        chapters = []
        chapter_pages = {1}
        for link in soup.select('a[href]'):
            query = urllib.parse.urlparse(link['href']).query
            for page in urllib.parse.parse_qs(query).get('Chapter_page', []):
                if page.isdigit():
                    chapter_pages.add(int(page))

        chapter_soups = [soup]
        for page in range(2, max(chapter_pages) + 1):
            page_url = '%s?Chapter_page=%d' % (self.url, page)
            chapter_soups.append(self.make_soup(self.get_request(page_url)))

        reverse_chapters = bool(soup.select_one("input[name=C_sortChapters][value='0']"))
        for chapter_soup in chapter_soups:
            for row in chapter_soup.select("#Chapters .chapter_row"):
                # Проверяем наличие кнопки "читать"
                read_btn = row.select_one("td>a.btn")
                if not read_btn or read_btn.get_text().strip() != "читать":
                    continue

                title_el = row.select_one("td.t a")
                if title_el:
                    chapter_url = title_el['href']
                    if not chapter_url.startswith('http'):
                        chapter_url = 'https://' + self.getSiteDomain() + chapter_url
                    chapter_title = title_el.get_text().strip()
                    chapters.append((chapter_title, chapter_url))

        if reverse_chapters:
            chapters.reverse()

        for title, url in chapters:
            self.add_chapter(title, url)

    def getChapterText(self, url):
        logger.debug('Getting chapter text from: %s' % url)
        
        data = self.get_request(url)
        soup = self.make_soup(data)
        
        # Проверяем различные селекторы для поиска контента
        chapter = soup.select_one("#text-container .content-text") or \
                 soup.select_one(".content-text") or \
                 soup.select_one("#text-container") or \
                 soup.select_one(".text-container") or \
                 soup.select_one(".text-content-group") or \
                 soup.select_one("#text") or \
                 soup.select_one(".chapter-text") or \
                 soup.select_one(".text")
                 
        if not chapter:
            # Проверяем различные случаи отсутствия контента
            if soup.find('p', text=re.compile("В этой главе нет ни одного переведённого фрагмента")):
                return "Глава не переведена"
            elif soup.find('div', text=re.compile("Глава не найдена")):
                return "Глава не найдена"
            elif soup.find(text=re.compile("Глава платная")):
                return "Глава платная"
            else:
                # Если не нашли контент по селекторам, попробуем найти любой текст
                text_content = soup.find('div', class_=lambda x: x and ('text' in x.lower() or 'content' in x.lower()))
                if text_content:
                    chapter = text_content
                else:
                    raise Exception('Chapter content not found!')
        
        # Remove link at the end of chapter if present
        paragraphs = chapter.find_all('p')
        last_p = paragraphs[-1] if paragraphs else None
        if last_p and self.getSiteDomain() in last_p.get_text():
            last_p.decompose()
            
        # Remove advertisement blocks
        for div in chapter.select("div.thumbnail"):
            div.decompose()
            
        # Process images
        for img in chapter.find_all('img'):
            # Get image URL from data-src or src
            img_url = img.get('data-src') or img.get('src', '')
            if not img_url:
                continue
                
            # Если это XPath, пропускаем
            if img_url.startswith('/html/'):
                continue
                
            # Если URL относительный и не начинается с http или //, добавляем домен
            if not (img_url.startswith('http') or img_url.startswith('//')):
                img_url = 'https://' + self.getSiteDomain() + ('/' if not img_url.startswith('/') else '') + img_url
            
            # Если URL начинается с //, добавляем https:
            if img_url.startswith('//'):
                img_url = 'https:' + img_url
            
            # Сохраняем alt текст из title если нет alt
            if not img.get('alt') and img.get('title'):
                img['alt'] = img['title']
                
            try:
                # Загружаем изображение
                if not img_url.startswith('/html/'):  # Пропускаем XPath ссылки
                    self.story.addImgUrl(url, img_url, self.get_request_raw)
            except Exception as e:
                logger.warning(f"Failed to process image {img_url}: {e}")
                img['src'] = img_url
            
        return self.utf8FromSoup(url, chapter) 

    def _request_with_retries(self, method, url, **kwargs):
        kwargs.setdefault('timeout', (10, 30))
        retryable = method.upper() in ('GET', 'HEAD')
        max_attempts = 3 if retryable else 1
        for attempt in range(max_attempts):
            self._total_requests += 1
            logger.debug("%s request #%d to %s",
                         method.upper(), self._total_requests, url)
            response = self._session.request(method, url, **kwargs)
            if response.status_code != 429:
                break
            if attempt < max_attempts - 1:
                retry_after = response.headers.get('Retry-After')
                try:
                    if retry_after and retry_after.isdigit():
                        delay = int(retry_after)
                    elif retry_after:
                        retry_at = parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        delay = max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    else:
                        delay = 2 ** (attempt + 1)
                except (TypeError, ValueError, OverflowError):
                    delay = 2 ** (attempt + 1)
                delay = min(delay, 30)
                logger.warning("tl.rulate.ru rate limit (429), retrying in %s seconds", delay)
                time.sleep(delay)

        if response.status_code == 429:
            raise Exception('tl.rulate.ru вернул 429 Too Many Requests; повторите позже')
        if response.status_code == 404 and url == self.url:
            raise exceptions.StoryDoesNotExist(self.url)
        response.raise_for_status()
        return response

    def get_request(self, url, **kwargs):
        """Перехватываем все запросы для подсчета и используем сессию"""
        response = self._request_with_retries('GET', url, **kwargs)
        return response.text  # Возвращаем текст вместо content

    def post_request(self, url, data, **kwargs):
        """Перехватываем все POST запросы для подсчета и используем сессию"""
        response = self._request_with_retries('POST', url, data=data, **kwargs)
        return response.text  # Возвращаем текст вместо content

    def login(self):
        """Login to tl.rulate.ru"""
        self._login_attempts += 1
        logger.debug("Login attempt #%d", self._login_attempts)
        
        if self._is_logged_in:
            logger.debug("[DEBUG] Пропускаем логин - уже авторизованы")
            return True
            
        # Получаем страницу с формой логина, не требуя гостевого доступа к книге
        login_page_url = 'https://' + self.getSiteDomain() + '/'
        initial_response = self._request_with_retries('GET', login_page_url)
        soup = self.make_soup(initial_response.text)

        if not soup.select_one('#header-login'):
            logger.debug("Already authenticated with tl.rulate.ru")
            self._is_logged_in = True
            return True
        
        # Ищем скрытое поле с CSRF-токеном
        csrf_meta = soup.find('meta', {'name': 'csrf-token'})
        if csrf_meta:
            csrf_token = csrf_meta.get('content')
            logger.debug("CSRF token found")
        else:
            logger.debug("CSRF token not found in meta tag")
            csrf_token = None
            
        # Выводим HTML формы для отладки
        login_form = soup.select_one('#header-login form')
        if login_form:
            logger.debug("Найдена форма логина:")
            # logger.debug(login_form.prettify()) # Слишком много вывода
        else:
            logger.debug("Форма логина не найдена!")
            return False
            
        logger.debug("Отправляем данные для авторизации...")
        
        # Формируем данные для отправки
        login_data = {
            'login[login]': self.getConfig('username'),
            'login[pass]': self.getConfig('password')
        }
        
        # Добавляем CSRF-токен если нашли
        if csrf_token:
            login_data['_csrf'] = csrf_token
            
        logger.debug("Prepared login data")
        
        # Добавляем заголовки
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://' + self.getSiteDomain(),
            'Referer': self.url,
            'X-CSRF-Token': csrf_token if csrf_token else '',
            'X-Requested-With': 'XMLHttpRequest'
        }
        
        # Отправляем на корневой URL
        login_url = 'https://' + self.getSiteDomain() + '/'
        logger.debug(f"Отправляем запрос на: {login_url}")
        
        try:
            # Отправляем POST-запрос для логина
            response = self._request_with_retries(
                'POST', login_url, data=login_data, headers=headers)
            
            # Проверяем успешность логина
            soup = self.make_soup(response.text)
            if soup.select_one('#header-login'):
                logger.debug("Ошибка авторизации! Проверьте логин и пароль.")
                self._is_logged_in = False
                return False
                
            logger.debug("Авторизация успешно завершена!")
            self._is_logged_in = True
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при авторизации: {str(e)}")
            self._is_logged_in = False
            return False
        
    def is_logged_in(self):
        """Check if we're logged in"""
        logger.debug("Checking authentication (attempts: %d, requests: %d)",
                 self._login_attempts, self._total_requests)
        
        if self._is_logged_in:
            logger.debug("[DEBUG] Используем сохраненное состояние авторизации")
            return True
            
        login_page_url = 'https://' + self.getSiteDomain() + '/'
        soup = self.make_soup(self.get_request(login_page_url))
        self._is_logged_in = not bool(soup.select_one('#header-login'))
        logger.debug("Authentication check result: %s", "success" if self._is_logged_in else "not logged in")
        return self._is_logged_in