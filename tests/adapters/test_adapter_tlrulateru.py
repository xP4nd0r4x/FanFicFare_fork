from unittest.mock import call, patch

from fanficfare.configurable import Configuration
from fanficfare.adapters.adapter_tlrulateru import TLRulateRuAdapter


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