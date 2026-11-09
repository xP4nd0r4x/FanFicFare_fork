from fanficfare.adapters.adapter_authortoday import AuthorTodayAdapter


class StoryStub:
    def addImgUrl(self, parenturl, url, cover, fetch):
        return ('images/gallery-42.png', url)


class AdapterStub:
    url = 'https://author.today/work/42'
    story = StoryStub()
    successful_book_downloads = 0
    failed_book_downloads = 0
    make_gallery_chapter = AuthorTodayAdapter.make_gallery_chapter

    def get_request_raw(self, url, referer=None, image=False):
        raise AssertionError('The fetch callback should be passed through, not called here')


def test_gallery_uses_story_image_path_and_omits_none_caption():
    adapter = AdapterStub()

    html = adapter.make_gallery_chapter([
        {'url': 'https://cm.author.today/image.png', 'caption': None},
    ])

    assert 'src="images/gallery-42.png"' in html
    assert 'alt=""' in html
    assert '>None<' not in html
    assert 'ffdl-0.jpg' not in html