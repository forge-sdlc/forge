import re
import os
import json
import time
import socket
import tempfile
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpcore
import httpx

from forge.workflow.utils.references import (
    normalize_url,
    is_safe_ip,
    resolve_and_verify_hostname,
    PinnedAsyncNetworkBackend,
    PinnedAsyncHTTPTransport,
    HTMLToMarkdownParser,
    html_to_markdown,
    read_from_cache,
    write_to_cache,
    get_cache_filepath,
    enforce_cache_folder_size,
    extract_references_from_comment,
    format_and_truncate_aggregate_references,
    fetch_and_inject_references,
    fetch_reference_url,
)


def test_normalize_url() -> None:
    # 1. Whitespace trimming
    assert normalize_url("  https://example.com/  ") == "https://example.com"
    # 2. Scheme and host lowercasing
    assert normalize_url("HTTPS://EXAMPLE.COM/FOO") == "https://example.com/FOO"
    # 3. Default port stripping
    assert normalize_url("http://example.com:80/foo") == "http://example.com/foo"
    assert normalize_url("https://example.com:443/foo") == "https://example.com/foo"
    # Port not stripped if not default
    assert normalize_url("http://example.com:8080/foo") == "http://example.com:8080/foo"
    # 4. Trailing root slash stripping
    assert normalize_url("http://example.com/") == "http://example.com"
    assert normalize_url("http://example.com/foo/") == "http://example.com/foo/"


def test_is_safe_ip() -> None:
    assert not is_safe_ip("127.0.0.1")
    assert not is_safe_ip("::1")
    assert not is_safe_ip("10.0.0.1")
    assert not is_safe_ip("192.168.1.1")
    assert not is_safe_ip("172.16.0.1")
    assert not is_safe_ip("fc00::1")
    assert not is_safe_ip("169.254.169.254")
    assert not is_safe_ip("fe80::1")
    assert not is_safe_ip("224.0.0.1")
    assert not is_safe_ip("ff02::1")
    assert not is_safe_ip("0.0.0.0")
    assert is_safe_ip("8.8.8.8")
    assert is_safe_ip("1.1.1.1")


@patch("socket.getaddrinfo")
def test_resolve_and_verify_hostname(mock_getaddrinfo: MagicMock) -> None:
    # 1. Success case
    mock_getaddrinfo.return_value = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))
    ]
    assert resolve_and_verify_hostname("example.com") == "8.8.8.8"

    # 2. Unsafe IP resolved
    mock_getaddrinfo.return_value = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
    ]
    with pytest.raises(ValueError, match="Unsafe IP address resolved"):
        resolve_and_verify_hostname("loopback.test")


@pytest.mark.asyncio
async def test_pinned_async_network_backend() -> None:
    pinned_ips = {"example.com": "1.1.1.1"}
    backend = PinnedAsyncNetworkBackend(pinned_ips)

    mock_anyio = MagicMock()
    mock_anyio.connect_tcp = AsyncMock()
    mock_anyio.connect_unix_socket = AsyncMock()
    mock_anyio.sleep = AsyncMock()
    backend._backend = mock_anyio

    # Test connect_tcp delegates with pinned IP
    await backend.connect_tcp("example.com", 443)
    mock_anyio.connect_tcp.assert_called_once_with(
        host="1.1.1.1",
        port=443,
        timeout=None,
        local_address=None,
        socket_options=None,
    )

    # Test connect_unix_socket
    await backend.connect_unix_socket("/tmp/socket")
    mock_anyio.connect_unix_socket.assert_called_once_with(
        path="/tmp/socket",
        timeout=None,
        socket_options=None,
    )

    # Test sleep
    await backend.sleep(1.0)
    mock_anyio.sleep.assert_called_once_with(1.0)


def test_html_parsing_malformed() -> None:
    malformed_html = "<h1>Main Title<p>Unclosed paragraph<b>Nested text"
    markdown = html_to_markdown(malformed_html)
    assert "# Main Title" in markdown
    assert "Nested text" in markdown

    # Parser exception scenario
    with patch(
        "html.parser.HTMLParser.feed", side_effect=Exception("Parsing explosion")
    ):
        fallback = html_to_markdown("<html><body>Some text</body></html>")
        assert "Some text" in fallback


def test_extract_references_from_comment() -> None:
    comment_body = (
        "Some general comment text\n"
        "@forge ref https://example.com/doc Standard Reference\n"
        "Another random line\n"
        "@forge ref http://another-url.org/spec\n"
    )
    extracted = extract_references_from_comment(comment_body)
    assert len(extracted) == 2
    assert extracted[0] == {
        "url": "https://example.com/doc",
        "description": "Standard Reference",
    }
    assert extracted[1] == {"url": "http://another-url.org/spec", "description": ""}


def test_individual_truncation() -> None:
    long_body = "A" * 15000
    ref_data = [
        {
            "url": "https://example.com",
            "description": "Truncation Test",
            "body_text": long_body,
        }
    ]
    formatted = format_and_truncate_aggregate_references(ref_data)
    assert "[TRUNCATED - Reference exceeded character limit]" in formatted
    # Check that individual content inside block is limited to 10000 chars plus suffix
    assert len(long_body) > 10000


