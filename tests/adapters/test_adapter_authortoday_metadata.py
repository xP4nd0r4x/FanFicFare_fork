import pytest
from unittest.mock import Mock

from fanficfare import exceptions
from fanficfare.adapters.adapter_authortoday import AuthorTodayAdapter


class StoryStub:
    def __init__(self):
        self.metadata = {'storyId': '42'}
        self.lists = {}

    def getMetadata(self, key):
        return self.metadata.get(key)

    def setMetadata(self, key, value):
        self.metadata[key] = value

    def addToList(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def getImgUrls(self):
        return []


class AdapterStub:
    extractChapterUrlsAndMetadata = AuthorTodayAdapter.extractChapterUrlsAndMetadata
    getSiteDomain = staticmethod(lambda: 'author.today')

    def __init__(self, details, chapters, is_adult=True):
        self.url = 'https://author.today/work/42'
        self.story = StoryStub()
        self.is_adult = is_adult
        self.dateformat = '%Y-%m-%d'
        self.bearer_token = 'test-token'
        self.gallery_images = []
        self.chapters = []
        self.checkLogin = Mock(return_value=True)
        details_response = Mock()
        details_response.json.return_value = details
        chapters_response = Mock()
        chapters_response.json.return_value = chapters
        self.session = Mock()
        self.session.get.side_effect = [details_response, chapters_response]

    def add_chapter(self, title, url):
        self.chapters.append((title, url))


def test_metadata_includes_tags_and_adult_mark():
    adapter = AdapterStub({
        'id': 42,
        'title': 'Test story',
        'adultOnly': True,
        'tags': ['Fantasy', 'Adventure'],
        'authorFIO': 'Test Author',
        'authorId': 7,
        'authorUserName': 'test-author',
        'isFinished': True,
    }, [{'id': 101, 'title': 'First chapter'}])

    adapter.extractChapterUrlsAndMetadata()

    assert adapter.story.metadata['title'] == 'Test story'
    assert adapter.story.metadata['author'] == 'Test Author'
    assert adapter.story.metadata['authorId'] == '7'
    assert adapter.story.lists['genre'] == ['Fantasy', 'Adventure', '18+']
    assert adapter.story.lists['tags'] == ['Fantasy', 'Adventure', '18+']
    assert adapter.story.lists['subject'] == ['Fantasy', 'Adventure', '18+']
    assert adapter.chapters == [
        ('First chapter', 'https://author.today/reader/42/101'),
    ]


def test_adult_story_still_requires_adult_setting():
    adapter = AdapterStub({
        'id': 42,
        'adultOnly': True,
    }, [], is_adult=False)

    with pytest.raises(exceptions.AdultCheckRequired):
        adapter.extractChapterUrlsAndMetadata()

    adapter.session.get.assert_called_once()
