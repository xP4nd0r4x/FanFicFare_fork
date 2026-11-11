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


def test_gallery_chapter_is_built_only_when_requested():
    class LazyGalleryAdapter:
        gallery_images = [{'url': 'https://cm.author.today/image.png'}]
        getChapterText = AuthorTodayAdapter.getChapterText

        def make_gallery_chapter(self, gallery_images):
            self.build_count = getattr(self, 'build_count', 0) + 1
            return '<div>gallery</div>'

    adapter = LazyGalleryAdapter()

    assert adapter.getChapterText('https://author.today/work/42#gallery') == '<div>gallery</div>'
    assert adapter.build_count == 1
    assert adapter.getChapterText('https://author.today/work/42#gallery') == '<div>gallery</div>'
    assert adapter.build_count == 1