def test_aggregate_truncation() -> None:
    # 4 references of 10000 characters each. Combined they will exceed 30000 characters limit.
    ref_data = [
        {
            "url": f"https://example.com/{i}",
            "description": f"Ref {i}",
            "body_text": "B" * 9000,
        }
        for i in range(4)
    ]
    formatted = format_and_truncate_aggregate_references(ref_data)
    assert "[TRUNCATED - Aggregate limit exceeded]" in formatted
    assert len(formatted) <= 30000


@pytest.mark.asyncio
async def test_cache_isolation_and_eviction() -> None:
    run_id_A = "run-uuid-A"
    run_id_B = "run-uuid-B"
    norm_url = "https://example.com/foo"

    with patch("forge.workflow.utils.references.get_cache_dir") as mock_cache_dir:
        with tempfile.TemporaryDirectory() as tmpdir:
            dir_A = os.path.join(tmpdir, "run_A")
            dir_B = os.path.join(tmpdir, "run_B")
            os.makedirs(dir_A, exist_ok=True)
            os.makedirs(dir_B, exist_ok=True)

            mock_cache_dir.side_effect = lambda run_id: (
                dir_A if run_id == run_id_A else dir_B
            )

            # 1. Cache isolation test: Write A, ensure B cannot read it
            await write_to_cache(run_id_A, norm_url, "text/html", "Content A")

            cached_A = await read_from_cache(run_id_A, norm_url)
            assert cached_A is not None
            assert cached_A[1] == "Content A"

            cached_B = await read_from_cache(run_id_B, norm_url)
            assert cached_B is None

            # 2. TTL (1 hour) expiration test
            filepath_A = get_cache_filepath(run_id_A, norm_url)
            # Set mtime to 2 hours ago
            past_time = time.time() - 7200
            os.utime(filepath_A, (past_time, past_time))

            expired_A = await read_from_cache(run_id_A, norm_url)
            assert expired_A is None

            # 3. Cache Eviction test (Folder cap 10 MB)
            # Create a 6MB file, then another 6MB file. The first one should get evicted.
            await write_to_cache(
                run_id_A,
                "https://example.com/file1",
                "text/plain",
                "C" * (6 * 1024 * 1024),
            )
            await write_to_cache(
                run_id_A,
                "https://example.com/file2",
                "text/plain",
                "D" * (6 * 1024 * 1024),
            )

            # Eviction is run in enforce_cache_folder_size during write.
            # file1 should be deleted.
            assert not os.path.exists(
                get_cache_filepath(run_id_A, "https://example.com/file1")
            )
            assert os.path.exists(
                get_cache_filepath(run_id_A, "https://example.com/file2")
            )


@pytest.mark.asyncio
async def test_fetch_and_inject_references_full_flow() -> None:
    state = {
        "ticket_key": "PROJ-123",
        "spec_content": "Original specifications here.",
        "context": {"run_id": "test-uuid"},
    }

    mock_jira = MagicMock()
    mock_jira.get_project_references = AsyncMock(
        return_value=[
            {"url": "https://example.com/standing", "description": "Standing Doc"}
        ]
    )

    comment_1 = MagicMock()
    comment_1.body = "@forge ref https://example.com/comment1 Comment Doc"
    comment_1.created = None
    mock_jira.get_comments = AsyncMock(return_value=[comment_1])

    # We mock read_from_cache and fetch_reference_url to avoid actual network calls
    with (
        patch(
            "forge.workflow.utils.references.read_from_cache",
            AsyncMock(return_value=None),
        ),
        patch(
            "forge.workflow.utils.references.fetch_reference_url",
            AsyncMock(return_value=("text/html", "<h1>Fetched Doc</h1>")),
        ),
        patch("forge.workflow.utils.references.write_to_cache", AsyncMock()),
    ):

        injected = await fetch_and_inject_references(
            state, mock_jira, state["spec_content"]
        )

        assert "Original specifications here." in injected
        assert "## External References" in injected
        assert "Reference: https://example.com/standing" in injected
        assert "Standing Doc" in injected
        assert "Reference: https://example.com/comment1" in injected
        assert "Comment Doc" in injected
        assert "# Fetched Doc" in injected


@pytest.mark.asyncio
async def test_pdf_deferrals_warning() -> None:
    state = {
        "ticket_key": "PROJ-123",
        "spec_content": "Specs",
        "context": {"run_id": "test-uuid"},
    }

    mock_jira = MagicMock()
    mock_jira.get_project_references = AsyncMock(
        return_value=[{"url": "https://example.com/spec.pdf", "description": "PDF Doc"}]
    )
    mock_jira.get_comments = AsyncMock(return_value=[])

    with (
        patch(
            "forge.workflow.utils.references.read_from_cache",
            AsyncMock(return_value=None),
        ),
        patch(
            "forge.workflow.utils.references.fetch_reference_url",
            AsyncMock(return_value=("application/pdf", "")),
        ),
    ):

        injected = await fetch_and_inject_references(
            state, mock_jira, state["spec_content"]
        )
        assert (
            "[WARNING: PDF reference deferred. Automatic text extraction from PDF URL is not supported"
            in injected
        )
