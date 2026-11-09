from unittest.mock import Mock, patch

from fanficfare.adapters.adapter_authortoday import AuthorTodayAdapter
from fanficfare.adapters.adapter_authortoday import REQUEST_TIMEOUT


class AdapterStub:
    user_id = None
    decrypt_chapter_text = AuthorTodayAdapter.decrypt_chapter_text


def test_decrypt_chapter_text_preserves_emoji():
    plain_text = '<p>Hello \U0001f600!</p>'
    reader_secret = 'reader-key'
    key = (reader_secret[::-1] + '@_@').encode('utf-16-le')
    plain = plain_text.encode('utf-16-le')
    encrypted = bytearray()

    for pos in range(0, len(plain), 2):
        text_code = int.from_bytes(plain[pos:pos + 2], 'little')
        key_pos = (pos // 2 * 2) % len(key)
        key_code = int.from_bytes(key[key_pos:key_pos + 2], 'little')
        encrypted.extend((text_code ^ key_code).to_bytes(2, 'little'))

    encrypted_text = bytes(encrypted).decode('utf-16-le', 'surrogatepass')
    decrypted = AdapterStub().decrypt_chapter_text(encrypted_text, reader_secret)

    assert decrypted == plain_text


def test_auth_state_is_reused_per_account_and_separate_between_accounts():
    first_account = AuthorTodayAdapter._get_auth_state('test-account-one')
    same_account = AuthorTodayAdapter._get_auth_state('test-account-one')
    second_account = AuthorTodayAdapter._get_auth_state('test-account-two')

    assert first_account is same_account
    assert first_account is not second_account
    assert first_account.session is not second_account.session


def test_download_image_accepts_headers_and_uses_timeout():
    class ImageAdapterStub:
        bearer_token = 'test-token'
        session = Mock()
        getSiteDomain = staticmethod(lambda: 'author.today')
        download_image = AuthorTodayAdapter.download_image

    image_url = 'https://cm.author.today/image.png'
    response = Mock(status_code=200, content=b'x' * 128,
                    headers={'content-type': 'image/png'})
    adapter = ImageAdapterStub()

    with patch.object(adapter.session, 'get', return_value=response) as get:
        assert adapter.download_image(image_url, headers={'Accept': 'image/png'}) == response.content

    request_headers = get.call_args.kwargs['headers']
    assert request_headers['Authorization'] == 'Bearer test-token'
    assert request_headers['Accept'] == 'image/png'
    assert get.call_args.kwargs['timeout'] == REQUEST_TIMEOUT