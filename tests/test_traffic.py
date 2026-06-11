"""Tests for the shared real-browser traffic classification."""
from oracle.traffic import is_real_browser, normalize_ip, real_browser_hit

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15"


def test_normalize_ip_v4_passthrough():
    assert normalize_ip("84.151.20.7") == "84.151.20.7"


def test_normalize_ip_v6_to_64_prefix():
    assert normalize_ip("2001:a61:1234:5678:abcd:ef01:2345:6789") == "2001:a61:1234:5678::/64"


def test_normalize_ip_same_64_collapses():
    a = normalize_ip("2001:a61:1234:5678:aaaa:bbbb:cccc:dddd")
    b = normalize_ip("2001:a61:1234:5678:1111:2222:3333:4444")
    assert a == b


def test_normalize_ip_garbage_passthrough():
    assert normalize_ip("not-an-ip") == "not-an-ip"


def test_normalize_ip_empty():
    assert normalize_ip("") == ""


def test_is_real_browser_mozilla_yes():
    assert is_real_browser(_UA)


def test_is_real_browser_bots_and_tools_no():
    assert not is_real_browser("")
    assert not is_real_browser("curl/8.4.0")
    assert not is_real_browser("Mozilla/5.0 (compatible; Googlebot/2.1)")
    assert not is_real_browser("python-requests/2.31")


def test_real_browser_hit_strips_host_and_query():
    hit = real_browser_hit(_UA, "https://walchensee.simon-stieber.de/?day=2026-06-11&lang=de", "84.151.20.7")
    assert hit == ("84.151.20.7", "/")


def test_real_browser_hit_drops_exploit_paths():
    assert real_browser_hit(_UA, "https://x.de/wp-admin/setup.php", "84.151.20.7") is None
    assert real_browser_hit(_UA, "https://x.de/.git/config", "84.151.20.7") is None


def test_real_browser_hit_drops_bots():
    assert real_browser_hit("curl/8.4.0", "https://x.de/", "84.151.20.7") is None


def test_real_browser_hit_bare_path_url():
    assert real_browser_hit(_UA, "", "84.151.20.7") == ("84.151.20.7", "/")
