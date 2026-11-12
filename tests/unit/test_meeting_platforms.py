import pytest

from app.meeting.security import meeting_platform, validate_meeting_url


@pytest.mark.parametrize(('url', 'platform'), [
    ('https://meet.google.com/abc-defg-hij', 'google_meet'),
    ('https://us02web.zoom.us/j/12345678901?pwd=abc', 'zoom'),
    ('https://zoom.us/j/123456789', 'zoom'),
    ('https://teams.microsoft.com/l/meetup-join/19%3ameeting_test%40thread.v2/0?context=%7B%7D', 'microsoft_teams'),
    ('https://teams.cloud.microsoft/meet/123456789?p=abc', 'microsoft_teams'),
])
def test_platform_links(url, platform):
    assert validate_meeting_url(url) == url
    assert meeting_platform(url) == platform


@pytest.mark.parametrize('url', [
    'https://zoom.us.evil.com/j/123456789', 'https://evilzoom.us/j/123456789',
    'https://user@zoom.us/j/123456789', 'https://zoom.us/j/123456789?redirect=evil',
    'https://teams.microsoft.com.evil/l/meetup-join/foo/0',
    'https://teams.microsoft.com/l/channel/foo', 'https://zoom.us:443/j/123456789',
    'https://zoom.us/j/123456789#x', 'https://zoom.us\\@evil/j/123456789',
])
def test_unsafe_links(url):
    with pytest.raises(ValueError):
        validate_meeting_url(url)
