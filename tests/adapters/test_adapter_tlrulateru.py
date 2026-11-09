from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, call, patch

from fanficfare.configurable import Configuration
from fanficfare.adapters.adapter_tlrulateru import TLRulateRuAdapter
from fanficfare.exceptions import StoryDoesNotExist


def test_adapter_instances_have_independent_sessions():
    configuration = Configuration(['tl.rulate.ru'], 'EPUB', lightweight=True)
    first = TLRulateRuAdapter(configuration, 'https://tl.rulate.ru/book/123')
    second = TLRulateRuAdapter(configuration, 'https://tl.rulate.ru/book/456')

    assert first._session is not second._session
    assert first._is_logged_in is False
    assert second._is_logged_in is False


def test_story_page_404_raises_story_does_not_exist():
    story_url = 'https://tl.rulate.ru/book/123'
    configuration = Configuration(['tl.rulate.ru'], 'EPUB', lightweight=True)
    adapter = TLRulateRuAdapter(configuration, story_url)
    response = Mock(status_code=404, headers={})

    with patch.object(adapter._session, 'request', return_value=response):
        try:
            adapter.get_request(story_url)
        except StoryDoesNotExist as error:
            assert error.url == story_url
        else:
            raise AssertionError('Expected StoryDoesNotExist for a missing story')


def test_login_status_checks_site_homepage():
    story_url = 'https://tl.rulate.ru/book/123'
    login_page_url = 'https://tl.rulate.ru/'
    configuration = Configuration(['tl.rulate.ru'], 'EPUB', lightweight=True)
    adapter = TLRulateRuAdapter(configuration, story_url)

    with patch.object(adapter, 'get_request', return_value='<div id="header-login"></div>') as get_request:
        assert adapter.is_logged_in() is False

    get_request.assert_called_once_with(login_page_url)


def test_get_retries_429_using_http_date():
    story_url = 'https://tl.rulate.ru/book/123'
    configuration = Configuration(['tl.rulate.ru'], 'EPUB', lightweight=True)
    adapter = TLRulateRuAdapter(configuration, story_url)
    retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=15), usegmt=True)
    rate_limited = Mock(status_code=429, headers={'Retry-After': retry_at})
    success = Mock(status_code=200)

    with patch.object(adapter._session, 'request', side_effect=[rate_limited, success]) as request:
        with patch('fanficfare.adapters.adapter_tlrulateru.time.sleep') as sleep:
            assert adapter.get_request(story_url) == success.text

    assert request.call_count == 2
    delay = sleep.call_args.args[0]
    assert 0 < delay <= 15


def test_post_does_not_retry_429():
    story_url = 'https://tl.rulate.ru/book/123'
    configuration = Configuration(['tl.rulate.ru'], 'EPUB', lightweight=True)
    adapter = TLRulateRuAdapter(configuration, story_url)
    rate_limited = Mock(status_code=429, headers={'Retry-After': '1'})

    with patch.object(adapter._session, 'request', return_value=rate_limited) as request:
        with patch('fanficfare.adapters.adapter_tlrulateru.time.sleep') as sleep:
            try:
                adapter.post_request(story_url, {'key': 'value'})
            except Exception as error:
                assert '429' in str(error)
            else:
                raise AssertionError('Expected an error for a rate-limited POST')

    request.assert_called_once()
    sleep.assert_not_called()


def test_extract_chapters_across_paginated_list():
    story_url = 'https://tl.rulate.ru/book/123'
    page_one = '''
    <h1>Book</h1>
    <input name="C_sortChapters" value="0">
        <table id="Chapters">
      <tr class="chapter_row"><td class="t"><a href="/chapter/3">Chapter 3</a></td><td><a class="btn">читать</a></td></tr>
      <tr class="chapter_row"><td class="t"><a href="/chapter/2">Chapter 2</a></td><td><a class="btn">читать</a></td></tr>
        </table>
    <a href="?Chapter_page=2">2</a>
    '''
    page_two = '''
        <table id="Chapters">
      <tr class="chapter_row"><td class="t"><a href="/chapter/1">Chapter 1</a></td><td><a class="btn">читать</a></td></tr>
        </table>
    '''
    responses = {
        story_url: page_one,
        story_url + '?Chapter_page=2': page_two,
    }
    configuration = Configuration(['tl.rulate.ru'], 'EPUB', lightweight=True)
    adapter = TLRulateRuAdapter(configuration, story_url)

    with patch.object(adapter, 'get_request', side_effect=responses.__getitem__) as get_request:
        adapter.extractChapterUrlsAndMetadata()

    assert get_request.call_args_list == [
        call(story_url),
        call(story_url + '?Chapter_page=2'),
    ]
    assert [(chapter['title'], chapter['url']) for chapter in adapter.chapterInfos] == [
        ('Chapter 1', 'https://tl.rulate.ru/chapter/1'),
        ('Chapter 2', 'https://tl.rulate.ru/chapter/2'),
        ('Chapter 3', 'https://tl.rulate.ru/chapter/3'),
    ]