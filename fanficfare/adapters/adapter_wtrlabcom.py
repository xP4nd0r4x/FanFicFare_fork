# -*- coding: utf-8 -*-

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape

from .base_adapter import BaseSiteAdapter
from .. import exceptions

logger = logging.getLogger(__name__)


def getClass():
    return WtrLabComAdapter


class WtrLabComAdapter(BaseSiteAdapter):
    """Adapter for WTR-LAB's Next.js site and reader API.

    WTR-LAB authenticates with a magic link.  Configure FFF with a Mozilla
    cookies.txt exported after opening that link in a browser; no password
    login is attempted here.  Without the cookie, the site normally exposes
    only its first ten chapters.
    """

    API_BASE = 'https://wtr-lab.com/api'

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)

        self.wtr_cookies_loaded = False
        cookie_file = self.getConfig('mozilla_cookies', '')
        if cookie_file:
            try:
                cookiejar = self.configuration.get_cookiejar(
                    filename=cookie_file, mozilla=True)
                self.configuration.set_cookiejar(cookiejar)
                self.wtr_cookies_loaded = True
                logger.debug('Loaded WTR-LAB cookies from %s', cookie_file)
            except Exception as exc:
                logger.warning('Could not load WTR-LAB cookies from %s: %s',
                               cookie_file, exc)

        match = re.match(self.getSiteURLPattern(), url)
        self.story_id = match.group('id')
        self.story_slug = match.group('slug')
        self.story.setMetadata('storyId', self.story_id)
        self.story.setMetadata('siteabbrev', 'wtrlab')
        self._setURL('https://%s/en/novel/%s/%s/' %
                 (self.getSiteDomain(), self.story_id, self.story_slug))

    @staticmethod
    def getSiteDomain():
        return 'wtr-lab.com'

    @classmethod
    def getSiteExampleURLs(cls):
        return ('https://%s/en/novel/83136/story-title' %
            cls.getSiteDomain())

    def getSiteURLPattern(self):
        return (r'https?://' + re.escape(self.getSiteDomain()) +
                r'/en/novel/(?P<id>\d+)/(?P<slug>[^/]+)'
            r'(?:/chapter-(?P<chapter>\d+))?/?$')

    def _get_json(self, url):
        try:
            fetcher = self.configuration.get_fetcher()
            if self.getConfig('use_browser_cache', False):
                response = fetcher.get_requests_session().get(url)
                if not response.ok:
                    raise exceptions.FailedToDownload(
                        'WTR-LAB API request failed with HTTP %s' %
                        response.status_code)
                return response.json()
            return json.loads(self.get_request(url))
        except (TypeError, ValueError) as exc:
            raise exceptions.FailedToDownload(
                'Invalid JSON response from WTR-LAB: %s' % url) from exc

    def _browser_bridge_enabled(self):
        return str(self.getConfig('browser_bridge', 'false')).lower() == 'true'

    def _get_chapter_from_browser(self, payload):
        bridge_url = self.getConfig('browser_bridge_url',
                                    'http://127.0.0.1:8768')
        timeout = int(self.getConfig('browser_bridge_timeout', 900))
        # Как часто повторять "я жив" в лог задания, если состояние не
        # меняется — иначе долгое ожидание Turnstile/браузера выглядит
        # как зависание, хотя мост просто ждёт.
        heartbeat_interval = int(self.getConfig('browser_bridge_heartbeat', 10))

        cache_query = urllib.parse.urlencode({
            'raw_id': payload['raw_id'],
            'chapter_id': payload['chapter_id'],
            'language': payload['language'],
        })
        try:
            with urllib.request.urlopen(
                    bridge_url.rstrip('/') + '/wtrlab/cache?' + cache_query,
                    timeout=10) as response:
                cached = json.loads(response.read().decode('utf-8'))
                if cached.get('chapter_html') is not None:
                    logger.info('WTR-LAB: chapter %s found in browser-bridge cache',
                                payload['chapter_id'])
                    return cached['chapter_html']
        except (OSError, ValueError, KeyError):
            pass

        logger.info('WTR-LAB: requesting chapter %s via browser bridge at %s',
                    payload['chapter_id'], bridge_url)
        request = urllib.request.Request(
            bridge_url.rstrip('/') + '/wtrlab/job',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                job_id = json.loads(response.read().decode('utf-8'))['job_id']
        except (OSError, ValueError, KeyError) as exc:
            raise exceptions.FailedToDownload(
                'WTR-LAB browser bridge is not running at %s. Start '
                'wtr_lab_bridge.py (or the FFF Bridge Calibre plugin) while '
                'the browser is open.' % bridge_url) from exc

        logger.info('WTR-LAB: job %s queued, waiting for browser (timeout %ss)',
                    job_id, timeout)

        start = time.time()
        deadline = start + timeout
        last_state = None
        last_heartbeat = start

        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        bridge_url.rstrip('/') + '/wtrlab/job/' + job_id,
                        timeout=10) as response:
                    status = json.loads(response.read().decode('utf-8'))
            except (OSError, ValueError) as exc:
                raise exceptions.FailedToDownload(
                    'WTR-LAB browser bridge stopped responding') from exc

            state = status.get('state')
            now = time.time()

            if state != last_state:
                if state == 'waiting':
                    logger.info(
                        'WTR-LAB: job %s is waiting on a Turnstile check — '
                        'solve it in the browser tab', job_id)
                elif state == 'running':
                    logger.info('WTR-LAB: job %s claimed by browser, downloading…',
                                job_id)
                elif state == 'pending':
                    logger.info('WTR-LAB: job %s re-queued, waiting for browser tab',
                                job_id)
                last_state = state
                last_heartbeat = now
            elif now - last_heartbeat >= heartbeat_interval:
                logger.info('WTR-LAB: job %s still %s (%ds elapsed of %ds timeout)',
                            job_id, state, int(now - start), timeout)
                last_heartbeat = now

            if state == 'complete':
                logger.info('WTR-LAB: job %s complete (%ds elapsed)',
                            job_id, int(now - start))
                return status.get('chapter_html', '')
            if state == 'error':
                raise exceptions.FailedToDownload(
                    'WTR-LAB browser bridge: %s' % status.get('error', 'unknown error'))
            time.sleep(1)

        raise exceptions.FailedToDownload(
            'Timed out waiting for Waterfox to download WTR-LAB chapter')

    def _extract_next_data(self, soup):
        """
        WTR-LAB — Next.js-сайт: JSON-LD (application/ld+json) там не
        используется, зато Next.js на КАЖДОЙ странице кладёт все
        данные, полученные с сервера, в
        <script id="__NEXT_DATA__" type="application/json"> — на этом
        держится гидратация страницы, так что этот блок практически
        гарантированно есть. Там title/author/description лежат в
        чистом виде, без "SEO-хвостов" вроде "RAW English Translation
        - WTR-LAB", которые есть в <title> и meta-описании.
        Возвращает словарь serie_data или None.
        """
        script = soup.find('script', id='__NEXT_DATA__')
        if not script or not script.string:
            return None
        try:
            data = json.loads(script.string)
        except (ValueError, TypeError):
            return None
        try:
            return data['props']['pageProps']['serie']['serie_data']
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _extract_author_from_details(soup):
        """
        Резервный вариант на случай, если структура __NEXT_DATA__
        изменится: в таблице "Details" на странице есть строка
        "Author" с одной или двумя ссылками — оригинальное имя и (не
        всегда) его английская транслитерация вторым линком с классом
        'opacity-65'. Предпочитаем транслитерацию, если она есть.
        """
        for row in soup.find_all('div'):
            label = row.find('span', recursive=False)
            if not label or label.get_text(strip=True).lower() != 'author':
                continue
            links = row.select('a')
            if not links:
                continue
            transliteration = next(
                (a.get_text(strip=True) for a in links
                 if 'opacity-65' in (a.get('class') or [])), None)
            return transliteration or links[0].get_text(strip=True)
        return None

    # Тексты кнопок-ссылок на /novel/..., которые НЕ являются
    # названием произведения — встречаются в верстке страницы и раньше
    # ошибочно попадали в title через селектор a[href*="/novel/"].
    _NOT_A_TITLE = {
        'start reading', 'read now', 'continue reading', 'read',
        'read more', 'view more', 'details',
    }

    @staticmethod
    def _extract_tags(soup):
        """
        Плашки жанров/тегов (Male, Marvel, System, action, adventure...)
        рендерятся прямо на странице отдельной строкой сразу под
        карточкой книги, каждая как обычный <span class="... capitalize
        ...">Название</span> БЕЗ обёртки в <a>. Ниже на странице есть
        ещё один, куда более длинный список тегов (полный "Genre &
        Tags"), но там те же CSS-классы используются внутри <a
        href="...">, поэтому фильтр "нет родителя <a>" надёжно берёт
        именно короткую сводную строку и не собирает лишнего.
        """
        tags = []
        seen = set()
        for span in soup.select('span[class*="capitalize"]'):
            if span.find_parent('a') is not None:
                continue
            text = span.get_text(' ', strip=True)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                tags.append(text)
        return tags

    def extractChapterUrlsAndMetadata(self, get_cover=True):
        page = self.get_request(self.url)
        soup = self.make_soup(page)

        title = None
        author = None
        description = None

        # 1) __NEXT_DATA__ — основной и самый надёжный источник для
        # этого конкретного сайта (см. docstring выше).
        next_data = self._extract_next_data(soup)
        if next_data:
            story_data = next_data.get('data') or {}
            title = story_data.get('title')
            # 'data.author' — обычно английская транслитерация имени
            # ("Ling Shu"); на верхнем уровне serie_data['author'] —
            # оригинальное (китайское) имя как запасной вариант.
            author = story_data.get('author') or next_data.get('author')
            description = story_data.get('description')

        # 2) JSON-LD — на случай другой раскладки сайта/будущих
        # изменений (на самом WTR-LAB его нет, но не помешает).
        if not title:
            ld = self._extract_json_ld(soup)
            if ld:
                title = ld.get('name')
                author = author or self._author_name_from(ld.get('author'))
                description = description or ld.get('description')

        # 3) OpenGraph/meta-теги.
        if not title:
            og_title = soup.select_one('meta[property="og:title"]')
            if og_title and og_title.get('content'):
                title = og_title['content']
        if not title:
            title_tag = soup.select_one('title')
            if title_tag:
                # У WTR-LAB <title> вида "Read <Название> RAW <Язык>
                # Translation - WTR-LAB" — отрезаем оба известных
                # хвоста, а не только "- WTR-LAB".
                raw_title = title_tag.get_text(' ', strip=True)
                cleaned = re.sub(r'^Read\s+', '', raw_title, flags=re.I)
                cleaned = re.sub(
                    r'\s+RAW\s+\S+\s+Translation\s*[-|]\s*WTR-LAB.*$',
                    '', cleaned, flags=re.I)
                cleaned = re.sub(r'\s*[-|]\s*WTR-LAB.*$', '', cleaned, flags=re.I)
                title = cleaned.strip() or None

        if not author:
            meta_author = soup.select_one('meta[name="author"], meta[property="article:author"]')
            if meta_author and meta_author.get('content'):
                author = meta_author['content']
        if not author:
            author = self._extract_author_from_details(soup)

        # 4) Резервный вариант — ищем ссылку на /novel/..., но
        # игнорируем текст кнопок-переходов вроде "Start Reading",
        # из-за которого раньше название книги терялось.
        if not title:
            for link in soup.select('a[href*="/novel/"]'):
                text = link.get_text(' ', strip=True)
                if text and text.strip().lower() not in self._NOT_A_TITLE:
                    title = text
                    break

        self.story.setMetadata('title', title or 'WTR-LAB %s' % self.story_id)
        if not self.story.getList('author'):
            self.story.addToList('author', author or 'WTR-LAB')
        if not self.story.getList('authorId'):
            self.story.addToList('authorId', 'wtr-lab')

        logger.debug('WTR-LAB: title=%r author=%r (next_data=%s)',
                     title, author, bool(next_data))

        for tag in self._extract_tags(soup):
            self.story.addToList('genre', tag)

        if get_cover:
            cover = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
            if cover and cover.get('content'):
                self.setCoverImage(self.url, cover['content'])

        if not description:
            desc_tag = soup.select_one('meta[name="description"]')
            if desc_tag and desc_tag.get('content'):
                description = desc_tag['content']
        if description:
            self.setDescription(self.url, description)

        chapter_data = self._get_json('%s/chapters/%s' %
                                      (self.API_BASE, self.story_id))
        chapters = chapter_data.get('chapters', [])
        if not chapters:
            raise exceptions.StoryDoesNotExist(self.url)

        for chapter in chapters:
            order = chapter.get('order')
            chapter_url = ('https://%s/en/novel/%s/%s/chapter-%s' %
                           (self.getSiteDomain(), self.story_id,
                            self.story_slug, order))
            self.add_chapter(chapter.get('title'), chapter_url,
                             {'chapter_id': chapter.get('id'),
                              'order': order})

    def getChapterText(self, url):
        match = re.match(self.getSiteURLPattern(), url)
        if not match:
            raise exceptions.InvalidStoryURL(url, self.getSiteDomain(),
                                              self.getSiteExampleURLs())

        chapter = next((item for item in self.chapterUrls
                        if item['url'] == url), None)
        if chapter is None:
            chapter_id = None
            order = match.group('chapter')
        else:
            chapter_id = chapter.get('chapter_id')
            order = chapter.get('order', match.group('chapter'))
        if not chapter_id:
            raise exceptions.FailedToDownload(
                'Missing WTR-LAB chapter id for %s' % url)

        payload = {
            'translate': 'ai',
            'language': self.getConfig('download_language', 'en'),
            'raw_id': self.story_id,
            'chapter_id': chapter_id,
        }
        if self._browser_bridge_enabled():
            return self._get_chapter_from_browser(payload)

        fetcher = self.configuration.get_fetcher()
        if not hasattr(fetcher, 'get_requests_session'):
            raise exceptions.FailedToDownload(
                'WTR-LAB requires the requests fetcher for its JSON reader API')

        response = fetcher.get_requests_session().post(
            '%s/reader/get' % self.API_BASE,
            json=payload,
            headers={'Content-Type': 'application/json;charset=UTF-8',
                     'Referer': url})
        if not response.ok:
            raise exceptions.FailedToDownload(
                'WTR-LAB reader request failed with HTTP %s' % response.status_code)
        try:
            result = response.json()
        except ValueError as exc:
            raise exceptions.FailedToDownload(
                'Invalid WTR-LAB reader response') from exc

        if result.get('requireTurnstile'):
            if self.wtr_cookies_loaded:
                message = (
                    'WTR-LAB cookies were loaded, but the reader API requires '
                    'a fresh browser Turnstile token. Cookies alone cannot '
                    'provide this token; download this chapter through the '
                    'browser or use the guest chapter limit.')
            else:
                message = (
                    'WTR-LAB requires a browser Turnstile check. Configure '
                    'active wtr-lab.com cookies from a magic-link session; '
                    'browser cache alone is not enough.')
            raise exceptions.AccessDenied(
                message)

        data = result.get('data', {}).get('data', {})
        body = data.get('body')
        if not body:
            raise exceptions.FailedToDownload(
                'WTR-LAB reader response has no chapter body')

        images = data.get('images', [])
        image_index = 0
        html = []
        for element in body:
            if element == '[image]':
                if image_index < len(images):
                    html.append('<img src="%s" />' % escape(images[image_index], quote=True))
                image_index += 1
            else:
                html.append('<p>%s</p>' % escape(self.make_soup(element).get_text()))

        title = result.get('chapter', {}).get('title')
        if not title and chapter:
            title = chapter.get('title', '')

        # ВАЖНО: раньше здесь вручную добавлялся свой <h1> с
        # локализованным названием главы. Но FanFicFare сам
        # автоматически вставляет заголовок главы в готовую книгу,
        # используя chapter['title'] из self.chapterUrls (то, что было
        # получено при extractChapterUrlsAndMetadata — на английском,
        # т.к. язык перевода тогда ещё не применялся). Из-за этого
        # получалось ДВА заголовка: наш (переведённый) внутри текста
        # главы + автоматический (английский) от FanFicFare, который
        # и попадал в оглавление книги.
        #
        # Правильно — не добавлять свой заголовок, а обновить
        # chapter['title'] переведённым названием ДО того, как
        # FanFicFare будет писать книгу (список self.chapterUrls —
        # тот же объект, что использует writer, и на момент записи
        # все главы уже скачаны). Тогда и текст, и оглавление получат
        # одно и то же, переведённое название.
        if chapter is not None and title:
            chapter['title'] = title

        return ''.join(html)